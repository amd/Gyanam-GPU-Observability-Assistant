# CPER Decoding (Common Platform Error Record)

GYANAM enriches hardware-fault alerts by decoding the **CPER** attachment a BMC
references from a Redfish `LogEntry`, turning an opaque record into a
human-readable "refined message" shown in the alerts UI.

This document records **what we decode, how, the public sources it is based on,
and the risks** — so it is clear the implementation relies only on publicly
available information.

## Trigger and flow

An alert is eligible for CPER enrichment when its raw Redfish `LogEntry` has
`DiagnosticDataType == "CPER"` **or** mentions `"CPER"` in `Resolution`, and
carries an `AdditionalDataURI` (`repository._cper_eligible`).

1. At ingest, eligible alerts are marked `cper_status = "pending"`.
2. A background worker (`CperEnrichmentWorker` in `collector/src/cper_worker.py`,
   started/stopped by `AlertManager`) claims pending
   alerts, fetches the attachment from the BMC (`cper_decoder.fetch_cper_attachment`,
   size-capped, per-target credentials), and decodes it.
3. The refined message + full decoded JSON are stored on the alert; the UI shows
   the refined message inline and lazily loads the decoded JSON on demand.

Statuses: `pending` → `decoded` | `no_data` | `fetch_failed` | `decode_failed`
| `unavailable` (attachment already rotated off the BMC). Retries are bounded by
`alerts.cper_max_attempts`.

## Two decode layers

### 1. Standard CPER — OpenBMC `libcper`

The CPER wrapper and standard sections (memory, PCIe, processor, CXL, …) are
defined by the **UEFI Specification, Appendix N**. We decode them with the
**OpenBMC `libcper`** library, which is compiled from source in a Docker build
stage and invoked as the `cper-convert to-json` CLI.

- Source: <https://github.com/openbmc/libcper> (pinned commit in
  `collector/Dockerfile`, arg `LIBCPER_REF`).
- Design: <https://github.com/openbmc/docs/blob/master/designs/cper-records.md>
- Runtime dependency: `libjson-c5` only.

`cper_decoder.summarize_cper` distills libcper's JSON (record severity,
notification type, and each section's type/severity/message) into one line.

### 2. AMD vendor sections — `amd_cper_sections.py`

AMD Instinct/OAM GPUs emit **vendor CPER sections** that libcper does not
decode (it returns them as an opaque `Unknown` base64 blob). These are decoded by
`collector/src/redfish/amd_cper_sections.py`.

**This module is written entirely from AMD's open-source Linux `amdgpu` kernel
driver — the authoritative, publicly published definition of these records.**

Public sources (torvalds/linux, `drivers/gpu/drm/amd/`):

| Source | Provides |
|---|---|
| `include/amd_cper.h` | Section GUIDs; packed structs `cper_sec_crashdump_{hdr,boot,fatal}`, `cper_sec_crashdump_reg_data`, `cper_sec_nonstd_err`; `CPER_ACA_REG_COUNT=32`, `CPER_MAX_OAM_COUNT=8`, context-type constants |
| `amdgpu/amdgpu_aca.h` | `enum aca_reg_idx` (CTL=0, STATUS=1, ADDR=2, MISC0=3, CONFIG=4, IPID=5, SYND=6, DESTAT=8, DEADDR=9, CTL_MASK=10) and the architectural `ACA_REG__STATUS__*` / `ACA_REG__IPID__*` bitfields |
| `pm/swsmu/smu13/smu_v13_0_6_ppt.c` | `smu_v13_0_6_mca_ipid_table` — the (HardwareID, McaType) → IP-block mapping used for best-effort IP naming (MI300 / `smu_v13_0_6`): UMC `0x96/0x0`, SMU `0x01/0x1`, MP5 `0x01/0x2`, PCS_XGMI `0x50/0x0` |

