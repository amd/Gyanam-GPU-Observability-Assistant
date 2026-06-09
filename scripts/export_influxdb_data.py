#!/usr/bin/env python3
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Export InfluxDB metrics to CSV (optionally gzipped) for analysis.

Designed for the gyanam bucket schema (single field `value` per measurement)
but works against arbitrary InfluxDB v2 buckets via flags.

Performance levers (see --help for details):

  Server-side:
    * HTTP gzip on responses (always on)
    * No-op pivot dropped by default — pivot is expensive and is a no-op
      for the gyanam schema. Pass --pivot to re-enable for multi-field buckets.
    * --aggregate-window / --aggregate-fn for downsample-on-the-fly
    * --columns to restrict transferred columns server-side
  Wire/disk:
    * Output to .csv or .csv.gz transparently (5-10x smaller)
  Client:
    * Streaming write loop (constant memory) with adaptive progress + EMA ETA
  Robustness:
    * Per-chunk retry with exponential backoff + jitter
    * Atomic file rename — partial output never overwrites a good file
    * Pre-flight count() and post-export row-count verification
    * --max-rows ceiling to fail fast on accidental huge ranges
    * Warmup diagnostics (DNS / TCP / TLS / ping timings)
"""

import argparse
import csv
import gzip
import os
import random
import re
import shutil
import socket
import ssl
import sys
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from influxdb_client import InfluxDBClient

# =============================================================================
# Constants
# =============================================================================

# Per-HTTP-request timeout. The influxdb-client default of 10s is way too low.
# `or` guards against the env var being passed through docker as the empty
# string (`-e VAR=`), which makes os.environ.get return "" rather than the
# default — and `int("")` raises ValueError.
DEFAULT_TIMEOUT_MS = int(os.environ.get("INFLUXDB_TIMEOUT_MS") or "600000")

# Per-chunk retry budget for transient errors.
DEFAULT_MAX_RETRIES = int(os.environ.get("INFLUX_EXPORT_MAX_RETRIES") or "3")
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0

# Progress prints: every N rows OR every M seconds, whichever comes first.
PROGRESS_EVERY_ROWS = 50_000
PROGRESS_EVERY_SECONDS = 10.0

# Rate ETA: exponential moving average so recent rate dominates.
ETA_EMA_ALPHA = 0.3

# Rough size of a gyanam CSV row (timestamp + ~8 tag columns + value).
# Used only for the up-front "estimated output size" hint.
EST_BYTES_PER_ROW = 280
EST_GZIP_RATIO = 0.12  # CSV compresses to ~12% of original

# Above this many rows in a single un-chunked, un-aggregated export, we
# refuse to start without an explicit INFLUX_CONFIRM_LARGE=1 override.
# Observed in production: 196M-row single-window run failed three times
# in a row with ReadTimeout / RemoteDisconnected / IncompleteRead.
# `or` handles the env-set-to-empty case (docker `-e VAR=`) which would
# otherwise crash int("").
LARGE_EXPORT_THRESHOLD = int(os.environ.get("INFLUX_LARGE_EXPORT_THRESHOLD") or "10000000")

# Flux pipeline columns we always drop (Flux internals, not real data).
_FLUX_INTERNAL_COLS = ("_start", "_stop")

# Transient errors worth retrying on. We catch broadly because urllib3 wraps
# OS errors and the influxdb-client wraps urllib3 errors — pinning a narrow
# class set tends to miss real cases.
_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "broken pipe",
    "reset",
    "eof",
    "remote end closed",
    "service unavailable",
)


# =============================================================================
# Format helpers
# =============================================================================


def _human_count(n: int) -> str:
    """1234567 -> '1.23M'."""
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= threshold:
            return f"{n / threshold:.2f}{suffix}"
    return str(n)


def _human_duration(seconds: float) -> str:
    """90 -> '1m30s'; 7320 -> '2h02m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _human_bytes(n: float) -> str:
    """1234567 -> '1.18 MB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# =============================================================================
# Time parsing
# =============================================================================


def parse_flux_time(s: str, anchor: datetime | None = None) -> datetime:
    """Resolve a Flux time expression to an absolute UTC datetime.

    Supports: now()/now, negative durations (-7d, -24h, -90m, -3600s),
    ISO-8601 timestamps with optional 'Z' suffix.

    Used by the chunking logic — Flux accepts these forms inline, but to
    split a range into windows we need absolute endpoints.
    """
    s = s.strip()
    if anchor is None:
        anchor = datetime.now(UTC)
    if s in ("now()", "now"):
        return anchor
    m = re.match(r"^-(\d+)([smhdw])$", s)
    if m:
        n = int(m.group(1))
        unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return anchor - timedelta(seconds=n * unit_seconds)
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso)
    except ValueError as e:
        raise ValueError(f"Could not parse Flux time {s!r}: {e}") from e


def _iso_z(dt: datetime) -> str:
    """RFC3339 with 'Z' suffix that Flux likes."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _print_resolved_tunables(args) -> None:
    """Print the tunables that actually took effect.

    The single biggest source of "but I set X..." support questions is
    `sudo` stripping the caller's env. Showing the resolved values up
    front turns a 40-minute mystery failure into a 5-second sanity
    check.
    """
    # Mark values that differ from the LITERAL ship-with defaults — not
    # the runtime constants (which would themselves move when env vars
    # are set, hiding the override from the user). Listed defaults must
    # match the values in the argparse / module-constant declarations.
    tunables = [
        ("INFLUXDB_TIMEOUT_MS", DEFAULT_TIMEOUT_MS, 600_000),
        ("INFLUX_CHUNK_HOURS", args.chunk_hours, 0),
        ("INFLUX_EXPORT_MAX_RETRIES", args.max_retries, 3),
        ("INFLUX_LARGE_EXPORT_THRESHOLD", LARGE_EXPORT_THRESHOLD, 10_000_000),
        ("INFLUX_MAX_ROWS", args.max_rows, None),
        ("INFLUX_FIELD_NAME", args.field_name, None),
        ("INFLUX_TARGET_TAG", args.target_tag, "target_name"),
        ("INFLUX_AGGREGATE_WINDOW", args.aggregate_window, None),
        ("INFLUX_AGGREGATE_FN", args.aggregate_fn, "mean"),
        ("INFLUX_PIVOT", args.pivot, False),
        ("INFLUX_DRY_RUN", args.dry_run, False),
        ("INFLUX_DEBUG_QUERY", args.debug_query, False),
        ("INFLUX_QUIET", args.quiet, False),
        ("INFLUX_SKIP_WARMUP", args.skip_warmup, False),
    ]
    print("Resolved tunables (any '*' marks a non-default value):")
    for name, value, default in tunables:
        marker = " *" if value != default else "  "
        # Render unset / None as "—" for readability.
        display = "—" if value is None or value == "" else value
        print(f"  {marker} {name:<28} = {display}")
    print(
        "  → If any of these look wrong, you probably ran with `sudo` "
        "without `-E` and the\n"
        "    env var was stripped. Re-run with `sudo -E ./gyanam.sh ...` "
        "or set vars after\n"
        "    sudo: `sudo INFLUX_CHUNK_HOURS=6 ./gyanam.sh ...`"
    )


