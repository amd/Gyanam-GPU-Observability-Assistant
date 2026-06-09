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
     ├── SSH Proxy (collect every 5m)                            ─┤
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

### SSH Proxy (request-based collection)

SSH connect + sequential `curl`/tool calls. Typical collection duration: 8-20s
warm (the SSH transport is also cached on the persistent client).

```
Default: 100 × (300 ÷ 15) = 2,000 targets/cycle (theoretical)
```

**Practical limit: 100-200 targets** (SSH-proxy fan-out is more
network-fragile than direct GET).

### SSE (Streaming)

One persistent TCP connection per target. No periodic-collection overhead. Bounded by:

- File descriptors (default 1024, tunable to 65K)
- Memory (~50-100 KB per connection)
- Result-processor throughput

**Practical limit: 200-500 targets** (limited by event processing speed,
not connections).

### Mixed Deployment Examples

| Configuration | Direct | SSH Proxy | SSE | Total | `max_concurrent` |
|---------------|--------|-----------|-----|-------|------------------|
| Small | 20 | 10 | 20 | 50 | 30 |
| Medium | 100 | 30 | 70 | 200 | 50 |
| Large (default) | 200 | 50 | 250 | 500 | 100 |

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

### For 500+ targets — sharding required

The current architecture has the following hard ceilings that no amount
of in-process tuning can solve:

- **SQLite single-writer**: even with batching, write contention scales
  linearly with target count.
- **Single Python process per collector**: the GIL bounds extraction
  throughput around ~600 results/min on one core.
- **Single InfluxDB writer pipeline**: each collector talks to one
  InfluxDB; write rate caps around 50-100K points/sec.

Recommended approach beyond ~750 targets:

- **Shard collectors by target group** — run multiple collector
  containers, each with a partition of targets. They can write to a
  shared InfluxDB or per-shard InfluxDB instances.
- **Migrate state DB to PostgreSQL** — the SQLAlchemy code already has
  a PostgreSQL branch (`repository.py:91-100`). Just point `DATABASE_URL`
  at `postgresql+asyncpg://…` and add a `postgres` service to
  `docker-compose.yml`.

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
| `exporter.connected` | true | false for >600s → pipeline silent stall (the [previously-undetected case](./CODEQL_REPORT.md)) |
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
- [`CODEQL_REPORT.md`](./CODEQL_REPORT.md) — current security posture
