# AGENTS.md - Gyanam Project Reference

## Project Overview

**GYANAM — GPU Observability Assistant**. The project's primary goal is
to support **large-scale GPU observability and any debug effort** on
AMD Instinct fleets. It gathers telemetry from GPU servers through DMTF
Redfish-defined interfaces — Redfish Aggregation on ODM/OEM BMCs that
support it, or proxy-based collection otherwise (with SSH-proxy and SSE
alternatives) — parses telemetry through a
schema-aware extraction pipeline, stores metrics in InfluxDB
(Prometheus also supported), surfaces them via Grafana, and adds
on-demand diagnostic-log collection + alert subscriptions + a CSV
export pipeline for debug-evidence sharing. The codebase / repo
short-name remains `gyanam`.

- **Stack**: Python 3.11 / FastAPI / SQLAlchemy (async) / InfluxDB 2.7 / PostgreSQL 16 / Grafana 13.0.1
- **Deployment**: **5 Docker containers** — `api`, `collector`, `influxdb`, `postgres`, `grafana`
  - `api` runs the Web UI / REST surface (FastAPI, port 8080)
  - `collector` runs the background pipeline (poller + exporter + alert/SSE managers, internal port 8081)
  - **Dual data store**: `api` + `collector` share the SQLite file (targets, collected logs) via
    the `shared_data` volume; **alerts live in PostgreSQL** (`postgres` service, high-volume
    time-series). Configured via `ALERTS_DATABASE_URL` (**required** — no SQLite fallback).
    The `TargetRepository` holds two engines and routes alert methods to the alert engine.
- **Current scale**: tested at 247 targets; configured defaults target 250-500 nodes (2000-4000 GPUs)

## Quick Start

```bash
./gyanam.sh init           # one-time, generates .env
./gyanam.sh start          # boots all four containers
# UI:       http://localhost:8080  (admin/changeme)
# Grafana:  http://localhost:3000
# InfluxDB: http://localhost:8086
```

## Architecture

### Data Flow

```
                              ┌──── Per-target persistent RedfishClient cache
                              │     (one httpx.AsyncClient + session per target,
                              │      evicted on failure or target removal)
                              ▼
Redfish BMC ── RedfishPoller._schedule_due_polls (fire-and-forget)
                  │
                  ├── PollResult (single-field, raw blob bytes dropped on GET fast-path)
                  │
                  ▼
              asyncio.Queue (maxsize=1000)
                  │
                  ▼
  result_processor_task  (max_concurrent_processors=16 via semaphore)
                  │
                  ▼
  asyncio.to_thread(_sync_extract_metrics)  ── runs on dedicated
                                                ThreadPoolExecutor
                                                (max(16, processors*2))
                  │
                  ├── BlobUnpacker (only on task-based fallback path)
                  ├── MetricExtractor (33 JSONPath schemas)
                  └── MetricDiscovery (auto-discover numeric fields)
                  │
                  ▼
              InfluxDBExporter
                  │
                  ├── asyncio.Lock-protected buffer (cap = batch_size × 200)
                  ├── _flush_event signal → flush_loop owns ALL writes
                  ├── max_concurrent_writes=10 parallel HTTP batches (gzipped)
                  └── consecutive-failure reconnect (≥3 → force _write_api=None)
                  │
                  ▼
              InfluxDB → Grafana dashboards

Parallel SQL writer:
  Each collection completion appends to RedfishPoller._pending_status; a separate
  _status_writer_loop drains the dict every 5s into ONE SQL transaction —
  avoids "database is locked" errors at high concurrency.
```

### Key Design Patterns

- **Two-process split**: `api_main.py` and `collector_main.py` are independent
  asyncio applications sharing a single SQLite file (WAL mode). Cross-service
  communication is over HTTP on the internal Docker network
  (`http://collector:8081/...`).
- **Shared state via `api/dependencies.py`**: holds `app_state` dict and
  getter functions; avoids circular imports between the FastAPI app factory
  and route modules.
- **Lazy imports**: `targets.py:trigger_poll()` lazy-imports `process_poll_result`
  from `collector_main.py`.
- **Config merging**: YAML defaults (`config/config.yaml`) overridden by env
  vars via pydantic `Settings` class in `load_config()`. InfluxDB token comes
  ONLY from environment.