# =============================================================================
# I/O helpers (transparent gzip)
# =============================================================================


def _is_gzip_path(path: str | Path) -> bool:
    """Detect gzip output by extension, ignoring a trailing .part marker.

    `data.csv.gz.part` (the atomic-write staging file) must be detected as
    gzip too, otherwise we'd write uncompressed bytes into a .gz file.
    """
    s = str(path)
    if s.endswith(".part"):
        s = s[: -len(".part")]
    return s.endswith(".gz")


def _open_text_writer(path: str | Path):
    """Open `path` for text writing; transparently gzip if it ends in .gz.

    Uses a 1 MB buffer for plain files; gzip handles its own buffering.
    """
    p = str(path)
    if _is_gzip_path(p):
        # compresslevel=4 is a good throughput/size trade-off for CSV.
        return gzip.open(p, "wt", newline="", encoding="utf-8", compresslevel=4)
    return open(p, "w", newline="", encoding="utf-8", buffering=1 << 20)  # noqa: SIM115


def _open_text_reader(path: str | Path):
    """Open a plain or gzip text file for reading.

    Note: Caller must use as context manager (with statement).
    """
    p = str(path)
    if _is_gzip_path(p):
        return gzip.open(p, "rt", encoding="utf-8")
    return open(p, encoding="utf-8")  # noqa: SIM115


def _part_path(final: str | Path) -> Path:
    """final.csv -> final.csv.part (atomic write target)."""
    return Path(str(final) + ".part")


# =============================================================================
# Warmup diagnostics
# =============================================================================


