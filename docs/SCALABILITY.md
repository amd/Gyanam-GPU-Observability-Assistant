# Scalability

This document describes how GYANAM (the GPU Observability Assistant)
scales — what knobs to tune for which fleet size, where the real
bottlenecks are, and what to watch on the health endpoint.

## Architecture Overview

GYANAM collects GPU metrics through three collection modes, processes
them in parallel, and stores them in InfluxDB. The architecture is
split across two long-lived containers (`api` and `collector`) that
share a SQLite file.

```
Targets (BMCs)
     │
     ├── Direct GET (collect every 5m, persistent client cache) ──┐
     └── SSE Subscription (real-time stream)                  ─┘
                                  │
                                  ▼
                 RedfishPoller._schedule_due_polls   (fire-and-forget tasks)
                                  │
                  per-target Semaphore (max_concurrent=100)
                                  │
                                  ▼
                        asyncio.Queue (maxsize=1000)
                                  │
              ┌───────────────────┴───────────────────┐
              │                                       │
              ▼                                       ▼
   result_processor_task                _status_writer_loop (5s tick)
   semaphore: max_concurrent_processors=16          │
              │                                     │
              ▼                                     ▼
   asyncio.to_thread(_sync_extract_metrics)    Batched UPDATE on Target rows
        ┌─────────────┐                        (one transaction per tick,
        │ Dedicated   │                         avoids "database is locked")
        │ ThreadPool  │
        │ max_workers=                        SQLite (WAL mode, shared file)
        │  max(16, processors*2)
        └─────────────┘
              │
              ▼
        InfluxDBExporter
        ┌───────────────────────────────────────────────────┐
        │ asyncio.Lock-protected buffer  (cap batch×200=1M) │
        │ _flush_event signal → single flush_loop owner     │
        │ max_concurrent_writes=10 parallel HTTP batches    │
        │ HTTP gzip always on (5-10× wire reduction)        │
        │ Force-reconnect after 3 consecutive full failures │
        └───────────────────────────────────────────────────┘
              │
              ▼
            InfluxDB → Grafana dashboards
```

## Scaling Parameters

All configurable in `collector/config/config.yaml`:

| Parameter | Default | Location | Effect |
|-----------|---------|----------|--------|
| `polling.max_concurrent` | **100** | Collector semaphore | Max targets collected from simultaneously |
| `polling.interval_seconds` | 300 | Collection cycle | Time budget per full cycle |
| `polling.timeout_seconds` | 45 | Per-HTTP-request | BMC request timeout |
| `parser.max_concurrent_processors` | **16** | Result processor | Parallel metric extraction workers |
| `parser.max_recursion_depth` | 50 | Discovery walk | JSON traversal cap |
| `influxdb.batch_size` | **5000** | Exporter | Points per InfluxDB write (lowered from 10000 to avoid mid-stream timeouts) |
| `influxdb.flush_interval_seconds` | 10 | Exporter | Max delay before flushing buffer |
| `influxdb.write_timeout_ms` | 90000 | Exporter | Per-write HTTP timeout |
| `influxdb.max_concurrent_writes` | **10** | Exporter | Parallel batch writers |

### Internal constants (code-level)

| Parameter | Value | Effect |
|-----------|-------|--------|
| Result queue | `maxsize=1000` | Buffer between collection and processing; drop counter exposed in `poller.get_stats()` |
| InfluxDB buffer cap | **`batch_size × 200 = 1,000,000`** | Absorbs InfluxDB outages |
| Reconnect threshold | **3 consecutive full failures** | Forces write_api refresh |
| Reconnect backoff | 10s → 300s exponential | Between reconnect attempts |
| Status-writer flush | 5s | Drains `_pending_status` dict into one SQL transaction |
| Extraction ThreadPool | `max(16, processors*2)` | Dedicated, bypasses default `min(32, cpu_count+4)` heuristic |
| SSE sync interval | 30s | How quickly new SSE targets are picked up |
| SSE activity timeout | 600s | Dead-stream detection |
| Pipeline-dead threshold | 600s | Drives `is_connected=false` in health check after this many seconds with no successful write |

