# GYANAM — GPU Observability Assistant

**Primary goal**: provide an open cluster- or fleet-wide **debug and observability reference
implementation for AMD GPU products** — giving operators accurate,
continuous, out-of-band telemetry and shareable diagnostic evidence from
AMD Instinct fleets, using only industry-standard interfaces and without
proprietary agents or vendor lock-in.

GYANAM gathers telemetry from GPU servers through DMTF Redfish-defined
interfaces — using native Redfish Aggregation on ODM/OEM BMCs that
support it, or proxy-based collection where they don't (with optional
SSH-proxy and SSE streaming paths) — parses the telemetry into a
schema-aware metric model, stores it in a time-series database
(InfluxDB by default; Prometheus also supported), and surfaces it
through pre-built Grafana dashboards. On-demand diagnostic-log
collection, alert subscriptions, and a CSV export pipeline round out
the toolset so operators and debug engineers can move from "something
is wrong" to a shareable evidence bundle without writing custom code.

<!-- HERO IMAGE — capture per docs/screenshots/README.md -->
<img src="docs/screenshots/01-hero-fleet-overview.png" alt="GYANAM Fleet Overview dashboard — fleet-wide GPU temperature, power, and health at a glance" width="800">

## Problem Statement

Running AMD Instinct fleets at scale, **observability and health
monitoring** are what keep jobs productive. Operators — whether a
hyperscaler, a startup neocloud, an established software org, or an
early-stage team — need open, ready-made building blocks for
**cluster-wide and fleet-wide debug and observability**: continuous
telemetry across every node, fast detection of the outliers that signal
trouble, and the ability to package diagnostic evidence the moment a job
breaks. Without that, debug starts from a cold trail.