- **Two collection paths**: GET-first (parallel GETs to 6 metric report endpoints)
  with task-based fallback (POST → await task → download blob).
- **Per-target persistent `RedfishClient` cache**: each target keeps its
  httpx connection + Redfish session alive across collection cycles. Evicted on failure
  or target removal. Saves ~247 TCP+TLS handshakes per cycle at scale.
- **Fire-and-forget collection scheduling**: `_schedule_due_polls` dispatches tasks
  and returns immediately. Each target's `_next_poll_time` is advanced
  *before* the task fires, so a slow cycle cannot stack a thundering-herd
  backlog. Concurrency is bounded by `_semaphore`.
- **Batched SQLite status writer**: collection completions update an in-memory dict;
  a 5-second loop flushes all pending updates in one transaction.
- **InfluxDB reconnect-on-failure**: after 3 consecutive full-flush failures
  the cached write API is nulled, triggering the flush loop's reconnect path.
  Prevents the "connected but every write fails" silent stall.
- **Circuit breaker**: 5 consecutive failures → 6× collection interval backoff per target.

## Directory Structure

```
collector/
  config/
    config.yaml              # Main configuration (YAML defaults)
    metrics_schema.yaml      # 33 metric extraction schemas + auto-discovery
  src/
    api_main.py              # FastAPI app factory + lifespan for the API service
    collector_main.py        # asyncio entry-point for the collector service
                             # (lifespan, result_processor_task, process_poll_result,
                             #  health server, webhook receiver)
    alert_manager.py         # Coordinates SSE/webhook alert subscriptions
                             # + baseline log-entry pull (once on start + periodic re-pull)
    log_collector.py         # On-demand diagnostic log bundle collection
    config.py                # Pydantic config models, load_config()
    api/
      auth.py                # HMAC session cookies, bcrypt passwords, Basic Auth
      csrf.py                # CSRF token generation/validation
      dependencies.py        # Shared app_state dict, getter functions
      routes/
        targets.py           # Target CRUD, bulk import, test-connection
        logs.py              # Collected-log download/delete
        alerts.py            # Alert browsing, manager-stats proxy
        schemas.py           # Schema viewer
        health.py            # /health, /health/detailed, /ready
      templates/             # Jinja2 HTML templates
    database/
      models.py              # SQLAlchemy models (Target, CollectedLog, Alert)
      repository.py          # Async CRUD, WAL mode, update_poll_status_batch
    exporters/
      base.py                # Metric / BaseExporter abstractions
      influxdb.py            # Buffered async writes, reconnect-on-failure,
                             # _flush_event signaling, gzipped HTTP
      prometheus.py          # Alternative Prometheus backend
    parser/
      extractor.py           # Schema-based metric extraction (JSONPath)
      discovery.py           # Auto-discovery of numeric fields
      schema.py              # Schema loader
      redfish_log_parser.py  # redfish-tree.log parser
      unpacker.py            # Blob extraction (tar/gz/zip) for task path
    redfish/
      client.py              # RedfishClient (httpx async, session auth)
      poller.py              # RedfishPoller (scheduling, semaphore,
                             # _next_poll_time, _inflight, _clients cache,
                             # _pending_status, _status_writer_loop)
      sse_subscriber.py      # SSE manager (persistent SSE connections)
      sse_capability_check.py# SSE-capability probe
      alert_subscriber.py    # Per-target SSE alert stream (+ shared severity/event
                             # normalization helpers used by the webhook path)
      log_baseline.py        # One-shot baseline pull of existing LogService entries
      webhook_subscriber.py  # Per-target webhook subscription mgmt
      ssh_transport.py       # SSH-proxy transport for air-gapped BMCs
grafana/
  provisioning/
    dashboards/              # 11 dashboard JSON files (auto-provisioned)
      fleet/                 # 3 fleet-wide views
      per-system/            # 6 per-system diagnostic dashboards
      historical/            # 2 long-term-trend dashboards
    datasources/             # InfluxDB datasource (must have explicit uid)
scripts/
  export_influxdb_data.py    # Bucket-status / list-measurements / CSV export
  run-codeql.sh              # Reproducible local CodeQL run
  monitor_volumes.sh         # Disk + volume size check
  log_volume_growth.sh       # Hourly volume-growth CSV log
  alert_disk_space.sh        # Threshold-based disk-space alert
  test_bmc_alerts.py         # Interactive BMC SSE/webhook probe (test only)
docs/
  CODEQL_REPORT.md           # Current CodeQL state + accepted-risk audit
  DATA_EXPORT_REFERENCE.md   # gyanam.sh export + native InfluxDB recipes
  DEPLOYMENT.md              # Ubuntu deployment guide
  SCALABILITY.md             # Sizing / tuning per fleet size
```

