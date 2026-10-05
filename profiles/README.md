# Gyanam Redfish Interoperability Profile

[`GyanamTelemetryAggregator.v1_0_2.json`](GyanamTelemetryAggregator.v1_0_2.json) is a
DMTF **Redfish Interoperability Profile** ([DSP0272](https://www.dmtf.org/dsp/DSP0272))
that declares what a system's BMC must expose for gyanam to discover, inventory,
stream metrics from, and collect diagnostic logs from it **without any
vendor-specific code**.

The goal: a system that conforms to this profile "just works" with gyanam.

## Requirement tiers

The profile uses standard DSP0272 `ReadRequirement`/`WriteRequirement` values:

- **Mandatory** — the core gyanam depends on to function at all:
  `ServiceRoot`, `ComputerSystem`, `Chassis`, the `TelemetryService` graph
  (`MetricReportDefinitions` / `MetricReports` / `MetricDefinitions`), and
  `EventService` (+ `EventDestination` create/delete) for subscriptions.
- **Recommended** — unlocks the fuller feature set:
  - GPU inventory (`Processor` with `ProcessorType` GPU/Accelerator + `Model` +
    `ProcessorMemory.CapacityMiB`), `ProcessorSummary` / `MemorySummary`,
    `BiosVersion`, `Manager.FirmwareVersion`, and
    `UpdateService/FirmwareInventory` (`SoftwareInventory.Version`).
  - Standards-based metric-unit resolution via `MetricDefinition.Units` (UCUM).
  - Event-driven diagnostic-log collection via
    `LogService` + the `CollectDiagnosticData` action.

A BMC that meets only the Mandatory requirements still works; the Recommended
requirements map one-to-one onto gyanam's richer features.

## Self-testing a system

Validate a live BMC against the profile with the DMTF
[Redfish-Interop-Validator](https://github.com/DMTF/Redfish-Interop-Validator):

```bash
pip install redfish_interop_validator
rf_interop_validator \
    --ip https://<bmc-host> --user <user> --passwordFromFile <pwfile> \
    profiles/GyanamTelemetryAggregator.v1_0_2.json
```

A clean run (no `Mandatory` failures) means the system is compatible with gyanam.
`Recommended` findings indicate which optional gyanam features that system will
light up.

## Versioning

Bump `ProfileVersion` when requirements change. Keep the filename's version suffix
in sync (`GyanamTelemetryAggregator.v<major>_<minor>_<patch>.json`).