These same themes show up in industry references like SemiAnalysis's
[ClusterMAX™ rating system](https://www.clustermax.ai/), which evaluates
GPU clouds in part on proactive health checks, out-of-the-box dashboards,
and fleet-wide monitoring — a useful articulation of what good GPU
observability looks like in practice.

GYANAM aims to make those building blocks available and open. As a GPU
hardware company, AMD's interest is straightforward: **faster debug and
faster turnaround, so customer productivity returns as quickly as
possible — regardless of fleet size or customer maturity.** Debug
organizations today too often receive
incomplete or stale diagnostic data from the point of failure, limiting
root-cause accuracy and slowing RMA Failure Analysis. GYANAM is an
on-premise, customer-deployable assistant that harvests time-series
telemetry and critical debug logs using only open specifications — DMTF
Redfish, OCP OAM, and related standards — so teams can autonomously
collect, inspect, and share comprehensive observability and debug data,
improving **Time to Hypothesis (TTH)** and **Time to Root Cause (TTR)**
while building trust through full transparency of the implementation.

## What GYANAM does

The GPU Observability Assistant provides:

-   **Continuous large-scale observability** — automated, periodic
    harvesting of OOB telemetry across a 5K-GPU cluster with
    per-target persistent connections, fire-and-forget scheduling, and
    tiered retention for long-term analysis.
-   **Debug-effort enablement** — on-demand diagnostic log-bundle
    collection per target, structured alert subscriptions (SSE +
    webhook fallback), and a robust CSV export pipeline with
    pre-flight count, chunking, retry, and server-side aggregation
    for sharing evidence with engineering.
-   **Standards-only data plane** — DMTF Redfish + OCP-aligned schemas,
    no proprietary agents on the host.
-   **On-premise deployment** — runs entirely inside the customer's
    security boundary; nothing leaves their network unless explicitly
    exported.
-   **Customer-initiated data packaging** — operators decide what
    snapshots to share when filing tickets or RMAs.
-   **Reference implementation for the ecosystem** — an open AMD-GPU
    observability/debug baseline that OEMs, cloud partners, and customers
    can adopt and align around open standards.

## Supported Features

What works today for cluster-wide and fleet-wide debug and observability:

| Capability | Details |
|------------|---------|
| **Out-of-band telemetry collection** | DMTF Redfish telemetry gathering (Redfish Aggregation or proxy-based) sized for a standard 5K-GPU cluster (typical of a training or inference AI cluster size). Categories available for deep-dive: temperature (GPU die, HBM memory, VR, board), power (per-GPU, aggregate, board), voltage & current (HBM, VDD, GPU-IO, VR rails), GPU utilization & memory bandwidth, link & connectivity (retimer, processor-port / interconnect), and health & status rollups — validated against AMD Instinct MI325 / MI350 reference artifacts |
| **Alternative transports** | SSH-proxy for air-gapped BMCs; SSE streaming where supported |
| **Schema-aware extraction** | 33 JSONPath metric schemas + auto-discovery of numeric fields |
| **GPU health metrics** | Temperature, power, clock, ECC/memory errors, and other OOB sensor data |
| **Time-series storage** | InfluxDB 2.7 (default) or Prometheus, with 15-min / hourly downsampling and tiered retention |
| **Pre-built Grafana dashboards** | 11 dashboards — fleet-wide, per-system drill-down, and historical trend views |
| **Fleet outlier detection** | Dashboard surfacing hot GPUs, high-power consumers, and thermal imbalance across the fleet |
| **Alerting** | Real-time SSE / webhook alert subscriptions with severity routing and history |
| **On-demand diagnostic logs** | Per-target log-bundle collection, download, and sharing for debug / RMA |
| **Debug-evidence export** | CSV export pipeline with pre-flight count, chunking, retry, and server-side aggregation |
| **Security posture** | SSRF/CSRF protection, bcrypt auth, Fernet-encrypted credentials, path-traversal protection, CodeQL-clean (see [`docs/CODEQL_REPORT.md`](docs/CODEQL_REPORT.md)) |

## Roadmap

Future work continues the same charter — a debug and observability
reference for AMD GPU products — by broadening *what* and *where* we can
observe:

- **In-band GPU telemetry via AMD Device Metrics Exporter (DME)** —
  integrate [AMD Device Metrics
  Exporter](https://rocm.blogs.amd.com/software-tools-optimization/device-metrics-exporter/README.html)
  to complement out-of-band Redfish data with rich in-band, hardware-level
  metrics: per-process attribution (`KFD_PROCESS_ID`), field-identifier
  reliability events (`GPU_AFID_ERRORS`), thermal/power/utilization
  violation counters, HBM thermal monitoring, and fine-grained clock
  behavior — turning diagnosis from inference-based into data-driven.
- **VM-level telemetry and log collection** — extend collection beyond the
  bare-metal BMC view into virtualized / tenant GPU environments making use of industry standard references.

Have a use case or want to help land one of these? See
[Contributing](#contributing).

## Quick Start

All operations use the `gyanam.sh` management script. This is the
short path; the full guide is in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

```bash
./gyanam.sh init     # one-time: generates .env (save the printed credentials)
./gyanam.sh start    # boots all four containers
```

| Service | URL | Default Credentials |
|---------|-----|---------------------|
| Web UI | http://localhost:8080 | admin / changeme |
| Grafana | http://localhost:3000 | (from init output) |
| InfluxDB | http://localhost:8086 | (from init output) |

Then open the Web UI, click **Add Target**, enter BMC connection details,
and click **Test** to verify. For bulk onboarding use **Import CSV** /
**Export CSV** on the targets page. Full management commands, downsampling
setup, and export recipes are documented in the
[guides below](#documentation).

## Screenshots

<!-- Gallery — capture per docs/screenshots/README.md -->

<a href="docs/screenshots/02-fleet-heatmap.png">
  <img src="docs/screenshots/02-fleet-heatmap.png" alt="Fleet temperature heatmap" width="800">
</a>
<br/><sub><b>Fleet Heatmap</b> — temperature and power distribution across every GPU in the fleet, at a glance.</sub>

<br/><br/>

<a href="docs/screenshots/03-targets-page.png">
  <img src="docs/screenshots/03-targets-page.png" alt="Targets management page" width="800">
</a>
<br/><sub><b>Targets</b> — bulk CSV import, per-target test &amp; on-demand log collection, live telemetry-gathering status.</sub>

<br/><br/>

<a href="docs/screenshots/04-gpu-compute.png">
  <img src="docs/screenshots/04-gpu-compute.png" alt="Per-system GPU compute dashboard" width="800">
</a>
<br/><sub><b>Per-system drill-down</b> — every GPU's compute, memory, interconnect, and power broken out.</sub>

<br/><br/>

<a href="docs/screenshots/05-alerts-page.png">
  <img src="docs/screenshots/05-alerts-page.png" alt="Alerts subscription and history page" width="800">
</a>
<br/><sub><b>Alerts</b> — real-time SSE / webhook subscriptions with severity routing and history.</sub>

<br/><br/>

<a href="docs/screenshots/06-collected-logs.png">
  <img src="docs/screenshots/06-collected-logs.png" alt="Collected diagnostic log bundles" width="800">
</a>
<br/><sub><b>Diagnostic log bundles</b> — on-demand harvest, download, and share for RMA or debug tickets.</sub>

<br/><br/>

<a href="docs/screenshots/07-fleet-outliers.png">
  <img src="docs/screenshots/07-fleet-outliers.png" alt="Fleet outliers dashboard" width="800">
</a>
<br/><sub><b>Outlier detection</b> — hot GPUs, high-power consumers, thermal imbalance — surfaces fleet anomalies fast.</sub>

> Screenshots above expect PNG files under [`docs/screenshots/`](docs/screenshots/)

## Contributing

GYANAM is open source and welcomes contributions from everyone —
hyperscaler operator, neocloud engineer, or individual debugging a single
node. Bug reports, dashboard additions, new metric schemas, transport
support, and documentation are all valued.

**Reporting issues** — bugs, feature requests, and questions go through the
standard GitHub issue process at
[github.com/amd/Gyanam-GPU-Observability-Assistant/issues](https://github.com/amd/Gyanam-GPU-Observability-Assistant/issues).
Click *New Issue* to use the bug-report or feature-request template. Please
search existing issues first, and **do not** file security vulnerabilities
as public issues — follow [SECURITY.md](SECURITY.md) instead.

Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** before opening a pull
request. In short:

- All commits require a **DCO sign-off** (`git commit -s`).
- Run the linters / hooks documented in [`LINTING.md`](LINTING.md).
- Follow the commit-message convention in the contributing guide.

## License

GYANAM is licensed under the **MIT License** — see [LICENSE](LICENSE).
Copyright © 2026 Advanced Micro Devices, Inc.

## Documentation

| Doc | When to read it |
|-----|-----------------|
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Full setup / deployment on Ubuntu, sized for a 5K-GPU cluster |
| [System Architecture (PDF)](docs/architecture.pdf) | Runtime data flow + 4-container layout (source: [`docs/architecture.mmd`](docs/architecture.mmd)) |
| [Class Diagram (PDF)](docs/class-diagram.pdf) | Class relationships across layers (source: [`docs/class-diagram.mmd`](docs/class-diagram.mmd)) |
| [`docs/SCALABILITY.md`](docs/SCALABILITY.md) | Tuning per fleet size + understanding the runtime architecture |
| [`docs/DATA_EXPORT_REFERENCE.md`](docs/DATA_EXPORT_REFERENCE.md) | Exporting metrics to CSV (gyanam.sh wrapper + native InfluxDB recipes) |
| [`scripts/README.md`](scripts/README.md) | Volume / disk-space monitoring scripts and automated growth tracking |
| [`LINTING.md`](LINTING.md) | Pre-commit / ruff / mypy / shellcheck setup |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute, DCO sign-off, PR workflow |

For the full set of management commands (`stop`, `restart`, `status`,
`monitor`, `logs`, `build`, downsampling setup, InfluxDB export, `clean`),
run `./gyanam.sh help`.