## Critical Implementation Notes

### JSONPath Regex Syntax

**jsonpath_ng.ext** does NOT support `/regex/flags` syntax. The `/` is
tokenised as `SORT_DIRECTION` by the lexer. Always use quoted strings:

```yaml
# WRONG (silently matches nothing):
path_pattern: "$.MetricValues[?(@.MetricProperty =~ /.*GPU_TEMP.*/i)]"
# CORRECT:
path_pattern: '$.MetricValues[?(@.MetricProperty =~ "(?i).*GPU_TEMP.*")]'
```

Use `jsonpath_ng.ext.parse` (not basic `jsonpath_ng.parse`) for regex filter
support.

### Grafana Flux Variable Queries

Flux `distinct()` operates per-table. Without `|> group()` before
`|> distinct()`, results stay in separate tables per InfluxDB series key.
Always use:

```flux
|> keep(columns: ["target_name"]) |> group() |> distinct(column: "target_name")
```

### Grafana File Provisioning

- Datasource YAML needs explicit `uid:` field
- Dashboard JSON `${DS_INFLUXDB}` is an export placeholder, NOT resolved
  during file provisioning
- Use literal datasource uid in dashboard JSON instead

### Python Type Gotcha

`bool` is a subclass of `int`. Always check `isinstance(value, bool)` BEFORE
`isinstance(value, (int, float))` when they need different handling
(see `extractor.py` numeric conversion).

### Redfish Alert Severity / EventType Drift

Redfish `Event` v1.7+ **deprecated `EventType`** and **deprecated `Severity`
in favour of `MessageSeverity`**. Modern BMCs may send `MessageSeverity` and
omit `EventType`. Alert parsing must read both spellings and must NOT drop an
event merely because a field is absent — see `normalize_severity()` /
`severity_allowed()` in `alert_subscriber.py` (shared by the SSE and webhook
paths). Reading only `Severity`/`EventType` silently discards every event on
newer BMCs (connection shows "connected", zero alerts stored).

### Alert Baseline Pull + Dedup

SSE streams and webhook subscriptions only capture events emitted *after* the
subscription exists, so standing conditions never appear on their own.
`log_baseline.pull_baseline_alerts()` GETs existing LogService entries
(Systems + Managers) once on subscription start and on a periodic re-pull
(`alerts.baseline_repull_interval_minutes`, 0 disables). Baseline alerts
bypass the per-target rate limiter (`_on_baseline_alert`) so a large baseline
isn't throttled. Idempotency is enforced by `Alert.dedup_key` (unique index):
baseline entries key off the LogEntry's stable `@odata.id` (`AlertEvent.source_id`)
so re-pulls always dedup; live events key off target + message_id + timestamp
(+ message). `create_alerts_batch` filters out already-present keys then inserts
the remainder (portable across SQLite/PostgreSQL/MySQL — no dialect-specific
upsert; safe because all alert writes funnel through the single batch-processor
consumer) and returns the count actually inserted. All alert datetimes are
stored **naive UTC** (`_to_naive_utc`) so SQLite string range comparisons on
`received_at` stay consistent; the JSON API re-tags them as UTC (`_iso_utc`).

### os.path.expandvars Limitation

Does NOT support bash `${VAR:-default}` syntax. The `:-default` suffix
prevents expansion even when the var IS set. Use plain `${VAR}`.

### Per-HTTP-request timeouts

The `influxdb-client` library default is 10s — far too short for any
non-trivial export. The export script overrides via `INFLUXDB_TIMEOUT_MS`
(default 600000 = 10 min). See `docs/DATA_EXPORT_REFERENCE.md`.

## Configuration Parameters (matches current config.yaml)