def warmup_check(url: str, verify_ssl: bool = False, quiet: bool = False) -> dict:
    """Time DNS / TCP / TLS so the operator can localise slowness early.

    Returns a dict with `dns_ms`, `connect_ms`, `tls_ms` (None for http://).
    Always returns even on partial failures — prints a clear warning if any
    step is unusually slow.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        if not quiet:
            print(f"⚠ warmup: cannot parse host from {url!r}", file=sys.stderr)
        return {"dns_ms": None, "connect_ms": None, "tls_ms": None}

    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    is_https = parsed.scheme == "https"

    result: dict[str, float | None] = {"dns_ms": None, "connect_ms": None, "tls_ms": None}
    sock = None
    try:
        t0 = time.monotonic()
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result["dns_ms"] = (time.monotonic() - t0) * 1000

        t1 = time.monotonic()
        sock = socket.create_connection((host, port), timeout=5)
        result["connect_ms"] = (time.monotonic() - t1) * 1000

        if is_https:
            t2 = time.monotonic()
            ctx = ssl.create_default_context()
            # Enforce TLS 1.2 minimum — the default context may accept older
            # versions on some Python builds, which CodeQL flags.
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if not verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
            result["tls_ms"] = (time.monotonic() - t2) * 1000
    except Exception as e:
        if not quiet:
            print(
                f"⚠ warmup: probe failed at "
                f"{'TLS' if result['connect_ms'] else ('TCP' if result['dns_ms'] else 'DNS')}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    finally:
        if sock is not None:
            with suppress(Exception):
                sock.close()

    if not quiet:
        dns = result["dns_ms"]
        tcp = result["connect_ms"]
        tls = result["tls_ms"]
        dns_s = f"{dns:.0f}ms" if dns is not None else "FAIL"
        tcp_s = f"{tcp:.0f}ms" if tcp is not None else "FAIL"
        tls_s = f"{tls:.0f}ms" if tls is not None else ("--" if not is_https else "FAIL")
        print(
            f"Warmup ({host}:{port}): DNS {dns_s}, TCP {tcp_s}, TLS {tls_s}",
            flush=True,
        )
        if dns and dns > 200:
            print("  ⚠ DNS is slow (>200ms) — check resolver / /etc/hosts.", file=sys.stderr)
        if tcp and tcp > 500:
            print(
                "  ⚠ TCP connect is slow (>500ms) — check network path / firewall.", file=sys.stderr
            )
        if tls and tls > 1000:
            print("  ⚠ TLS handshake is slow (>1s) — check cert chain / cipher.", file=sys.stderr)
    return result


# =============================================================================
# Bucket utilities
# =============================================================================


def bucket_exists(client: InfluxDBClient, bucket: str, org: str) -> bool:
    """Return True iff a bucket with the given name exists in the org."""
    try:
        b = client.buckets_api().find_bucket_by_name(bucket)
        return b is not None
    except Exception:
        # Some org/permission combos throw rather than return None.
        try:
            all_buckets = client.buckets_api().find_buckets(org=org).buckets
            return any(x.name == bucket for x in all_buckets)
        except Exception:
            return False


def check_bucket_status(client: InfluxDBClient, org: str) -> None:
    """Print all buckets with retention + type guess."""
    buckets = client.buckets_api().find_buckets(org=org).buckets

    print("\n=== InfluxDB Bucket Status ===\n")
    print(f"{'Bucket Name':<30} {'Retention':<15} {'Type':<20}")
    print("-" * 65)

    for b in buckets:
        if b.name.startswith("_"):
            continue
        if "15m" in b.name or "15min" in b.name:
            kind = "15-minute downsampled"
        elif "hourly" in b.name or "1h" in b.name:
            kind = "Hourly downsampled"
        elif "daily" in b.name or "1d" in b.name:
            kind = "Daily downsampled"
        else:
            kind = "Raw metrics"

        retention_s = b.retention_rules[0].every_seconds if b.retention_rules else 0
        if retention_s == 0:
            retention = "Infinite"
        elif retention_s < 3600:
            retention = f"{retention_s // 60}m"
        elif retention_s < 86400:
            retention = f"{retention_s // 3600}h"
        else:
            retention = f"{retention_s // 86400}d"

        print(f"{b.name:<30} {retention:<15} {kind:<20}")
    print()


def list_measurements(client: InfluxDBClient, bucket: str, org: str) -> None:
    """List all measurement names in a bucket."""
    query = 'import "influxdata/influxdb/schema"\n' f'schema.measurements(bucket: "{bucket}")\n'
    try:
        tables = client.query_api().query(query, org=org)
    except Exception as e:
        print(f"Error listing measurements: {e}", file=sys.stderr)
        return

    measurements = [r.get_value() for t in tables for r in t.records]
    print(f"\n=== Measurements in '{bucket}' ===\n")
    for m in sorted(measurements):
        print(f"  - {m}")
    print()


# =============================================================================
# Pre-flight count
# =============================================================================


def count_records_in_range(
    client: InfluxDBClient,
    bucket: str,
    org: str,
    start_time: str,
    stop_time: str,
    measurement: str | None = None,
    targets: list[str] | None = None,
    field_name: str | None = None,
    target_tag: str = "target_name",
    debug_query: bool = False,
) -> tuple[int, float, float]:
    """Return (count, total_seconds, first_byte_seconds).

    Counts post-filter raw points in the range.

    `field_name=None` (the default) counts ALL fields in the bucket —
    one count per InfluxDB point. For the gyanam collector (single
    `value` field per measurement) this equals the long-format CSV row
    count. For multi-field buckets (e.g. downsampled with mean/max/min)
    the count equals N × the pivoted CSV row count, where N is the
    number of fields per measurement — pass `--field-name=<key>` to
    narrow if you want the pivoted-row estimate.

    Returns (-1, elapsed, -1) on failure so callers can proceed without
    an ETA estimate.
    """
    query = _build_count_query(
        bucket=bucket,
        start_time=start_time,
        stop_time=stop_time,
        measurement=measurement,
        targets=targets,
        field_name=field_name,
        target_tag=target_tag,
    )
    if debug_query:
        print(f"-- Flux count query --\n{query}", file=sys.stderr)

    sent_at = time.monotonic()
    first_byte_at: float | None = None
    total = 0
    try:
        stream = client.query_api().query_stream(query, org=org)
        for record in stream:
            if first_byte_at is None:
                first_byte_at = time.monotonic()
            v = record.get_value()
            if isinstance(v, int | float):
                total += int(v)
    except Exception as e:
        elapsed = time.monotonic() - sent_at
        print(
            f"  ⚠ Pre-flight count failed in {elapsed:.1f}s: " f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return (-1, elapsed, -1.0)

    elapsed = time.monotonic() - sent_at
    first_byte = (first_byte_at - sent_at) if first_byte_at is not None else -1.0
    return (total, elapsed, first_byte)


def diagnose_zero_rows(
    client: InfluxDBClient,
    bucket: str,
    org: str,
    start_time: str,
    stop_time: str,
    *,
    measurement: str | None = None,
    targets: list[str] | None = None,
    field_name: str | None = None,
    target_tag: str = "target_name",
    debug_query: bool = False,
) -> None:
    """When the pre-flight count returns 0, probe the bucket to tell the
    operator WHY: total points in range, distinct measurements, distinct
    field keys, distinct tag keys, and a sample of `target_tag` values.

    Prints findings to stdout. Never raises — best-effort diagnostic.
    """
    qapi = client.query_api()

    def _run(label: str, q: str) -> list:
        if debug_query:
            print(f"-- diagnostic: {label} --\n{q}", file=sys.stderr)
        try:
            tables = qapi.query(q, org=org)
            return [r.get_value() for t in tables for r in t.records]
        except Exception as e:
            print(f"  ⚠ diagnostic '{label}' failed: {type(e).__name__}: {e}", file=sys.stderr)
            return []

    # 1. Total un-filtered point count in the range (no measurement/target/field).
    q_total = (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: {start_time}, stop: {stop_time})\n"
        f"  |> group()\n"
        f"  |> count()\n"
    )
    total = _run("total points in range (no filters)", q_total)
    total_n = int(total[0]) if total and isinstance(total[0], int | float) else 0
    print(f"  • total points in range (no filters): {total_n:,}")

    if total_n == 0:
        print(
            "    → the bucket itself has nothing in this range. "
            "Either the time window is wrong, retention has dropped "
            "the data, or the collector never wrote here.\n"
            "    Try: `./gyanam.sh influx-status` to confirm bucket "
            "retention; `./gyanam.sh influx-list <bucket>` to list "
            "measurements."
        )
        return

    # 2. Distinct measurements (so the operator can check --measurement).
    q_meas = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.measurements(bucket: "{bucket}", '
        f"start: {start_time}, stop: {stop_time})\n"
    )
    meas = sorted(set(_run("distinct measurements", q_meas)))
    if meas:
        preview = ", ".join(meas[:8])
        more = f" (+{len(meas)-8} more)" if len(meas) > 8 else ""
        print(f"  • measurements present: {len(meas)} ({preview}{more})")
        if measurement and measurement not in meas:
            print(
                f"    → --measurement={measurement!r} is NOT in this bucket. "
                f"Drop the flag or pick one from above."
            )

    # 3. Distinct field keys (so the operator can check --field-name).
    q_fields = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.fieldKeys(bucket: "{bucket}", '
        f"start: {start_time}, stop: {stop_time})\n"
    )
    fields = sorted(set(_run("distinct field keys", q_fields)))
    if fields:
        print(f"  • field keys present: {', '.join(fields[:10])}")
        if field_name and field_name not in fields:
            print(
                f"    → --field-name={field_name!r} does NOT exist in this "
                f"bucket. Either drop --field-name (the default exports "
                f"every field) or set it to one of the keys listed above."
            )

    # 4. Distinct tag keys (so the operator can check --target-tag).
    q_tags = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.tagKeys(bucket: "{bucket}", '
        f"start: {start_time}, stop: {stop_time})\n"
    )
    tag_keys = sorted(set(_run("distinct tag keys", q_tags)))
    if tag_keys:
        print(f"  • tag keys present: {', '.join(tag_keys[:12])}")
        if targets and target_tag not in tag_keys:
            # Try the legacy / common alternatives.
            suggestions = [
                k for k in ("target_name", "target", "host", "hostname", "node") if k in tag_keys
            ]
            print(
                f"    → --target-tag={target_tag!r} does NOT exist as a tag. "
                f"This is the most likely cause of 0 rows when --targets "
                f"is used."
            )
            if suggestions:
                print(
                    f"    Try: --target-tag={suggestions[0]} "
                    f"(other candidates: {', '.join(suggestions[1:]) or 'none'})"
                )

    # 5. Sample of target_tag values (so the operator can verify their list).
    if target_tag in tag_keys:
        q_values = (
            'import "influxdata/influxdb/schema"\n'
            f'schema.tagValues(bucket: "{bucket}", tag: "{target_tag}", '
            f"start: {start_time}, stop: {stop_time})\n"
        )
        values = sorted(set(_run(f"distinct {target_tag} values", q_values)))
        if values:
            sample = ", ".join(values[:6])
            more = f" (+{len(values)-6} more)" if len(values) > 6 else ""
            print(f"  • {target_tag} values in range: {len(values)} " f"({sample}{more})")
            if targets:
                missing = [t for t in targets if t not in values]
                if missing:
                    print(
                        f"    → these --targets values are NOT in the "
                        f"bucket: {', '.join(missing[:5])}"
                        f"{f' (+{len(missing)-5} more)' if len(missing)>5 else ''}"
                    )

    print(
        "\n  Tip: re-run with INFLUX_DEBUG_QUERY=1 to print the actual " "Flux queries being sent."
    )


def _build_count_query(
    bucket: str,
    start_time: str,
    stop_time: str,
    measurement: str | None,
    targets: list[str] | None,
    field_name: str | None,
    target_tag: str,
) -> str:
    """Assemble the Flux query used by `count_records_in_range`.

    Notes:
    - We deliberately do NOT `keep(columns: ["_time"])` before `count()`.
      `count()` defaults to operating on the `_value` column; dropping it
      would silently return 0 records, which used to be the most common
      "0 rows / 0 bytes" symptom in dry-runs.
    - The target-tag filter uses `target_tag` (default `target_name`),
      which matches what `collector_main.py` writes
      (`extra_tags = {"target_name": result.target_name}`). Older docs /
      builds used `r.target` — that path never matched anything and was a
      silent dropper.
    - `field_name=None` (default) skips the field filter and counts all
      points across all field keys. That's the safe default for arbitrary
      buckets; pass an explicit key to narrow to a single field.
    """
    q = f'from(bucket: "{bucket}")\n'
    q += f"  |> range(start: {start_time}, stop: {stop_time})\n"
    if measurement:
        q += f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
    if targets:
        clause = " or ".join(f'r.{target_tag} == "{t}"' for t in targets)
        q += f"  |> filter(fn: (r) => {clause})\n"
    if field_name:
        q += f'  |> filter(fn: (r) => r._field == "{field_name}")\n'
    q += "  |> group()\n"
    q += "  |> count()\n"
    return q


# =============================================================================
# Query builder
# =============================================================================


def build_export_query(
    bucket: str,
    start_time: str,
    stop_time: str,
    measurement: str | None = None,
    targets: list[str] | None = None,
    pivot: bool = False,
    aggregate_window: str | None = None,
    aggregate_fn: str = "mean",
    columns_keep: list[str] | None = None,
    target_tag: str = "target_name",
    field_name: str | None = None,
) -> str:
    """Assemble the export Flux query.

    Defaults are tuned for the gyanam schema (single `value` field):
      - No pivot (pivot is expensive and a no-op for single-field schemas).
      - _value renamed to `value`, _field renamed to `field` for cleaner CSV.
      - No field filter — export every field key in the bucket.

    Pass `pivot=True` for multi-field schemas where row-per-timestamp wide
    output is wanted. With `aggregate_window`, points are bucketed
    server-side, hugely reducing export volume for analytics. Pass
    `field_name="<key>"` to restrict the export (and the pre-flight
    count) to a single field; this is what makes count match the
    pivoted row count on a multi-field bucket.
    """
    q = f'from(bucket: "{bucket}")\n'
    q += f"  |> range(start: {start_time}, stop: {stop_time})\n"
    if measurement:
        q += f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
    if targets:
        # target_tag defaults to "target_name" (what the collector writes).
        # Older versions used `r.target` which never matched anything.
        clause = " or ".join(f'r.{target_tag} == "{t}"' for t in targets)
        q += f"  |> filter(fn: (r) => {clause})\n"
    if field_name:
        # Mirrors the count query; only applied if user explicitly sets
        # field_name / --field-name / INFLUX_FIELD_NAME. Otherwise all
        # fields are exported (one CSV row per InfluxDB point in long
        # format).
        q += f'  |> filter(fn: (r) => r._field == "{field_name}")\n'

    if aggregate_window:
        # Server-side downsample. createEmpty:false so we don't fabricate rows.
        q += (
            f"  |> aggregateWindow(every: {aggregate_window}, "
            f"fn: {aggregate_fn}, createEmpty: false)\n"
        )

    if pivot:
        # Multi-field-aware wide output. Required if you actually have
        # several fields per measurement; otherwise it's just sort+transpose
        # for no semantic change.
        q += '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        q += '  |> drop(columns: ["_start", "_stop"])\n'
    else:
        # Long format. Rename for friendly CSV headers.
        q += '  |> drop(columns: ["_start", "_stop"])\n'
        q += '  |> rename(columns: {_value: "value", _field: "field"})\n'

    if columns_keep:
        # Quote each column name; Flux keep() takes a string array.
        cols = ", ".join(f'"{c}"' for c in columns_keep)
        q += f"  |> keep(columns: [{cols}])\n"

    return q


# =============================================================================
# Stream a single window (with retry)
# =============================================================================


def _looks_transient(exc: Exception) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def _backoff_seconds(attempt: int) -> float:
    base: float = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2**attempt))
    return base + random.uniform(0, base * 0.25)


def _stream_window_once(
    query_api,
    query: str,
    output_path: Path,
    estimated_total: int,
    written_before_window: int,
    overall_start: float,
    rate_state: dict,
    quiet: bool,
) -> int:
    """Stream ONE window's results into `output_path`. Returns row count.

    Caller is responsible for retry & atomic-rename. This function always
    starts a fresh output file (truncating any prior partial content from a
    previous failed attempt).
    """
    sent_at = time.monotonic()
    record_stream = query_api.query_stream(query)
    first_byte_at: float | None = None
    written = 0
    columns: list[str] | None = None
    datetime_cols: list[int] = []
    next_progress_at = sent_at + PROGRESS_EVERY_SECONDS

    with _open_text_writer(output_path) as csvfile:
        out = csv.writer(csvfile)

        for record in record_stream:
            if first_byte_at is None:
                first_byte_at = time.monotonic()
                if not quiet:
                    print(
                        f"  first row arrived after "
                        f"{first_byte_at - sent_at:.1f}s "
                        f"(query-plan + initial read)",
                        flush=True,
                    )

            values = record.values

            if columns is None:
                # Establish stable column order from the first record.
                # Filter Flux-internal columns we don't want in the CSV.
                columns = [c for c in sorted(values.keys()) if c not in _FLUX_INTERNAL_COLS]
                datetime_cols = [
                    i for i, c in enumerate(columns) if isinstance(values.get(c), datetime)
                ]
                out.writerow(columns)

            # Build row positionally — much faster than DictWriter.
            row = [values.get(c) for c in columns]
            # Inline-convert datetime columns. Most rows have only `_time` as
            # datetime, so this list of indices is short.
            for idx in datetime_cols:
                v = row[idx]
                if v is not None:
                    row[idx] = v.isoformat()
            out.writerow(row)
            written += 1

            now = time.monotonic()
            if written % PROGRESS_EVERY_ROWS == 0 or now >= next_progress_at:
                next_progress_at = now + PROGRESS_EVERY_SECONDS
                total_so_far = written_before_window + written
                elapsed = now - overall_start
                rate = total_so_far / elapsed if elapsed > 0 else 0.0
                # EMA over rate so ETA reacts to slow-downs.
                ema = rate_state.get("ema_rate") or rate
                ema = ETA_EMA_ALPHA * rate + (1 - ETA_EMA_ALPHA) * ema
                rate_state["ema_rate"] = ema
                eta_part = ""
                if estimated_total > 0 and ema > 0:
                    remaining = max(0, estimated_total - total_so_far)
                    eta_part = f", ETA {_human_duration(remaining / ema)}"
                if not quiet:
                    print(
                        f"  ...{total_so_far:,} rows "
                        f"({_human_count(total_so_far)}, "
                        f"{ema:.0f} rec/s{eta_part})",
                        flush=True,
                    )

    return written


def stream_window_with_retry(
    query_api,
    query: str,
    output_path: Path,
    *,
    estimated_total: int,
    written_before_window: int,
    overall_start: float,
    rate_state: dict,
    max_retries: int,
    quiet: bool,
) -> int:
    """_stream_window_once with retry on transient errors.

    Each attempt fully rewrites `output_path`, so a partial write from a
    failed attempt cannot leak duplicates into the final output.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _stream_window_once(
                query_api=query_api,
                query=query,
                output_path=output_path,
                estimated_total=estimated_total,
                written_before_window=written_before_window,
                overall_start=overall_start,
                rate_state=rate_state,
                quiet=quiet,
            )
        except Exception as e:
            last_exc = e
            if attempt == max_retries or not _looks_transient(e):
                raise
            wait = _backoff_seconds(attempt)
            print(
                f"  ⚠ window failed ({type(e).__name__}: {e}); "
                f"retrying in {wait:.1f}s (attempt {attempt + 2}/{max_retries + 1})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
    # unreachable, but keeps type checker happy
    raise last_exc if last_exc else RuntimeError("retry loop exited without exception")


# =============================================================================
# Concatenating chunk files (multi-window mode)
# =============================================================================


def _concat_chunk_files(parts: list[Path], final_path: Path) -> None:
    """Concatenate part files into `final_path`, deduping CSV headers.

    Works for both plain CSV and .csv.gz (handled transparently via the
    _open_text_* helpers).
    """
    with _open_text_writer(final_path) as out:
        for i, p in enumerate(parts):
            with _open_text_reader(p) as inp:
                if i > 0:
                    # Skip header line on second+ files.
                    next(inp, None)
                shutil.copyfileobj(inp, out)


# =============================================================================
# Main export orchestrator
# =============================================================================


def export_metrics_to_csv(
    client: InfluxDBClient,
    bucket: str,
    org: str,
    output_file: str,
    start_time: str,
    stop_time: str,
    *,
    measurement: str | None = None,
    targets: list[str] | None = None,
    pivot: bool = False,
    aggregate_window: str | None = None,
    aggregate_fn: str = "mean",
    columns_keep: list[str] | None = None,
    chunk_hours: int = 0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_rows: int | None = None,
    field_name: str | None = None,
    target_tag: str = "target_name",
    skip_warmup: bool = False,
    quiet: bool = False,
    debug_query: bool = False,
    url: str = "http://localhost:8086",
    verify_ssl: bool = False,
) -> None:
    """End-to-end export pipeline.

    Steps: warmup → bucket check → pre-flight count → window planning →
    streaming export with per-chunk retry → atomic rename → verification.
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n=== InfluxDB Export ===")
    print(f"  bucket:       {bucket}")
    print(f"  time range:   {start_time}  →  {stop_time}")
    if measurement:
        print(f"  measurement:  {measurement}")
    if targets:
        print(f"  targets:      {', '.join(targets)}")
    print(f"  pivot:        {'on' if pivot else 'off (long format)'}")
    if aggregate_window:
        print(f"  aggregate:    every {aggregate_window} via {aggregate_fn}()")
    if columns_keep:
        print(f"  columns:      {', '.join(columns_keep)}")
    print(f"  chunk hours:  {'(single window)' if chunk_hours <= 0 else chunk_hours}")
    print(f"  output:       {output_file} ({'gzip' if _is_gzip_path(output_file) else 'plain'})")
    print(f"  retries:      up to {max_retries} per window")
    print()

    # 1. Warmup probe.
    if not skip_warmup:
        warmup_check(url, verify_ssl=verify_ssl, quiet=quiet)

    # 2. Bucket existence (fast — avoids a useless count query against a typo).
    if not bucket_exists(client, bucket, org):
        print(f"❌ Bucket {bucket!r} does not exist in org {org!r}.", file=sys.stderr)
        sys.exit(2)

    # 3. Pre-flight count.
    print("Pre-flight count() query running...")
    estimated_total, count_elapsed, count_first_byte = count_records_in_range(
        client,
        bucket,
        org,
        start_time,
        stop_time,
        measurement,
        targets,
        field_name=field_name,
        target_tag=target_tag,
        debug_query=debug_query,
    )
    if estimated_total >= 0:
        fb = f", first byte after {count_first_byte:.2f}s" if count_first_byte >= 0 else ""
        print(
            f"  estimated {estimated_total:,} records "
            f"({_human_count(estimated_total)}) — count() took "
            f"{count_elapsed:.2f}s{fb}"
        )
        # Output size hint.
        est_bytes = estimated_total * EST_BYTES_PER_ROW
        if _is_gzip_path(output_file):
            print(
                f"  estimated output: ~{_human_bytes(est_bytes * EST_GZIP_RATIO)} "
                f"(gzip; ~{_human_bytes(est_bytes)} uncompressed)"
            )
        else:
            print(f"  estimated output: ~{_human_bytes(est_bytes)}")
    else:
        print("  estimate unavailable; proceeding without an ETA.")

    # 4. Safety ceiling.
    if max_rows is not None and estimated_total > max_rows:
        print(
            f"\n❌ Refusing to export: estimated {estimated_total:,} rows "
            f"exceeds --max-rows {max_rows:,}. Narrow the range/filters, "
            f"use --aggregate-window, or raise --max-rows.",
            file=sys.stderr,
        )
        sys.exit(3)

    # 4b. Big-export guardrail. Without chunking, exports above this
    # size hit InfluxDB query timeouts / OOM and end up streaming for
    # hours before failing mid-stream (e.g. 196M-row single-window run
    # observed in production failed after 30+ minutes with
    # IncompleteRead). Force the operator to acknowledge.
    if (
        estimated_total > LARGE_EXPORT_THRESHOLD
        and chunk_hours <= 0
        and not aggregate_window
        and os.environ.get("INFLUX_CONFIRM_LARGE", "").lower() not in ("1", "true", "yes")
    ):
        print(
            f"\n❌ Pre-flight count is {estimated_total:,} rows "
            f"({_human_count(estimated_total)}) — this is a LARGE export and "
            f"will almost certainly fail without chunking or aggregation.\n"
            f"\n"
            f"   Pick ONE of the following and re-run:\n"
            f"   • For analytics — aggregate server-side (cuts rows ~10-100×):\n"
            f"       INFLUX_AGGREGATE_WINDOW=1h INFLUX_AGGREGATE_FN=mean ./gyanam.sh ...\n"
            f"   • For raw fidelity — chunk by time window:\n"
            f"       INFLUX_CHUNK_HOURS=1 ./gyanam.sh ...\n"
            f"   • To override and proceed anyway (NOT RECOMMENDED):\n"
            f"       INFLUX_CONFIRM_LARGE=1 ./gyanam.sh ...\n"
            f"\n"
            f"   Why this guard exists: a single-window export of "
            f"~{_human_count(estimated_total)} rows takes hours to plan +\n"
            f"   stream on the InfluxDB side, and any mid-stream disconnect "
            f"forces a full restart\n"
            f"   from row 0. Chunking caps each HTTP request to a "
            f"recoverable size.",
            file=sys.stderr,
        )
        sys.exit(4)

    if estimated_total == 0:
        print(
            "No data in the requested range — running diagnostic probe "
            "so you can see why before giving up..."
        )
        diagnose_zero_rows(
            client,
            bucket,
            org,
            start_time,
            stop_time,
            measurement=measurement,
            targets=targets,
            field_name=field_name,
            target_tag=target_tag,
            debug_query=debug_query,
        )
        return

    # 5. Window planning.
    if chunk_hours > 0:
        start_dt = parse_flux_time(start_time)
        stop_dt = parse_flux_time(stop_time)
        delta = timedelta(hours=chunk_hours)
        windows: list[tuple[str, str]] = []
        cur = start_dt
        while cur < stop_dt:
            end = min(cur + delta, stop_dt)
            windows.append((_iso_z(cur), _iso_z(end)))
            cur = end
        print(f"\nSplit into {len(windows)} window(s) of {chunk_hours}h each.")
    else:
        windows = [(start_time, stop_time)]

    # 6. Execute. Two code paths to keep simple cases simple:
    #    - single window: write to output.part, atomic rename on success.
    #    - multi window: write each chunk into a temp dir, concat at end,
    #      then atomic rename.
    query_api = client.query_api()
    overall_start = time.monotonic()
    rate_state: dict = {}
    total_written = 0

    part_final = _part_path(output_path)
    temp_dir: tempfile.TemporaryDirectory | None = None
    chunk_files: list[Path] = []

    try:
        if len(windows) == 1:
            ws, we = windows[0]
            query = build_export_query(
                bucket,
                ws,
                we,
                measurement,
                targets,
                pivot,
                aggregate_window,
                aggregate_fn,
                columns_keep,
                target_tag=target_tag,
                field_name=field_name,
            )
            if debug_query:
                print(f"-- Flux export query --\n{query}", file=sys.stderr)
            total_written = stream_window_with_retry(
                query_api=query_api,
                query=query,
                output_path=part_final,
                estimated_total=estimated_total,
                written_before_window=0,
                overall_start=overall_start,
                rate_state=rate_state,
                max_retries=max_retries,
                quiet=quiet,
            )
        else:
            temp_dir = tempfile.TemporaryDirectory(
                prefix="gyanam_export_", dir=str(output_path.parent)
            )
            tmp = Path(temp_dir.name)
            suffix = ".csv.gz" if _is_gzip_path(output_file) else ".csv"
            for i, (ws, we) in enumerate(windows, start=1):
                chunk_file = tmp / f"chunk_{i:04d}{suffix}"
                if not quiet:
                    print(f"\n[window {i}/{len(windows)}] {ws}  →  {we}")
                window_started = time.monotonic()
                query = build_export_query(
                    bucket,
                    ws,
                    we,
                    measurement,
                    targets,
                    pivot,
                    aggregate_window,
                    aggregate_fn,
                    columns_keep,
                    target_tag=target_tag,
                    field_name=field_name,
                )
                if debug_query and i == 1:
                    print(f"-- Flux export query (chunk 1) --\n{query}", file=sys.stderr)
                written = stream_window_with_retry(
                    query_api=query_api,
                    query=query,
                    output_path=chunk_file,
                    estimated_total=estimated_total,
                    written_before_window=total_written,
                    overall_start=overall_start,
                    rate_state=rate_state,
                    max_retries=max_retries,
                    quiet=quiet,
                )
                chunk_files.append(chunk_file)
                total_written += written
                window_elapsed = time.monotonic() - window_started
                rate = written / window_elapsed if window_elapsed > 0 else 0.0
                if not quiet:
                    print(
                        f"  window done: {written:,} rows in "
                        f"{_human_duration(window_elapsed)} ({rate:.0f} rec/s)"
                    )

            # Concatenate part files into the staging path. Dedup headers.
            if not quiet:
                print(f"\nConcatenating {len(chunk_files)} chunk files → " f"{part_final}...")
            concat_started = time.monotonic()
            _concat_chunk_files(chunk_files, part_final)
            if not quiet:
                print(f"  concat done in {_human_duration(time.monotonic() - concat_started)}")

    except KeyboardInterrupt:
        elapsed = time.monotonic() - overall_start
        print(
            f"\n⚠ Interrupted after {_human_duration(elapsed)}, "
            f"{total_written:,} rows written so far. "
            f"Partial output at {part_final} retained for inspection.",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as e:
        elapsed = time.monotonic() - overall_start
        print(
            f"\n❌ Export failed after {_human_duration(elapsed)} "
            f"with {total_written:,} rows written: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        # Clean up partial staging file; leave temp_dir for inspection unless empty.
        with suppress(OSError):
            if part_final.exists():
                part_final.unlink()
        sys.exit(1)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    # 7. Atomic rename → final file.
    if total_written == 0:
        print("\nNo rows written. Removing empty staging file.")
        with suppress(OSError):
            part_final.unlink()
        return

    os.replace(part_final, output_path)
    elapsed = time.monotonic() - overall_start
    overall_rate = total_written / elapsed if elapsed > 0 else 0.0
    actual_size = output_path.stat().st_size
    print(
        f"\n✅ Exported {total_written:,} rows in {_human_duration(elapsed)} "
        f"({overall_rate:.0f} rec/s avg) → {output_path}"
    )
    print(f"   File size: {_human_bytes(actual_size)}")

    # 8. Post-export verification.
    if estimated_total > 0:
        drift = (total_written - estimated_total) / estimated_total
        if abs(drift) > 0.02:  # 2% drift tolerance for concurrent writes
            print(
                f"   ⚠ Row count drift: expected ~{estimated_total:,}, "
                f"got {total_written:,} ({drift * 100:+.1f}%). "
                f"Could indicate writes happening during export, "
                f"or query inconsistency.",
                file=sys.stderr,
            )


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Export InfluxDB metrics to CSV (or .csv.gz) for analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check status of all buckets
  %(prog)s --check-status

  # List measurements in a bucket
  %(prog)s --list-measurements --bucket gpu_metrics

  # Quick: last 24h, gzipped output (recommended default)
  %(prog)s --export --bucket gpu_metrics --start -24h --output /tmp/last24h.csv.gz

  # Probe before exporting (no data written)
  %(prog)s --export --bucket gpu_metrics --start -7d --dry-run

  # Big export, chunked, gzipped, with safety ceiling
  %(prog)s --export --bucket gpu_metrics --start -30d \\
           --chunk-hours 6 --max-rows 200000000 \\
           --output /tmp/30d.csv.gz

  # Analysis export — downsample to 5-min means server-side (60x less data)
  %(prog)s --export --bucket gpu_metrics --start -30d \\
           --aggregate-window 5m --aggregate-fn mean \\
           --output /tmp/30d_5m.csv.gz

  # Narrow columns server-side (smaller, faster)
  %(prog)s --export --bucket gpu_metrics --start -24h \\
           --columns _time,_measurement,target,value \\
           --output /tmp/narrow.csv.gz

Recommended workflow for a remote / large export:
  1) --dry-run to see the row count + first-byte time + size estimate.
  2) If too large, add --aggregate-window 5m (or coarser) — almost always
     the right answer for analytics; cuts rows by 60x for 5-second data.
  3) For multi-day exports, --chunk-hours 6 splits into independent
     bounded windows; each has its own per-HTTP-request timeout and is
     independently retryable.
  4) Output to .csv.gz — both the wire transfer and the disk write are
     5-10x smaller for the same data.
  5) Tune INFLUXDB_TIMEOUT_MS if any single chunk is genuinely slow
     (default 600000 = 10m; sensible ceiling 1800000 = 30m per chunk).

Environment variables:
  INFLUXDB_URL              - InfluxDB URL (default: http://localhost:8086)
  INFLUXDB_TOKEN            - InfluxDB authentication token (required)
  INFLUXDB_ORG              - InfluxDB organization (default: prometheus)
  INFLUXDB_TIMEOUT_MS       - Per-HTTP-request timeout, ms (default: 600000)
                              With chunking this applies PER CHUNK.
  INFLUX_CHUNK_HOURS        - Default for --chunk-hours (default: 0)
  INFLUX_EXPORT_MAX_RETRIES - Default for --max-retries (default: 3)
        """,
    )

    # Connection
    parser.add_argument(
        "--url",
        default=os.environ.get("INFLUXDB_URL", "http://localhost:8086"),
        help="InfluxDB URL (env: INFLUXDB_URL)",
    )
    parser.add_argument("--token", help="InfluxDB token (env: INFLUXDB_TOKEN)")
    parser.add_argument(
        "--org",
        default=os.environ.get("INFLUXDB_ORG", "prometheus"),
        help="InfluxDB org (env: INFLUXDB_ORG)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify TLS certificates (default: off, matching the collector)",
    )

    # Actions
    parser.add_argument("--check-status", action="store_true", help="Check status of all buckets")
    parser.add_argument(
        "--list-measurements",
        action="store_true",
        help="List all measurements in --bucket",
    )
    parser.add_argument("--export", action="store_true", help="Export data to CSV (or .csv.gz)")

    # Query parameters
    parser.add_argument("--bucket", help="Bucket name to query")
    parser.add_argument(
        "--start",
        default="-24h",
        help="Start time (Flux expr or ISO-8601, e.g. -24h, 2026-01-01T00:00:00Z)",
    )
    parser.add_argument("--stop", default="now()", help="Stop time (default: now())")
    parser.add_argument("--measurement", help="Filter to a specific measurement")
    parser.add_argument("--targets", help="Comma-separated target= filter values")

    # Output
    parser.add_argument(
        "--output",
        help="Output CSV file path. If it ends in .gz, output is gzip-compressed.",
    )

    # Performance / shape
    parser.add_argument(
        "--pivot",
        action="store_true",
        help=(
            "Emit pivoted (wide) output. Default is long format because pivot "
            "is a no-op for single-field schemas like gyanam — and pivot is "
            "one of the most expensive Flux operators on big ranges."
        ),
    )
    parser.add_argument(
        "--aggregate-window",
        help=(
            "Server-side downsample to N-unit windows (e.g. 5m, 1h). "
            "Massively reduces export volume for analytics. Default: off."
        ),
    )
    parser.add_argument(
        "--aggregate-fn",
        default="mean",
        choices=("mean", "max", "min", "median", "sum", "count", "first", "last"),
        help="Aggregation function paired with --aggregate-window (default: mean)",
    )
    parser.add_argument(
        "--columns",
        help=(
            "Comma-separated list of column names to keep server-side. "
            "Reduces bytes-over-wire (recommended for narrow analyses). "
            'Example: --columns "_time,_measurement,target,value"'
        ),
    )
    parser.add_argument(
        "--field-name",
        default=os.environ.get("INFLUX_FIELD_NAME") or None,
        help=(
            "Filter the export and the pre-flight count to a single "
            "_field key (e.g. 'value', 'mean'). "
            "DEFAULT: unset — count and export ALL fields in the bucket "
            "(one CSV row per InfluxDB point in long format). "
            "Set this only if you want to narrow to one field, OR if "
            "you're in pivot mode against a multi-field bucket and want "
            "the row-count estimate to match the pivoted output. "
            "Env: INFLUX_FIELD_NAME (unset / empty = no filter)"
        ),
    )
    parser.add_argument(
        "--target-tag",
        default=os.environ.get("INFLUX_TARGET_TAG", "target_name"),
        help=(
            "Tag key that holds the target identifier "
            "(default: 'target_name' — matches gyanam's collector, "
            "which writes `extra_tags = {'target_name': result.target_name}`). "
            "Used by the --targets filter. Previous releases used 'target' "
            "which silently matched nothing. Env: INFLUX_TARGET_TAG"
        ),
    )

    # Big-export controls
    parser.add_argument(
        "--chunk-hours",
        type=int,
        default=int(os.environ.get("INFLUX_CHUNK_HOURS", "0")),
        help=(
            "Split the export into N-hour windows, each a separate HTTP "
            "request. 0 = single window. Env: INFLUX_CHUNK_HOURS"
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=(
            "Per-window retry budget on transient errors "
            "(timeout, connection-reset, server-disconnect). "
            "Env: INFLUX_EXPORT_MAX_RETRIES (default: 3)"
        ),
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=(
            "Refuse to export if the pre-flight count exceeds this number. "
            "Guards against accidental huge ranges (e.g. -10y vs -10d)."
        ),
    )

    # Behavioural toggles
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run only the pre-flight count + warmup, print findings, exit. "
            "Use to probe a bucket without committing to an export."
        ),
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Skip the DNS/TCP/TLS warmup probe at startup.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress periodic progress lines (final summary still printed).",
    )
    parser.add_argument(
        "--debug-query",
        action="store_true",
        default=os.environ.get("INFLUX_DEBUG_QUERY", "").lower() in ("1", "true", "yes"),
        help=(
            "Print every Flux query to stderr before sending. Useful for "
            "diagnosing 0-rows results. Env: INFLUX_DEBUG_QUERY"
        ),
    )

    args = parser.parse_args()

    # Force line-buffered stdout so progress / pre-flight prints appear
    # in event order alongside stderr error messages — was a real
    # problem when running inside Docker, where block-buffering on a
    # non-tty made the log read backwards.
    with suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    # Connection setup.
    token = args.token or os.getenv("INFLUXDB_TOKEN")
    if not token:
        print("Error: InfluxDB token required (--token or INFLUXDB_TOKEN env)", file=sys.stderr)
        sys.exit(1)

    client = InfluxDBClient(
        url=args.url,
        token=token,
        org=args.org,
        verify_ssl=args.verify_ssl,
        timeout=DEFAULT_TIMEOUT_MS,
        enable_gzip=True,  # 5-10x smaller responses for CSV
    )
    print(
        f"InfluxDB client: timeout={DEFAULT_TIMEOUT_MS}ms, "
        f"gzip=on, verify_ssl={args.verify_ssl}"
    )

    # Print resolved tunables so the user can confirm their env vars
    # made it through (sudo strips env by default — many users have
    # been bitten by silently-defaulted INFLUX_CHUNK_HOURS / TIMEOUT_MS).
    _print_resolved_tunables(args)

    try:
        if not client.ping():
            print(f"Error: Cannot connect to InfluxDB at {args.url}", file=sys.stderr)
            sys.exit(1)

        if args.check_status:
            check_bucket_status(client, args.org)
            return

        if args.list_measurements:
            if not args.bucket:
                print("Error: --bucket required for --list-measurements", file=sys.stderr)
                sys.exit(1)
            list_measurements(client, args.bucket, args.org)
            return

        if not args.export:
            parser.print_help()
            return

        # --- export path ---
        if not args.bucket:
            print("Error: --bucket required for --export", file=sys.stderr)
            sys.exit(1)
        if not args.dry_run and not args.output:
            print("Error: --output required for --export (or use --dry-run)", file=sys.stderr)
            sys.exit(1)

        targets = [t.strip() for t in args.targets.split(",")] if args.targets else None
        columns_keep = [c.strip() for c in args.columns.split(",")] if args.columns else None

        if args.dry_run:
            # Short-circuit: warmup + bucket-check + count, no real export call.
            if not args.skip_warmup:
                warmup_check(args.url, verify_ssl=args.verify_ssl, quiet=args.quiet)
            if not bucket_exists(client, args.bucket, args.org):
                print(
                    f"❌ Bucket {args.bucket!r} does not exist in org {args.org!r}.",
                    file=sys.stderr,
                )
                sys.exit(2)
            print("Dry-run: running pre-flight count only.")
            total, elapsed, first_byte = count_records_in_range(
                client,
                args.bucket,
                args.org,
                args.start,
                args.stop,
                args.measurement,
                targets,
                field_name=args.field_name,
                target_tag=args.target_tag,
                debug_query=args.debug_query,
            )
            if total > 0:
                fb = f", first byte after {first_byte:.2f}s" if first_byte >= 0 else ""
                print(
                    f"  estimated {total:,} rows ({_human_count(total)}) — "
                    f"count() took {elapsed:.2f}s{fb}"
                )
                est_bytes = total * EST_BYTES_PER_ROW
                print(
                    f"  estimated CSV size: ~{_human_bytes(est_bytes)} "
                    f"(~{_human_bytes(est_bytes * EST_GZIP_RATIO)} gzipped)"
                )
                if args.max_rows is not None and total > args.max_rows:
                    print(
                        f"  ⚠ would EXCEED --max-rows {args.max_rows:,}.",
                        file=sys.stderr,
                    )
            elif total == 0:
                # Zero rows: very rarely the right answer, very often a
                # filter / tag-name / field-name mistake. Run a diagnostic
                # probe of the bucket so the operator sees what's actually
                # there and which filter killed the result.
                print(
                    f"  ⚠ estimated 0 rows — running diagnostic probe of "
                    f"bucket {args.bucket!r} so you can see what's there..."
                )
                diagnose_zero_rows(
                    client,
                    args.bucket,
                    args.org,
                    args.start,
                    args.stop,
                    measurement=args.measurement,
                    targets=targets,
                    field_name=args.field_name,
                    target_tag=args.target_tag,
                    debug_query=args.debug_query,
                )
            else:
                print("  count failed; see error above.")
            return

        export_metrics_to_csv(
            client=client,
            bucket=args.bucket,
            org=args.org,
            output_file=args.output,
            start_time=args.start,
            stop_time=args.stop,
            measurement=args.measurement,
            targets=targets,
            pivot=args.pivot,
            aggregate_window=args.aggregate_window,
            aggregate_fn=args.aggregate_fn,
            columns_keep=columns_keep,
            chunk_hours=args.chunk_hours,
            max_retries=args.max_retries,
            max_rows=args.max_rows,
            field_name=args.field_name,
            target_tag=args.target_tag,
            skip_warmup=args.skip_warmup,
            quiet=args.quiet,
            debug_query=args.debug_query,
            url=args.url,
            verify_ssl=args.verify_ssl,
        )

    finally:
        client.close()


if __name__ == "__main__":
    main()