## Throughput by Collection Mode

### Direct GET (request-based collection)

Each collection cycle fires 6 parallel GETs to metric report endpoints,
reusing a **persistent per-target httpx client + Redfish session** (no
TCP/TLS handshake or session POST per cycle in steady state). Typical
collection duration: 1-3 seconds warm, 3-6 seconds on first cold cycle.

```
Capacity = max_concurrent × (interval_seconds ÷ collection_duration)

Steady state (warm): 100 × (300 ÷ 2) = 15,000 targets/cycle (theoretical)
Conservative real:   100 × (300 ÷ 5) =  6,000 targets/cycle
```

**Practical limit: 250-500 targets** with default `max_concurrent=100`
(InfluxDB ingestion + result-processor CPU are usually the next
bottleneck before the collector itself).

### SSE (Streaming)

One persistent TCP connection per target. No periodic-collection overhead. Bounded by:

- File descriptors (default 1024, tunable to 65K)
- Memory (~50-100 KB per connection)
- Result-processor throughput

**Practical limit: 200-500 targets** (limited by event processing speed,
not connections).

### Mixed Deployment Examples

| Configuration | Direct | SSE | Total | `max_concurrent` |
|---------------|--------|-----|-------|------------------|
| Small | 30 | 20 | 50 | 30 |
| Medium | 130 | 70 | 200 | 50 |
| Large (default) | 250 | 250 | 500 | 100 |

## Result Processing Pipeline

The bottleneck for all modes is the shared result processor. Metric
extraction is CPU-bound (JSON parsing, JSONPath matching, numeric
conversion) and runs in the dedicated `ThreadPoolExecutor`.

The throughput numbers below are **rough estimates**, not measured
benchmarks — actual numbers depend heavily on per-target metric
counts, payload size, and host CPU speed. They're intended as
order-of-magnitude guidance for sizing.

| Workers | Estimated throughput | Use case |
|---------|---------------------|----------|
| 4 | ~200 results/min | ≤ 100 targets |
| 8 | ~350 results/min | 100-250 targets |
| **16** (current default) | ~500-600 results/min | 250-500 targets |
| 32 | diminishing returns | GIL-limited beyond this point |

For 500 targets on 5-min cycles: 500 results / 300s = ~100 results/min.
Default 16 workers has ~5× headroom on this estimate.

For 500 SSE targets streaming every 30s: ~1000 events/min.
16 workers is sufficient; for higher SSE event rates, also bump
`max_concurrent_writes` on the exporter.

## Storage

Raw metrics use ~740 points per target per collection cycle.

### Tiered Retention Strategy

Three-tier storage to make long-term retention affordable for large fleets:

| Tier | Retention | Interval | Purpose |
|------|-----------|----------|---------|
| Raw data | 7 days | 5 minutes | Recent detailed diagnostics |
| 15-min aggregates | 30 days | 15 minutes | Medium-term trend analysis |
| Hourly aggregates | 90 days | 1 hour | Long-term fleet patterns |

### Storage Estimates by Fleet Size

| Fleet Size | Raw (7d) | 15-min (30d) | Hourly (90d) | Total |
|------------|----------|--------------|--------------|-------|
| 50 targets | 1.2 GB | 1.5 GB | 0.3 GB | ~3 GB |
| 100 targets | 2.4 GB | 3.0 GB | 0.5 GB | ~6 GB |
| 250 targets | 6.0 GB | 7.5 GB | 1.2 GB | ~15 GB |
| 500 targets | 12 GB | 15 GB | 2.5 GB | ~30 GB |

Storage scales linearly. SSE targets at the same event frequency as
periodic collection (5 min) use identical storage. Higher-frequency SSE streams
increase proportionally.

### Setup Downsampling

```bash
./gyanam.sh setup-all-downsampling          # both tiers at once
./gyanam.sh setup-15m-downsampling          # 15-minute only
./gyanam.sh setup-hourly-downsampling       # hourly only
```

Retention periods are configurable in `.env`:

```bash
INFLUXDB_RETENTION=7d                # Raw data
INFLUXDB_15M_RETENTION=30d           # 15-minute aggregates
INFLUXDB_HOURLY_RETENTION=90d        # Hourly aggregates
```

## Recommended Tuning

### Defaults (no changes needed for 50-500 targets)

The current `config.yaml` defaults are sized for 250-500 targets. For
smaller fleets the values are still safe — you just won't use the
headroom.

### For very small fleets (<50 targets) — optional downsizing

Save container RAM with no functional change:

```yaml
# config.yaml
polling:
  max_concurrent: 30
parser:
  max_concurrent_processors: 4
influxdb:
  max_concurrent_writes: 4
```

```yaml
# docker-compose.yml
collector: { mem_limit: 2g, cpus: 2 }
influxdb:  { mem_limit: 4g, cpus: 2 }
```

### For 500+ targets — horizontal collector sharding

A single collector is bounded by one Python event loop: the GIL caps
extraction throughput around one core's worth of work, regardless of
in-process tuning. (SQLite single-writer contention and the single
InfluxDB writer pipeline are secondary — in practice the event loop
saturates first; InfluxDB typically sits <5% CPU while one collector is
pegged at 100%.)

**Horizontal sharding (implemented, elastic, lease-based — ON by default).**
The shipped `docker-compose.yml` enables sharding (`COLLECTOR_SHARDING=true`)
and runs **3 collector replicas** by default, each capped at
`MAX_TARGETS_PER_SHARD=200` — sized for a fleet up to **~500 targets**
(3 × 200 = 600 capacity, with headroom). Replicas automatically divide the
fleet between themselves — no manual target partitioning:

```bash
# .env — defaults shown; override only to retune
COLLECTOR_SHARDING=true
MAX_TARGETS_PER_SHARD=200          # per-replica cap (config.yaml default)

# Replica count: 3 by default (deploy.replicas in compose). Scale for fleet size:
docker compose up -d --scale collector=1   # small fleets (≤200 targets)
docker compose up -d --scale collector=3   # ~500 targets (default)
docker compose up -d --scale collector=N   # keep N × cap ≥ fleet size
```

Each replica owns a slice of the fleet (how the slice is chosen depends on
the balancing mode below) as rows in the shared `shard_leases` table,
heartbeated on a TTL; a replica that stops heartbeating has its leases
reclaimed by the survivors, so targets rebalance as replicas are added or
removed. `get_active_targets()` returns only the owned slice, so metric
polling, alert SSE and webhook subscriptions, diagnostic log collection, and
inventory enrichment all shard together. The API merges per-replica state
from `collector_stats` / per-collector `heatmap_snapshots`, so the fleet
views stay whole. The leading `DELETE` in each claim/reconcile pass takes
SQLite's write lock, serializing passes across replicas so no target is ever
double-owned.

**Balancing strategy (`COLLECTOR_BALANCE`, default `dynamic`).**

- **`dynamic` — Rendezvous (HRW) hashing (recommended).** Each target's owner is
  a stable hash of `(target_id, collector_id)`, bounded by the *fair share*
  `ceil(enabled / live_collectors)`. Load lands balanced to **within ±1** (e.g.
  302 targets / 3 replicas → 101/101/100, not 200/102/0), and adding or removing a
  replica moves only ~1/N of the fleet rather than reshuffling it. Each collector
  recomputes its slice every claim pass from the live-collector set (fresh
  `collector_stats` rows) and reconciles the `shard_leases` table to match. Handoff
  is **lazy and gap-free**: a collector keeps polling a target it has shed until its
  lease on it expires, and the new owner can only claim once it is stale — so a
  target is never un-owned mid-handoff. Because HRW keys on the collector id, each
  replica self-assigns a **stable `collector-<ordinal>`** id (lowest free ordinal in
  a `collector_slots` table) so a redeploy under `--scale` doesn't reshuffle the
  fleet; set an explicit `COLLECTOR_ID` (e.g. a k8s StatefulSet pod name) to bypass.
  Like all the lease/heartbeat TTLs here, slot liveness assumes replicas share a
  wall clock (true for compose replicas on one host; k8s StatefulSets pin
  `COLLECTOR_ID` and skip the slot table) — keep clocks within a lease TTL of each
  other if you run collectors across hosts.
