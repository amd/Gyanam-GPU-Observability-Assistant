# Data Export Reference

Comprehensive guide to getting data **out** of the GYANAM (GPU
Observability Assistant) InfluxDB — covering the
`gyanam.sh influx-export` wrapper and every native InfluxDB
alternative, with side-by-side comparison and a decision guide.

> If you just want "the right command for my situation right now,"
> skip to **[Decision Guide](#decision-guide)**.

---

## Table of Contents

1. [At a Glance](#at-a-glance)
2. [Part 1: gyanam.sh influx-export](#part-1-gyanamsh-influx-export)
   - [What it is](#what-it-is)
   - [Command syntax](#command-syntax)
   - [Positional arguments](#positional-arguments)
   - [Environment variables](#environment-variables)
   - [Output file formats](#output-file-formats)
   - [What it prints](#what-it-prints)
   - [Recommended workflows](#recommended-workflows)
3. [Part 2: Native InfluxDB alternatives](#part-2-native-influxdb-alternatives)
   - [influx query (CLI)](#21-influx-query-cli)
   - [curl /api/v2/query](#22-curl-apiv2query)
   - [influx backup](#23-influx-backup)
   - [influxd inspect export-tsm](#24-influxd-inspect-export-tsm)
4. [Part 3: Side-by-side comparison](#part-3-side-by-side-comparison)
5. [Decision Guide](#decision-guide)
6. [Performance Notes](#performance-notes)
7. [Troubleshooting Cookbook](#troubleshooting-cookbook)

---

## At a Glance

| Goal | Use |
|------|-----|
| Pull data into Excel / pandas / spreadsheet | **`gyanam.sh influx-export`** with `.csv.gz` |
| Quick ad-hoc Flux query on a local InfluxDB | **`influx query --raw`** |
| Embed an export in a shell pipeline (no Python container) | **`curl /api/v2/query`** |
| Move data between InfluxDB instances | **`influx backup`** + `influx restore` |
| Re-ingest data later via line protocol | **`influxd inspect export-tsm`** |
| Big export on a remote machine (network unreliable) | **`gyanam.sh influx-export`** with chunks + retry |
| Multi-month analytics; rows >> 10M | **`gyanam.sh influx-export`** with `--aggregate-window` |

---

# Part 1: gyanam.sh influx-export

## What it is

A wrapper around the `scripts/export_influxdb_data.py` Python script,
run inside a transient `python:3.11-slim` Docker container so you
don't need to install anything on the host. The wrapper adds:

- Streaming write loop (constant memory, billions of rows OK)
- Pre-flight `count()` so you see the volume **before** committing
- Adaptive progress (rows/s + EMA-smoothed ETA + first-byte latency)
- Per-window retry with exponential backoff + jitter
- Atomic write (`.part` staging → `os.replace` on success)
- Time-windowed chunking so a multi-day export is bounded into
  independent HTTP requests, each retryable
- Server-side downsampling (`aggregateWindow`) for analytics
- HTTP gzip on the wire + transparent `.csv.gz` on disk
- DNS / TCP / TLS warmup probe to diagnose network slowness
- Bucket-existence check + `--max-rows` safety ceiling
- Post-export count-drift verification

## Command syntax

```bash
./gyanam.sh influx-export [bucket] [output] [start] [stop]
```

All positional arguments are optional with sensible defaults; all
behavioural knobs are environment variables (prepended to the command).

## Positional arguments

| Position | Name | Default | Notes |
|----------|------|---------|-------|
| 1 | `bucket` | `gpu_metrics` | InfluxDB bucket name. Validated against the server before the export starts. |
| 2 | `output` | `./exports/metrics_<TIMESTAMP>.csv` | Output file path. **If it ends in `.gz`, output is gzip-compressed transparently** — typically 5–10× smaller. Excel / pandas / LibreOffice all read `.csv.gz` natively. |
| 3 | `start` | `-24h` | Flux time expression. Examples: `-7d`, `-90m`, `-3600s`, `2026-05-01T00:00:00Z`. |
| 4 | `stop` | `now()` | Flux time expression. |

## Environment variables

All optional. Group them on the command line as needed; bash's
trailing-backslash convention works.

### Data shape & volume

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUX_AGGREGATE_WINDOW` | unset | Server-side downsample to this window (e.g. `5m`, `1h`, `1d`). Massive row reduction for analytics — `5m` on 5-second data is ~60× fewer rows. |
| `INFLUX_AGGREGATE_FN` | `mean` | Aggregation function paired with `_AGGREGATE_WINDOW`. Choices: `mean`, `max`, `min`, `median`, `sum`, `count`, `first`, `last`. |
| `INFLUX_COLUMNS` | unset | Comma-separated list of column names to keep server-side. Reduces bytes-over-wire when you only need a few. Example: `"_time,_measurement,target,value"`. |
| `INFLUX_PIVOT` | `0` | Emit pivoted (wide) CSV — one row per `_time`, one column per `_field`. Default `0` (long format) because for gyanam's single-field schema pivot is a no-op but expensive server-side. Set `1` for multi-field buckets. |

### Schema mapping (set these if you see "0 rows")

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUX_FIELD_NAME` | unset (no filter) | Narrow the export and the pre-flight count to a **single `_field` key** (e.g. `value`, `mean`). **Default is unset** — export and count ALL fields in the bucket so the dry-run works against any bucket out of the box. Set this if you want the count to reflect the pivoted row count on a multi-field bucket, or to limit a wide bucket to one field. (For gyanam-collector buckets, which only write a single `value` field, setting or not setting this is a no-op.) |
| `INFLUX_TARGET_TAG` | `target_name` | Tag key holding the target identifier — used by the `--targets` filter. Matches gyanam's collector, which writes `extra_tags = {"target_name": result.target_name}`. **Older builds used `target`** and silently matched nothing; if you have an old bucket or are exporting a non-gyanam bucket, set this to the correct tag key. |
| `INFLUX_DEBUG_QUERY` | `0` | Set to `1` to print every Flux query the script sends to stderr. **The fastest way to diagnose 0-rows results.** |

### Chunking & timing

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUX_CHUNK_HOURS` | `0` (single window) | Split the range into N-hour windows. Each is a separate HTTP request, bounded by its own timeout, independently retried. **Strongly recommended for any export >1 day.** |
| `INFLUXDB_TIMEOUT_MS` | `600000` (10 min) | Per-HTTP-request timeout in milliseconds. With chunking, this is a per-chunk ceiling. Bump to `1800000` (30 min) for very slow single chunks. |
| `INFLUX_EXPORT_MAX_RETRIES` | `3` | Per-chunk retry budget for transient errors (timeout, connection-reset, server-disconnect). Each retry re-runs the chunk's query from scratch — no duplicate rows on retry. |

### Safety & probing

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUX_MAX_ROWS` | unset | Refuse to start the export if the pre-flight `count()` exceeds N rows. Guards against accidental huge ranges (e.g. typing `-10y` instead of `-10d`). |
| `INFLUX_DRY_RUN` | `0` | Set to `1` to run only the pre-flight count + warmup probe. No CSV is written. **Use before every long export.** |

### Output behaviour

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUX_QUIET` | `0` | Set to `1` to suppress periodic progress lines. The final summary is still printed. |
| `INFLUX_SKIP_WARMUP` | `0` | Set to `1` to skip the DNS / TCP / TLS warmup probe. |

### Connection (rarely changed)

| Variable | Default | Effect |
|----------|---------|--------|
| `INFLUXDB_TOKEN` | (required) | Loaded from `.env` automatically by `gyanam.sh`. |
| `INFLUXDB_ORG` | `prometheus` | Loaded from `.env`. |
| `INFLUXDB_URL` | `http://influxdb:8086` | Resolved inside the export container; rarely needs override. |

## Output file formats

| Extension | Format | Behaviour |
|-----------|--------|-----------|
| `.csv` | Plain CSV | Faster to write/read, 5–10× larger on disk |
| `.csv.gz` | Gzipped CSV | Transparent compression; tools like Excel / pandas / `csv-reader` handle it natively; recommended default |

### CSV column layout (default — long format)

```
_time, _measurement, field, value, host, target, target_name, metric_type, unit, report_type, ...
```

One row per data point. `field` is always `value` for gyanam's schema
(rename of `_field`). `value` is the rename of `_value`.

### CSV column layout (with `INFLUX_PIVOT=1`)

```
_time, _measurement, value, host, target, target_name, metric_type, unit, report_type, ...
```

One row per unique `_time` × tag combination. For gyanam's
single-field schema this is identical to the long format in row count,
but server-side it pays a pivot sort+transpose cost.

## What it prints

The export emits five distinct sections of output, in order.

### 1. Startup banner

```
=== InfluxDB Export ===
  bucket:       gpu_metrics
  time range:   -7d  →  now()
  pivot:        off (long format)
  aggregate:    every 5m via mean()
  chunk hours:  6
  output:       ./30d.csv.gz (gzip)
  retries:      up to 3 per window
```

Lets you sanity-check every parameter before any data movement.

### 2. Warmup probe

```
Warmup (influxdb:8086): DNS 4ms, TCP 1ms, TLS --
```

DNS / TCP / TLS handshake timing. Slow values (DNS > 200ms, TCP >
500ms, TLS > 1s) are flagged explicitly. **The single most useful
signal for diagnosing remote-export slowness** — high values point at
network/DNS issues; low values mean the network is fine and the
slowness (if any) is server-side.

### 3. Pre-flight count

```
Pre-flight count() query running...
  estimated 47,382,910 records (47.38M) — count() took 1.42s, first byte after 0.31s
  estimated output: ~13.32 GB uncompressed (~1.60 GB gzipped)
```

The count itself uses a `keep()` + `group()` + `count()` pipeline that
runs over the TSI index server-side, so it returns even for billions
of points in seconds. First-byte time on the count is a separate
signal — if THAT is slow, the InfluxDB query planner is overloaded.

### 4. Streaming progress

For a single window:

```
  first row arrived after 0.8s (query-plan + initial read)
  ...50,000 rows (50.00K, 12483 rec/s, ETA 1h03m)
  ...100,000 rows (100.00K, 13511 rec/s, ETA 58m22s)
```

For chunked mode:

```
[window 1/28] 2026-05-23T13:00:00Z  →  2026-05-23T19:00:00Z
  first row arrived after 0.4s (query-plan + initial read)
  ...50,000 rows (50.00K, 12340 rec/s, ETA 1h02m)
  window done: 1,692,103 rows in 2m05s (13537 rec/s)
```

Progress lines fire every 50,000 rows OR every 10 seconds, whichever
comes first. ETA uses an exponential moving average over rate so it
reacts to slow-downs (instead of using the all-time average).

### 5. Final summary

```
Concatenating 28 chunk files → ./30d.csv.gz.part...
  concat done in 4.2s

✅ Exported 47,382,910 rows in 58m22s (13534 rec/s avg) → ./30d.csv.gz
   File size: 1.58 GB
```

If actual rows differ from the pre-flight estimate by more than 2%,
you'll see a drift warning (suggests writes happening during export
or query inconsistency).

### 6. Error/interrupt output

On `Ctrl-C`:
```
⚠ Interrupted after 12m05s, 8,432,109 rows written so far.
  Partial output at ./30d.csv.gz.part retained for inspection.
```

The `.part` file is **never** atomically renamed on failure — your
existing `30d.csv.gz` (if any) is untouched.

On transient error (auto-retry):
```
  ⚠ window failed (ReadTimeoutError: ...); retrying in 4.3s (attempt 2/4)
```

On a fatal error (after exhausting retries):
```
❌ Export failed after 14m12s with 4,200,000 rows written: ServerDisconnectedError: ...
```

## Recommended workflows

### A. Probe before any large export

Always do this first. Costs nothing, tells you everything:

```bash
INFLUX_DRY_RUN=1 ./gyanam.sh influx-export gpu_metrics ./out.csv.gz -7d
```

If the estimated row count is reasonable for your purpose, proceed.
If it's 500M rows when you wanted ~5M, adjust filters or add
`INFLUX_AGGREGATE_WINDOW`.

### B. Quick small export (last few hours)

```bash
./gyanam.sh influx-export gpu_metrics ./quick.csv.gz -2h
```

No chunking needed; gzip output keeps the file small.

### C. Multi-day raw export

```bash
INFLUX_CHUNK_HOURS=6 INFLUX_MAX_ROWS=200000000 \
  ./gyanam.sh influx-export gpu_metrics ./7d.csv.gz -7d
```

Chunking gives bounded per-request timeouts + independent retries.
The row ceiling protects you against a typo that pulls a year.

### D. Long-range analytics

```bash
INFLUX_AGGREGATE_WINDOW=5m INFLUX_AGGREGATE_FN=mean \
  ./gyanam.sh influx-export gpu_metrics ./30d_5m.csv.gz -30d
```

Server-side downsampling — typically 60× fewer rows than raw, with
identical statistical fidelity for most analytics purposes.

### E. Narrow column projection

```bash
INFLUX_COLUMNS="_time,_measurement,target,value" \
  ./gyanam.sh influx-export gpu_metrics ./narrow.csv.gz -24h
```

Useful when you only need a few columns for a specific analysis —
both wire transfer and disk size shrink proportionally.

### F. Remote machine, unreliable network

```bash
INFLUX_CHUNK_HOURS=2 INFLUX_EXPORT_MAX_RETRIES=5 INFLUXDB_TIMEOUT_MS=1800000 \
  ./gyanam.sh influx-export gpu_metrics ./remote.csv.gz -7d
```

Smaller chunks, more retries, longer per-chunk timeout. The atomic
write means a final crash never corrupts an existing good file.

### G. Quiet scripted run

```bash
INFLUX_QUIET=1 INFLUX_SKIP_WARMUP=1 \
  ./gyanam.sh influx-export gpu_metrics ./auto.csv.gz -1h \
  >> /var/log/gyanam_export.log 2>&1
```

Suppresses progress; only the startup banner and final summary land
in the log.

---

# Part 2: Native InfluxDB alternatives

The InfluxDB v2 distribution ships several native data-extraction
tools. None of them have the robustness/visibility features of the
`gyanam.sh` wrapper, but each excels in specific scenarios.

## 2.1 `influx query` (CLI)

The standard CLI tool for running Flux queries and getting back CSV.
Already installed in your `influxdb:2.7.11` container at
`/usr/bin/influx`.

### Recipe

```bash
source .env  # for INFLUXDB_TOKEN, INFLUXDB_ORG

# Stream raw CSV to stdout (--raw is critical — without it you get a
# formatted human-readable table)
docker exec -i gyanam_influxdb_1 influx query \
  --host http://localhost:8086 \
  --token "$INFLUXDB_TOKEN" \
  --org "${INFLUXDB_ORG:-prometheus}" \
  --raw \
  'from(bucket:"gpu_metrics") |> range(start:-1h)' \
  > out.csv

# Gzipped, with a filter:
docker exec -i gyanam_influxdb_1 influx query \
  --host http://localhost:8086 \
  --token "$INFLUXDB_TOKEN" \
  --org prometheus \
  --raw \
  'from(bucket:"gpu_metrics")
     |> range(start:-7d)
     |> filter(fn: (r) => r._measurement == "gpu_temp")' \
  | gzip > gpu_temp_7d.csv.gz
```

### What you get

Annotated CSV — first three lines are header annotations:

```
#group,false,false,true,true,false,false,true,true,true
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string,string,string
#default,_result,,,,,,,,
,result,table,_start,_stop,_time,_value,_field,_measurement,target
,,0,2026-05-23T13:00:00Z,2026-05-30T13:00:00Z,2026-05-30T12:59:55Z,67.5,value,gpu_temp,smci355-...
```

pandas can ingest it with `comment="#"`; Excel users will need to
strip the annotation rows manually.

### When to use

- Local InfluxDB (no network concerns)
- Small to medium export (< 10M rows)
- You're already comfortable writing Flux
- You want the absolute fastest path for a known-good query

### Limitations

- No progress, no ETA
- No retry on transient errors
- No chunking → one HTTP request → vulnerable to the same 10-min
  timeout problem
- No pre-flight count
- Annotated CSV format is awkward for naïve consumers (Excel, etc.)
- No safety ceiling — a typo can saturate the server

## 2.2 `curl` `/api/v2/query`

The lowest-dependency option: any shell with `curl` can pull data.

### Recipe

```bash
source .env
INFLUX_URL="http://localhost:${INFLUXDB_PORT:-8086}"

# Direct gzipped output, plain CSV in the file:
curl --compressed -sS -X POST \
  "${INFLUX_URL}/api/v2/query?org=${INFLUXDB_ORG:-prometheus}" \
  -H "Authorization: Token ${INFLUXDB_TOKEN}" \
  -H "Accept: application/csv" \
  -H "Content-Type: application/vnd.flux" \
  --max-time 1800 \
  -d 'from(bucket:"gpu_metrics") |> range(start:-1h)' \
  > out.csv

# Keep wire-level gzip AND store it gzipped on disk
# (drop --compressed so curl doesn't auto-decompress)
curl -sS -X POST \
  "${INFLUX_URL}/api/v2/query?org=${INFLUXDB_ORG:-prometheus}" \
  -H "Authorization: Token ${INFLUXDB_TOKEN}" \
  -H "Accept: application/csv" \
  -H "Accept-Encoding: gzip" \
  -H "Content-Type: application/vnd.flux" \
  --max-time 1800 \
  -d 'from(bucket:"gpu_metrics") |> range(start:-1h)' \
  > out.csv.gz
```

### When to use

- You need to embed an export in another shell script
- Minimal dependencies (no docker, no python)
- You want absolute header-level control (custom auth, etc.)
- One-off pipelines: `curl ... | jq ... | psql ...`

### Limitations

Same as `influx query` — no progress, no retry, no chunking,
annotated CSV. Plus you handle Flux query construction by hand.

## 2.3 `influx backup`

**Not a CSV path.** Produces a manifest + binary TSM files designed
for restore into another InfluxDB instance. By far the fastest and
smallest way to extract a bucket — typically 5–10× smaller than
gzipped CSV because TSM is the on-disk format already optimized for
time-series compression.

### Recipe

```bash
# Back up a single bucket
mkdir -p ./backups
docker exec gyanam_influxdb_1 influx backup /tmp/backup \
  --bucket gpu_metrics \
  --org "${INFLUXDB_ORG:-prometheus}" \
  --token "$INFLUXDB_TOKEN"

# Copy out of the container
docker cp gyanam_influxdb_1:/tmp/backup ./backups/snapshot_$(date +%Y%m%d)
docker exec gyanam_influxdb_1 rm -rf /tmp/backup

# Backup ALL buckets (omit --bucket)
docker exec gyanam_influxdb_1 influx backup /tmp/full_backup \
  --org "${INFLUXDB_ORG:-prometheus}" \
  --token "$INFLUXDB_TOKEN"
```

### Restore

```bash
docker cp ./backups/snapshot_20260530 gyanam_influxdb_1:/tmp/restore
docker exec gyanam_influxdb_1 influx restore /tmp/restore \
  --token "$INFLUXDB_TOKEN"
```

### When to use

- Periodic backups for disaster recovery
- Migrating data to another InfluxDB instance
- Reproducing a dataset for testing (full fidelity)
- You want smallest possible export size

### When **not** to use

- Anything you want to open in Excel, pandas, R, or any non-InfluxDB tool
- One-off analysis exports

### Limitations

- Output is binary; not human-readable
- Cannot be selectively queried after extraction (it's a snapshot of
  the engine's storage files, not a query result)
- Bucket-granularity only; cannot back up "just measurement X over
  range Y"

## 2.4 `influxd inspect export-tsm`

Lowest-level export, emits InfluxDB **line protocol** — the same
format `influx write` consumes. Useful for migrating subsets of data
between instances or for diagnostic dumps.

### Recipe

```bash
# Look up the bucket id (the inspect tool needs ID, not name)
docker exec gyanam_influxdb_1 influx bucket list \
  --token "$INFLUXDB_TOKEN" --org prometheus

# Export as line protocol for a date range
docker exec gyanam_influxdb_1 influxd inspect export-tsm \
  --bucket-id <id-from-above> \
  --start 2026-05-23T00:00:00Z \
  --end 2026-05-30T00:00:00Z \
  --output /tmp/export.lp

# Copy out
docker cp gyanam_influxdb_1:/tmp/export.lp ./
```

### When to use

- Re-ingesting data into another InfluxDB via `influx write`
- Debugging a specific time range at the line-protocol level
- Round-trip testing of write pipelines

### Limitations

- Line protocol is not CSV; tools must understand it
- Bucket-id rather than name (extra lookup step)
- Output is larger than gzipped CSV (~30–50% more)
- No selectivity beyond range (no field/tag filters)

---

# Part 3: Side-by-side comparison

| Aspect | `gyanam.sh influx-export` | `influx query` | `curl /query` | `influx backup` | `inspect export-tsm` |
|---|---|---|---|---|---|
| **Output format** | CSV / .csv.gz (clean schema) | Annotated CSV | Annotated CSV | TSM (binary) | Line protocol |
| **Use case** | Analysis, sharing | Quick ad-hoc | Pipelines | Migration / DR | Re-ingest |
| **Streaming** | yes | yes | yes | n/a (file copy) | n/a |
| **Pre-flight count + ETA** | yes | no | no | no | no |
| **Per-chunk retry** | yes | no | no | n/a | no |
| **Atomic write** | yes | no | no | yes | no |
| **Auto-chunking by time** | yes (`INFLUX_CHUNK_HOURS`) | no | no | n/a | no |
| **Server-side aggregation** | yes (`INFLUX_AGGREGATE_WINDOW`) | yes (in Flux you write) | yes (in Flux you write) | n/a | no |
| **HTTP gzip** | yes (auto) | yes | manual | n/a | n/a |
| **Auto on-disk gzip** | yes (`.csv.gz`) | manual (`\| gzip`) | manual | already compressed | manual |
| **Raw speed (small export)** | medium | **fast** | **fastest** | **fastest** | **fastest** |
| **Raw speed (big export)** | fast (gzip + no-pivot + chunks dominate) | medium | medium | **fastest** | medium |
| **Output usability** | Long-format CSV → pandas/Excel | Annotation rows; needs pre-process | Same | Cannot read as CSV | Cannot read as CSV |
| **Dependencies** | Docker (Python container pulled on first use) | `influx` CLI (in your container) | `curl` | `influx` CLI | `influxd` (in your container) |
| **Progress visibility** | Full (rows/s, ETA, first-byte) | None | None | Minimal | None |
| **Safety ceiling** | `INFLUX_MAX_ROWS` | None | `--max-time` only | n/a | None |
| **Filter capabilities** | bucket/measurement/targets/columns; arbitrary Flux via `--query`-style customisation in script | Arbitrary Flux | Arbitrary Flux | Bucket only | Date range only |
| **Selective export** | yes | yes | yes | no (full bucket) | no (full bucket date range) |

---

# Decision Guide

```
                ┌── Just need quick CSV from local InfluxDB ────→  influx query --raw
                │
                ├── Shell pipeline, no docker/python wanted ─────→  curl /api/v2/query
                │
                ├── Spreadsheet / pandas / R analysis ───────────┐
                │                                                │
   I need data ─┤   Remote machine, possibly unreliable link  ───┼─→  gyanam.sh influx-export
                │   Large export (>1M rows)                      │
                │   Want progress / safety / retry               │
                │   Want easy long-format CSV                    ┘
                │
                ├── Migrating to another InfluxDB ───────────────→  influx backup
                │
                └── Re-ingesting subset via line protocol ───────→  influxd inspect export-tsm
```

## Specifically for the "remote machine timeout" symptom

The native `influx query` and `curl` paths have the same failure
mode: one HTTP request for the whole range, no chunking, no retry, no
visibility into where time is being spent. The wrapper was built
exactly to address this — every layer (warmup → pre-flight count →
chunking → per-chunk retry → progress) makes a previously-mysterious
timeout into a diagnosable, recoverable event.

---

# Performance Notes

Approximate impact of each `gyanam.sh influx-export` lever, measured
on a typical gyanam workload (250 targets × ~2000 metrics × 5s
cadence):

| Lever | Improvement | Notes |
|-------|-------------|-------|
| `enable_gzip` (always on) | 5–10× wire bandwidth | Largest single network win for CSV |
| `.csv.gz` output | 5–10× disk size | Transparent; readers don't notice |
| No-pivot default (`INFLUX_PIVOT=0`) | 2–5× server-side query speed | Pivot is one of Flux's most expensive operators; no-op for gyanam |
| `INFLUX_AGGREGATE_WINDOW=5m` | ~60× fewer rows for analytics | Server-side downsample; identical statistical fidelity for most analyses |
| `INFLUX_AGGREGATE_WINDOW=1h` | ~720× fewer rows | Even more aggressive |
| `INFLUX_COLUMNS=...` | 30–50% fewer bytes per row | When you can narrow |
| `INFLUX_CHUNK_HOURS=6` | Per-chunk timeout budget | Doesn't speed things up, but makes a 4-hour export survivable |
| `INFLUX_EXPORT_MAX_RETRIES=3` | Automatic recovery from transient errors | No throughput change; resilience only |

### Order-of-magnitude estimate for a 7-day raw export

| Configuration | Approx rows | Approx file size |
|---------------|-------------|------------------|
| Raw, pivoted, plain CSV (worst) | ~500M | ~150 GB |
| Raw, no-pivot, .csv.gz | ~500M | ~15 GB |
| 5-min aggregate, no-pivot, .csv.gz | ~8M | ~250 MB |
| 1-hour aggregate, narrow columns, .csv.gz | ~670K | ~10 MB |

For analytics, the **aggregate-window** lever alone usually does more
than every other optimisation combined.

---

# Troubleshooting Cookbook

### Symptom: `HTTPConnectionPool ReadTimeout: ... 10 seconds`

**Cause**: pre-fix script used the influxdb-client library's default
10s timeout.
**Fix**: already in place — `INFLUXDB_TIMEOUT_MS=600000` (10 min)
is the new default. Override higher for slow chunks.

### Symptom: Dry-run / Export shows "0 rows" and "0 bytes"

**Most likely causes, in order of frequency**:

1. **`--targets` was passed but the tag key is wrong.** gyanam's
   collector writes the target identifier to the **`target_name`**
   tag (not `target`). Older builds and some non-gyanam buckets used
   `target`. Set `INFLUX_TARGET_TAG=target` (or whatever the bucket
   actually uses) and re-run. The automatic diagnostic will print the
   actual tag keys present in the bucket.
2. **The bucket really has no data in the requested range.** Verify
   with `./gyanam.sh influx-status` (retention) and
   `./gyanam.sh influx-list <bucket>` (measurements).
3. **You explicitly set `--field-name` and it doesn't exist in this
   bucket.** Since the **default is now "no field filter"** (count
   and export every field), this only happens when you've overridden
   `--field-name` / `INFLUX_FIELD_NAME`. Drop the override or set it
   to a key the diagnostic confirms is present.

**What the script does for you**: when count returns 0, the script
now **automatically runs a diagnostic probe** of the bucket and
prints distinct measurements, field keys, tag keys, and a sample of
target-tag values in the requested range. The output tells you
exactly which filter is over-restrictive and what to set it to.

**To see the raw Flux queries** being sent (useful when the
diagnostic output doesn't pin the cause), set
`INFLUX_DEBUG_QUERY=1`. The queries are written to stderr before
they execute.

### Symptom: Export hits `INFLUX_MAX_ROWS` ceiling

**That's the feature working.** Means you're about to pull more rows
than expected. Either:
- Add `INFLUX_AGGREGATE_WINDOW=5m` (or coarser)
- Narrow `--measurement` or `--targets`
- Shorten the time range
- Raise `INFLUX_MAX_ROWS` if the row count is actually correct

### Symptom: Chunks fail and retry repeatedly with `ServerDisconnectedError`

**Cause**: InfluxDB CPU saturated; the server is killing slow
connections.
**Diagnose**:
1. Check InfluxDB container CPU: `docker stats gyanam_influxdb_1`.
2. Look at first-byte time in the progress output — if it's >30s,
   the query planner is overloaded.
**Fix**:
- Smaller chunks (`INFLUX_CHUNK_HOURS=1`)
- Use `INFLUX_AGGREGATE_WINDOW` so the server returns fewer rows
- Raise InfluxDB CPU limit in `docker-compose.yml`

### Symptom: Export finishes but `Row count drift` warning fires

**Cause**: data was being written to the bucket during the export, OR
InfluxDB compaction merged points mid-query.
**Diagnose**: drift up to 2% is tolerated silently; >2% is flagged.
For a live system this is usually expected.
**Fix**: nothing required unless drift is large (>10%). If reproducible
and large, file an InfluxDB bug.

### Symptom: Warmup probe shows `DNS 850ms`

**Cause**: resolver issue inside the container.
**Fix**: check `/etc/resolv.conf` in the container, or use IP
literal instead of hostname in `INFLUXDB_URL`.

### Symptom: Warmup shows `TCP FAIL`

**Cause**: collector container can't reach InfluxDB at all.
**Fix**: ensure both are on the same Docker network (the wrapper does
this via `--network gyanam_monitoring`). If running the script
outside the docker-compose stack, point `INFLUXDB_URL` at a
host-reachable address.

### Symptom: `.csv.gz.part` file left after a crash

That's the atomic-write feature: the partial file is **not** renamed
to the final path, so your existing good file (if any) is unchanged.
The `.part` file is kept for forensic inspection. Delete it manually
or it'll be overwritten on the next run.

### Symptom: First-byte time is fine, but throughput is slow

**Cause**: lots of data but the network is the bottleneck.
**Fix**:
- Verify `enable_gzip=True` is in effect (it is by default; check the
  startup banner reports `gzip=on`)
- Use `.csv.gz` output (the wire bytes are already gzip; disk write
  doesn't add cost)
- Use `INFLUX_COLUMNS` to drop unneeded columns
- Use `INFLUX_AGGREGATE_WINDOW` to reduce row count

### Symptom: Export script needs to be run from a CI job

```bash
# Quiet mode, log everything, set a hard ceiling
INFLUX_QUIET=1 \
INFLUX_MAX_ROWS=100000000 \
INFLUX_CHUNK_HOURS=2 \
INFLUX_EXPORT_MAX_RETRIES=5 \
  ./gyanam.sh influx-export gpu_metrics ./ci_export.csv.gz -24h \
  2>&1 | tee export.log

# Exit code reflects success/failure for CI
echo "exit: $?"
```

The script's exit code distinguishes:
- `0` — success
- `1` — generic failure (export error, after retries)
- `2` — bucket does not exist
- `3` — pre-flight count exceeded `INFLUX_MAX_ROWS`
- `130` — interrupted (Ctrl-C / SIGINT)

---

## Related documentation

- [`SCALABILITY.md`](./SCALABILITY.md) — overall scaling notes
- [`DEPLOYMENT.md`](./DEPLOYMENT.md) — full deployment guide
- `./gyanam.sh help` — quick reference in the terminal
- `python /scripts/export_influxdb_data.py --help` — every script-level flag