Introduced by the amdgpu CPER/ACA patch series (2025) on the amd-gfx list, e.g.
["drm/amd/include: Add amd cper header"](https://www.mail-archive.com/amd-gfx@lists.freedesktop.org/msg118471.html)
and ["Include ACA error type in aca bank"](https://www.mail-archive.com/amd-gfx@lists.freedesktop.org/msg118467.html).

Section GUIDs handled:

| GUID | Meaning |
|---|---|
| `32ac0c78-2623-48f6-b0d0-7365725fd6ae` | `AMD_CRASHDUMP` (boot / fatal crashdump) |
| `32ac0c78-2623-48f6-81a2-ac691780551d` | `AMD_GPU_NONSTANDARD_ERROR` (runtime RAS) |

What we extract (as much as the published layout allows):

- **Boot crashdump** (`cper_sec_crashdump_boot`, 208 B): firmware id and OAM
  message words.
- **Fatal crashdump** (`cper_sec_crashdump_fatal`, 176 B): the named ACA
  register set (`MCA_STATUS`, `ADDR`, `IPID`, `SYND`), decoded to architectural
  MCA flags (VAL/UC/PCC/UECC/CECC/deferred/poison/overflow), error code, address,
  and IPID (HardwareID / McaType / instance / best-effort IP block).
- **Runtime nonstandard** (`cper_sec_nonstd_err`, 272 B): **every** field —
  all named ACA registers from `reg_dump` (CTL, STATUS, ADDR, MISC0, CONFIG,
  IPID, SYND, DESTAT, DEADDR, CTL_MASK), the full `ms_check` bit set, the section
  `error_type` GUID, apic id, context and identifier fields.

Register bit-decode (from `ACA_REG__*` macros):
- **STATUS / DESTAT**: val, overflow, uc, en, miscv, addrv, pcc, errcoreid_valid,
  tcc, syndv, cecc, uecc, deferred, poison, scrub, errcoreid, **addr_lsb**,
  error_code, error_code_ext.
- **MISC0**: valid, overflow, **error_count** (ErrCnt).
- **SYND**: error_information.
- **ADDR / DEADDR**: `raw` + `address_valid` + **`valid_address`** (masked by the
  paired STATUS/DESTAT `addr_lsb`, per MCA_ADDR semantics).
- **IPID**: mca_type, hardware_id, instance id hi/lo, + best-effort ip_block.
- **UMC/HBM location** (when IPID = UMC): `socket_id`, `aid_die_id`, `channel`,
  `umc_instance`, decoded from the IPID InstanceId fields via the MI300
  `umc_v12_0.h` `MCA_IPID_2_{SOCKET_ID,DIE_ID,UMC_CH,UMC_INST}` macros (validated:
  `socket_id` matches the record's OAM number). **Bank / row / column are NOT
  decoded** — they require the live SMU MCA-address→physical translation
  (`umc_v12_0_convert_error_address`), which isn't available from the CPER alone,
  so they're omitted rather than guessed.
- CTL / CTL_MASK / CONFIG: no public bit definitions → kept raw.

**De-duplication:** decoded fields are stored once — the ACA registers live only
under `registers` (no duplicated top-level `status`/`address`/`ipid`/`syndrome`).
A CPER record can also contain repeated sections; the UI panel collapses
identical sections into one entry with an occurrence count (the raw JSON keeps
all).

**Best-effort IP-block naming:** the IPID `(HardwareID, McaType)` is mapped to an
IP block (UMC/SMU-family/MP5/PCS_XGMI) using the MI300 table above. This is
**best-effort**: the values are ASIC/kernel-version specific, unknown pairs fall
back to the raw hex (never a wrong label), and the SMU entry aggregates
GFX/SDMA/MMHUB/VCN/JPEG (the kernel disambiguates those by MCA error code, which
we do not attempt). The mapping is tagged `ip_block_source` in the output.

## Display

The alert detail shows the enriched one-line summary inline and a **"Load
decoded CPER"** button that renders a **structured, readable panel**:

- a plain-English **`description`** per section (e.g. *"Uncorrectable ECC error
  on UMC (HBM memory) (poisoned data, deferred)."*) and an **`error_class`**
  badge (Uncorrectable / Deferred / Corrected / Informational), derived from the
  decoded MCA_STATUS + IPID;
- the CPER record header plus a per-section breakdown of **every** decoded field
  as key/value tables, with **friendly field labels** (e.g. `uecc` →
  "Uncorrectable ECC", `ip_block` → "IP block");
- the full **raw JSON** under a collapsible section.

## Retry / failure handling

Fetch failures are classified: a 404/410 (attachment definitively gone) →
`unavailable` (terminal); a timeout/network/oversize error → `fetch_failed`,
which is **periodically requeued** (`cper_retry_failed_interval_minutes`,
default 6 h) so a slow or briefly-unavailable BMC recovers automatically. The
per-fetch timeout defaults to 60 s (BMC attachment serving can take ~20–30 s);
concurrency keeps that affordable. Rows left `pending` with retries exhausted
are reconciled to `fetch_failed` each cycle so nothing lingers in limbo.

## Risks and how they are mitigated

**The AMD structs live in *driver-internal* kernel headers, not a stable
`uapi/` ABI**, and have already been refactored once (`amd_cper.h` →
`ras_cper.h`) while keeping the same GUID values. Field layout, sizes, or the ACA
register count *can* change between kernel/firmware versions.

The decoder is therefore deliberately **defensive**:

- Dispatches by section **GUID + byte length**; validates the buffer is at least
  the expected packed size before reading any field.
- On any mismatch it returns a `{"decode_error": …}` marker instead of guessing,
  and the caller falls back to the generic summary (section GUID, size, embedded
  ASCII strings) — **never a wrong claim**.
- The ACA `STATUS`/`IPID` bit decode is architectural (AMD MCA/SMCA), which is
  more stable than the wrapper structs.
- The SMCA `HardwareID` is surfaced as a **raw value**, not mapped to an
  IP-block name, because that mapping is version/SoC specific and cannot be
  verified from within this project.

**Validation status:** the `AMD_CRASHDUMP` boot path is validated against a real
Instinct record (`reference_artifacts/cper/amd_crashdump_boot.bin`, firmware
`01.10.180862`). The fatal and runtime paths are implemented from the published
layout and covered by **round-trip fixtures** in `tests/test_cper.py`; they
should be re-validated against a real runtime/fatal record before their fields
are relied upon operationally.

## Extending / upgrading

- **Bump libcper:** change `LIBCPER_REF` in `collector/Dockerfile` and rebuild.
- **New/changed AMD layout:** update the sizes/offsets in
  `amd_cper_sections.py` against the current kernel headers and add a fixture.
- **Upstream option (recommended long-term):** contribute AMD section support to
  OpenBMC libcper (it already carries an NVIDIA vendor section), after which
  `cper-convert` decodes it and the Python post-processor can be retired.

## Configuration (`alerts.*`)

`cper_enrichment_enabled`, `cper_poll_interval_seconds`, `cper_max_attempts`,
`cper_batch_size`, `cper_concurrency`, `cper_fetch_timeout`,
`cper_retry_failed_interval_minutes`, `cper_decode_timeout`, `cper_max_bytes`,
`cper_convert_path`.