### Telemetry Collection (`config.yaml → polling`)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `interval_seconds` | 300 | Per-target overridable |
| `timeout_seconds` | 45 | HTTP request timeout |
| `max_concurrent` | **100** | Semaphore limit; sized for 250-500 targets |
| `task_poll_interval` | 5 | Seconds between task status checks |
| `task_timeout` | **600** | Max wait for task completion |
| `download_timeout` | **600** | Blob download timeout |
| `error_retry_interval` | 10 | Sleep on collection-loop exception |

### Parser (`config.yaml → parser`)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `max_recursion_depth` | 50 | JSON traversal cap |
| `max_concurrent_processors` | **16** | Result-processor semaphore |

The result_processor uses a dedicated `ThreadPoolExecutor` sized at
`max(16, max_concurrent_processors * 2)` — set in
`collector_main.run_collector()` so it isn't bounded by Python's
default `min(32, cpu_count+4)` heuristic (which under cgroup `cpus=N`
can shrink to ~5).

### InfluxDB Exporter (`config.yaml → influxdb`)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `batch_size` | **5000** | Points per flush (lowered from 10000 to avoid mid-stream "Can not write request body" errors) |
| `flush_interval_seconds` | 10 | Wake the flush loop at this cadence (or earlier on `_flush_event`) |
| `write_timeout_ms` | 90000 | Per-write HTTP timeout |
| `max_concurrent_writes` | **10** | Parallel batch writes |
| Internal: `max_buffer_size` | **batch_size × 200 = 1,000,000** | Absorbs InfluxDB outages |
| Internal: reconnect threshold | **3 consecutive full failures** | Forces `_write_api=None` to enter reconnect path |
| Internal: reconnect_delay | 10s → 300s exponential | Backoff between reconnect attempts |
| HTTP gzip | **always on** | 5-10× wire reduction for CSV-like Flux output |

### Resource Lifecycle

- **Per-target RedfishClient cache**: `RedfishPoller._clients[target_id]`
  holds long-lived httpx.AsyncClient + Redfish session. Evicted on
  collection failure or target removal. (Was per-cycle create-and-destroy.)
- **Blob extraction**: Temp dir `/tmp/telemetry/{target}_{uuid}/`, cleaned
  immediately or by hourly cleanup task.
- **JSON parsing**: Full `json.load()` into memory (max 50MB per file,
  500MB total decompressed).
- **InfluxDB buffer**: In-memory only, drops oldest points when full
  during outage. Drop counter exposed via `get_health_metrics()`.

## Current Performance Posture (post-scale-fix)

Eight scale bottlenecks identified in earlier reviews have all been
addressed. Replacement notes for each:

| Original bottleneck | Now |
|---|---|
| `max_concurrent=10` → 5× over cycle at 500 targets | `max_concurrent=100`; fire-and-forget scheduling means slow cycles cannot stack backlog |
| Sequential result processing | `max_concurrent_processors=16` + dedicated `ThreadPoolExecutor(max_workers=max(16, processors*2))` |
| Default thread pool starves under cgroup CPU limit | Explicit `set_default_executor` at startup |
| Result queue silent drops | Drop counter exposed via `_polls_dropped_queue_full` in `poller.get_stats()` |
| InfluxDB single write API | `max_concurrent_writes=10` parallel batches |
| InfluxDB buffer 50K | `batch_size × 200 = 1,000,000` points |
| SQLite single-writer contention at 100 concurrent commits | `_pending_status` dict + 5-second batched `update_poll_status_batch` |
| New httpx client per collection cycle (TCP+TLS handshake × N targets per cycle) | Per-target persistent `RedfishClient` cache with eviction-on-failure |

### Health/observability knobs introduced

- `/health/detailed` returns `poller.get_stats()` and
  `exporter.get_health_metrics()`. New fields include
  `cached_clients`, `client_cache_hit_rate_pct`, `inflight`,
  `polls_dropped_queue_full`, `consecutive_batch_failures`, `reconnects`.
- `is_connected` in the exporter health check now requires *both* a live
  `write_api` *and* a recent successful write within `_PIPELINE_DEAD_AFTER_S`
  (default 600s). The old check stayed `true` for 7 hours during a silent
  hang — that's fixed.

## Security Status

**Implemented**:

- SSRF prevention with loopback blocking
- CSRF tokens with HMAC + expiry
- bcrypt password hashing with timing-safe comparison
- Fernet encryption for stored credentials
- Path traversal protection in log operations
- SQLAlchemy ORM (no SQL injection)
- HTTP-response exception-text sanitisation (CodeQL pass: stack traces
  no longer leak to API responses — see `docs/CODEQL_REPORT.md`)
- TLS 1.2 minimum enforced on the export-script warmup probe
- Webhook URL validation at alert-manager startup (refuses loopback URLs
  unless `GYANAM_ALLOW_LOOPBACK_WEBHOOK=1`)

**Accepted risk** (documented in `docs/CODEQL_REPORT.md`):

- BMCs ship with self-signed certs by default; per-target `verify_ssl`
  flag is the actual control point.
- Bulk-target-import CSV: first line of validation exception (200-char
  cap, printable-chars-only) is surfaced to the operator — needed for
  CSV debugging.

**Missing for production**:

- No HTTPS/TLS termination (all HTTP)
- No security headers (X-Frame-Options, CSP, etc.)
- No rate limiting on any endpoint
- No RBAC (single admin account)
- No audit logging
- Default "changeme" password (warns but doesn't refuse)
- No secrets rotation mechanism
- Docker network not segmented

## Testing

A pytest suite lives under `collector/tests/` covering the parser
(extractor/discovery/schema/log-parser/unpacker), config, auth/CSRF, exporters
(Prometheus + InfluxDB buffering), the Redfish client helpers, the alert
subsystem (repository dedup/ordering/window/counts/pagination/cursors + SSE
subscriber parsing/backoff), input validators, log-collector path-traversal
defense, and the FastAPI routes (auth enforcement, CSRF, alerts, targets CRUD +
bulk CSV import, health). Tests run against **SQLite** with HTTP mocked — no
PostgreSQL / live InfluxDB / BMC required (`asyncio_mode=auto`).

Canonical run (in the collector image, with coverage + `--cov-fail-under=35`):

```bash
./scripts/run-tests.sh                 # full suite + coverage
./scripts/run-tests.sh -k dedup        # filter
```

Local run: `cd collector && pip install -r requirements.txt -r requirements-dev.txt && PYTHONPATH=. python -m pytest`.
Dev-only test deps are in `collector/requirements-dev.txt`. **~56% coverage**
across 220+ tests; the gate is `--cov-fail-under=50`. See **`TESTING.md`** for
layout, conventions, and coverage notes. **Lower coverage / future work:** the
async orchestration loops (`poller` scheduling, `sse_subscriber`,
`ssh_transport`, `alert_subscriber` reconnect, `influxdb` flush/reconnect,
`collector_main`/`api_main` lifespans).

## CodeQL

Single-command local re-run:

```bash
./scripts/run-codeql.sh
```

See `docs/CODEQL_REPORT.md` for the current accepted-risk findings (3,
all documented) and the per-rule fix log.

## Diagrams

Both the architecture and class diagrams live in `docs/` as Mermaid
source (`.mmd`) — that's the source of truth. The `.pdf` files are
renders for GitHub preview and are regenerated separately:

```bash
# Transient docker — no host install needed
docker run --rm -v "$(pwd)/docs:/data" minlag/mermaid-cli:latest \
  -i /data/architecture.mmd -o /data/architecture.pdf
docker run --rm -v "$(pwd)/docs:/data" minlag/mermaid-cli:latest \
  -i /data/class-diagram.mmd -o /data/class-diagram.pdf
```

When you edit a `.mmd`, regenerate the `.pdf` and commit both.

## See Also

- `docs/architecture.mmd` (+ `.pdf`) — runtime data flow + 4-container layout
- `docs/class-diagram.mmd` (+ `.pdf`) — class relationships across layers
- `docs/SCALABILITY.md` — sizing / tuning per fleet size
- `docs/DEPLOYMENT.md` — Ubuntu deployment guide
- `docs/DATA_EXPORT_REFERENCE.md` — CSV export pipeline + native InfluxDB alternatives
- `docs/CODEQL_REPORT.md` — security-scan state and accepted risk
- `LINTING.md` — pre-commit / ruff / mypy setup
- `collector/config/metrics_schema.yaml` — all 33 metric schemas
- `reference_artifacts/` — sample Redfish data for testing schemas