- **`static` — greedy id-order claiming (fallback).** Each replica claims up to
  `MAX_TARGETS_PER_SHARD` targets in id order; replicas fill in turn so one may sit
  idle (302/3 at cap 200 → 200/102/0). Correct and simple, but not load-balanced.
  Flip with `COLLECTOR_BALANCE=static` if dynamic ever misbehaves — the two modes
  share the lease table and steal-guard, so a rolling switch never double-owns a
  target (balance is just uneven until all replicas agree on the mode).

In both modes `get_active_targets()` returns only the owned slice, so metric
polling, **alert SSE and webhook subscriptions, diagnostic log collection**, and
inventory enrichment all shard on the same leases. The API merges per-replica state
from `collector_stats` / per-collector `heatmap_snapshots`, so the fleet views stay
whole.

**Sizing & failover.** Capacity is `replicas × MAX_TARGETS_PER_SHARD`; keep
it above the enabled-target count or the surplus goes unpolled (a
`Fleet under-provisioned` warning fires — every renew pass in dynamic mode, once at
startup in static mode). The default cap of 200 is a **steady-state** size: if one
of the three replicas is lost, the two survivors cover 2 × 200 = 400, leaving ~100
targets unpolled until the replica auto-restarts and reclaims (within one
lease TTL, ~75 s). For zero-gap single-replica failover at 500 targets,
raise the cap so `(replicas − 1) × cap ≥ fleet` (e.g. cap 250). Small
fleets can run a single collector (`--scale collector=1`); it owns the whole
fleet up to the cap and never needs the lease table to rebalance.

**Operational preconditions (correctness, not just tuning):**

- **`COLLECTOR_ID` must be unique per replica.** It defaults to the
  container hostname (unique under `--scale`/`replicas`), which is safe.
  Do **not** set the same `COLLECTOR_ID` on multiple replicas — they would
  heartbeat each other's leases, never go stale, and double-poll the shared
  targets (duplicate writes + duplicate BMC subscriptions), with the cap
  applied per-id instead of per-process.
- **`claim_interval_seconds` must be well below `lease_ttl_seconds`**
  (defaults 20 vs 75 — keep `lease_ttl ≳ 3 × claim_interval`). The
  heartbeat cadence equals the claim interval; if it meets or exceeds the
  TTL, a live replica lets its own leases expire and ownership flaps.
- **Each replica must advertise its own routable `ALERT_WEBHOOK_BASE_URL`**
  if webhook alerting is used. A webhook delivered to a replica that does
  not own the target is dropped (events are not forwarded between shards);
  a single shared/load-balanced webhook URL will silently lose events.
- **Provision capacity ≥ fleet size.** With `N × MAX_TARGETS_PER_SHARD <`
  enabled targets, the overflow is simply never claimed — those targets go
  unpolled (graceful degradation, but currently only lightly surfaced in
  logs). Size `N` and the cap so the product exceeds the fleet with
  headroom for one replica loss.
- **Freshness windows.** A departed replica's `collector_stats` and
  `heatmap_snapshots` rows age out of their read windows rather than being
  deleted on crash; during that window fleet aggregates can briefly
  double-count or show a stale heatmap value for a reclaimed host. Keep the
  stats/heatmap max-age ≤ `lease_ttl` to minimise the window.

**Known limitation — leases balance *count*, not *cost*.** A replica owning
fewer but heavier targets (more/larger metric reports) can saturate while a
peer idles. If one shard sustains ~100% CPU, add a replica; a future
improvement is to weight leases by observed poll cost.

**Beyond sharding — migrate the state DB to PostgreSQL.** The SQLAlchemy
code already has a PostgreSQL branch (`repository.py:91-100`). Point
`DATABASE_URL` at `postgresql+asyncpg://…` and add a `postgres` service to
`docker-compose.yml` to remove the SQLite single-writer ceiling for the
shared coordination tables.

## Alert Subsystem Scalability

Alerts are a high-volume, append-heavy time series and are stored **separately
from the SQLite target store**, in **PostgreSQL** (`postgres` service). The
`TargetRepository` runs two engines; alert methods use the alert engine.
`ALERTS_DATABASE_URL` (PostgreSQL) is **required**.

**Scope**: only **Critical** and **Warning** events are collected. OK /
informational events are intentionally out of scope (very high volume, not
actionable) — controlled by `alerts.severities` in `config.yaml`.

Design points that keep it scalable:

- **Indexed occurrence time** — a materialized `occurred_at` column
  (`event_timestamp` when the BMC provided one, else `received_at`) is indexed
  and drives both the display window and ordering (latest-first). Avoids a
  full-scan `coalesce()` sort as history grows.
- **Server-side pagination + filtering** — the UI queries one page (100 rows)
  with `count_alerts` for totals; severity/target/search/time filters run in
  SQL, not client-side over a mega-page. Raw event JSON is **lazy-loaded** per
  row (`GET /alerts/api/{id}/raw`), keeping the list payload small
  (~370 KB/page vs multi-MB).
- **Bulk retention** — `delete_alerts_before` is a single `DELETE` statement
  (no ORM row materialization). Retention keys off **`received_at`** (age since
  observed) so historical events surfaced by a baseline pull aren't purged on
  arrival.
- **Incremental baseline pull** — the fallback log pull records a per-(target,
  log-collection) high-water mark (`log_cursors`) and fetches only entries newer
  than last seen, instead of re-fetching + de-duplicating the whole tail every
  cycle. SSE/webhook is the primary real-time channel; the periodic pull is a
  safety net (default `baseline_repull_interval_minutes: 60`).
- **JSONB raw storage** — the full original event/log entry is stored as JSONB
  on PostgreSQL (JSON/TEXT on SQLite), so no source field is lost.
- **Idempotent ingest** — a unique `dedup_key` (source `@odata.id` for baseline
  entries, else target+message+timestamp) makes re-pulls and SSE reconnects
  safe.

- **Connection pooling** — both `api` and `collector` open their own alert
  engine, so pools are deliberately small (`pool_size=10, max_overflow=10` →
  ≤20 each, ≤40 combined) to stay well under PostgreSQL's default
  `max_connections` (100). Raise both together if you scale out.
- **Concurrent schema init** — `api` and `collector` both run `init_db` at
  startup; alert-store DDL is serialized with a PostgreSQL transaction-scoped
  advisory lock (`pg_advisory_xact_lock`) to avoid a create-table/index race.
- **Graceful degradation** — alert-store init is non-fatal. If PostgreSQL is
  unreachable at startup, targets/metrics/health keep working and only the
  alerts feature degrades. `/health/detailed` reports
  `alert_manager.alert_store_connected`.

### Known caveat: retention vs. long-standing conditions

Retention deletes by `received_at` (age since observed) and the incremental
cursor won't re-pull entries older than its high-water mark. A Critical/Warning
that **occurred more than the retention window ago but is still present** on the
BMC will therefore be dropped at `retention_days` after it was first observed and
will **not** reappear (the cursor has moved past it). SSE re-alerts on genuine
state changes; but a static, unresolved, very old condition can age out of the
store. If you need such conditions to persist, raise `retention_days` or
periodically reset the `log_cursors` for a full re-sync. This is an accepted
trade-off that keeps the store bounded.

### Optional: faster search at scale

`message`/target search uses `LOWER(col) LIKE` (sequential scan). If search
becomes hot on very large tables, add a PostgreSQL `pg_trgm` GIN index:
`CREATE EXTENSION pg_trgm; CREATE INDEX ... USING gin (message gin_trgm_ops);`.
Not enabled by default (avoids an extension dependency).

**Deferred (add when a single node's ceiling is reached):** native monthly
range **partitioning** of `alerts` by `occurred_at` (instant retention via
partition drop) and read replicas. The current single indexed table with bulk
retention handles millions of rows comfortably.

### Backup

Alert history is **intentionally not backed up**: it is reconstructable from the
BMC event/log services via the baseline pull, and is high-volume and
short-retention by design. The documented backup covers the SQLite target
store, `.env`, and config (which are *not* reconstructable). If you nonetheless
want alert history in backups, add `pg_dump gyanam_alerts` to your backup job.

## File Descriptor Limits

For >200 SSE targets, raise the collector's open-file limit:

```yaml
# docker-compose.yml
collector:
  ulimits:
    nofile:
      soft: 8192
      hard: 16384
```

## Dashboard Organization

Grafana ships with 11 auto-provisioned dashboards under three tiers
(see `grafana/provisioning/dashboards/`):

### 📊 Per-System GPU Monitoring (6 dashboards)

Detailed diagnostics for individual systems
(`grafana/provisioning/dashboards/per-system/`):

- GPU Compute (`gpu-compute.json`)
- GPU Memory (`gpu-memory.json`)
- GPU Interconnect (`gpu-interconnect.json`)
- VR/HSC/IBC Power (`vr-hsc-ibc-power.json`)
- UBB Platform Sensors (`ubb-platform-sensors.json`)
- UBB System Health (`ubb-system-health.json`)

### 🌐 Fleet-Wide Monitoring (3 dashboards)

Aggregate views across all systems
(`grafana/provisioning/dashboards/fleet/`):

- **Fleet Overview** (`fleet-overview.json`) — system/GPU counts,
  average temperatures, total power consumption
- **Fleet Heatmap** (`fleet-heatmap.json`) — temperature and power
  distribution visualisation
- **Fleet Outliers** (`fleet-outliers.json`) — hot GPUs, high-power
  consumers, thermal imbalance detection

### 📈 Historical & Trends (2 dashboards)

Long-term analysis using downsampled data
(`grafana/provisioning/dashboards/historical/`):

- **GPU Long-term Trends** (`gpu-longterm-trends.json`) — 90-day
  historical patterns from hourly aggregates
- **GPU Historical Trends (Hourly)** (`gpu-historical-trends-hourly.json`)
  — finer hourly-aggregate view

## Monitoring Scaling Health

`/health/detailed` (or `./gyanam.sh status`) exposes the live counters
to watch. The most useful ones:

| Field | Healthy | Warning |
|-------|---------|---------|
| `poller.cached_clients` | ≈ number of non-SSE enabled targets after warm-up | Below that for sustained period → eviction storm (BMCs killing sessions) |
| `poller.client_cache_hit_rate_pct` | ≥99% in steady state | Sustained <90% → recheck BMC session timeouts |
| `poller.inflight` | bounded by `max_concurrent` | Stuck at ceiling for ≫ cycle → a target hung; check `task_timeout` |
| `poller.result_queue_size` | <100 | Sustained >500 → processor can't keep up; bump `max_concurrent_processors` |
| `poller.result_queue_drops` | 0 | Non-zero → results being lost; investigate why processor is behind |
| `poller.pending_status_updates` | <100 between flushes | Sustained growth → status writer stalled |
| `exporter.connected` | true | false for >600s → pipeline silent stall (previously an undetected silent-stall case) |
| `exporter.consecutive_batch_failures` | 0 | ≥3 triggers automatic reconnect; if it never resets, InfluxDB is genuinely down |
| `exporter.buffer_size` | < a few thousand | Near `batch_size × 200` → InfluxDB outage or sustained too-slow ingest |
| `exporter.failure_rate_pct` | <2% | >5% → upstream issue (latency / saturation) |
| Collection cycle visibility | "Scheduling N due polls (M in-flight)" log line each tick | Stops → collector loop stalled |
| Circuit breaker | 0 targets in backoff (steady state) | ≥5 consecutive failures triggers 6× interval backoff per target |

## See Also

- [`DEPLOYMENT.md`](./DEPLOYMENT.md) — Ubuntu deployment guide for
  300-node fleets
- [`DATA_EXPORT_REFERENCE.md`](./DATA_EXPORT_REFERENCE.md) — CSV export
  pipeline (relevant for analytics workloads)
- [`CODEQL_REPORT.md`](./CODEQL_REPORT.md) — running CodeQL locally (live status: Security tab)
