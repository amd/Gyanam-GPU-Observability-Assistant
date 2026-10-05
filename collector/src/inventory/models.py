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
"""Basic inventory value object."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

# One EIA-310 rack unit in millimetres (used to derive height in U from HeightMm).
_MM_PER_U = 44.45


@dataclass
class Inventory:
    """Basic hardware inventory read from a BMC's standard Redfish resources."""

    manufacturer: str | None = None
    model: str | None = None
    sku: str | None = None
    serial: str | None = None
    part_number: str | None = None
    power_state: str | None = None
    health: str | None = None
    asset_tag: str | None = None
    chassis_type: str | None = None
    processor_count: int | None = None
    processor_model: str | None = None
    gpu_count: int | None = None
    gpu_model: str | None = None
    gpu_memory_gib: float | None = None
    memory_gib: float | None = None
    bios_version: str | None = None
    bmc_firmware: str | None = None
    # Component -> version, from /redfish/v1/UpdateService/FirmwareInventory.
    firmware: dict | None = None
    height_mm: float | None = None
    height_u: int | None = None
    unit_type: str | None = None
    # BMC-reported location, carried for reconciliation (not stored in to_dict()).
    placement: object | None = field(default=None, repr=False)

    def has_data(self) -> bool:
        """True if any inventory field was populated."""
        return any(getattr(self, f.name) is not None for f in fields(self) if f.name != "placement")

    def derive_height_u(self) -> None:
        """Populate ``height_u`` from ``height_mm`` when not already set."""
        if self.height_u is None and self.height_mm:
            self.height_u = max(1, round(self.height_mm / _MM_PER_U))

    def to_dict(self) -> dict:
        """Serializable hardware fields (excludes the transient placement)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "placement" and getattr(self, f.name) is not None
        }

    def tooltip(self) -> dict:
        """Compact subset shown in the Data Hall hover tooltip."""
        return tooltip_fields(self.to_dict())


def _combo(count, model, noun: str) -> str | None:
    """Format a '{count}x {model}' / '{count} {noun}s' / '{model}' label."""
    if model:
        prefix = f"{count}x " if count else ""
        return f"{prefix}{model}".strip()
    if count:
        return f"{count} {noun}s"
    return None


def _gpu_combo(raw: dict) -> str | None:
    """GPU label with per-GPU memory folded in, e.g. '8x MI300X · 192 GiB'."""
    label = _combo(raw.get("gpu_count"), raw.get("gpu_model"), "GPU")
    mem = raw.get("gpu_memory_gib")
    if label and mem:
        # Trim a trailing .0 so 192.0 -> 192.
        mem_str = f"{mem:g}"
        return f"{label} · {mem_str} GiB"
    return label


# How many firmware components to surface in the compact tooltip.
_TOOLTIP_FIRMWARE_LIMIT = 4
# Firmware "versions" that carry no information — skipped in the tooltip.
_FW_PLACEHOLDERS = {"", "-", "n/a", "na", "none", "not present", "unknown"}


def tooltip_fields(raw: dict) -> dict:
    """Compact inventory subset for the Data Hall tooltip, from a plain dict.

    Shared by :meth:`Inventory.tooltip` and the Data Hall route (which reads the
    persisted ``inventory_json`` dict), so the two never drift.
    """
    out = {
        "model": raw.get("model"),
        "serial": raw.get("serial"),
        "asset_tag": raw.get("asset_tag"),
        "health": raw.get("health"),
        "power_state": raw.get("power_state"),
        "cpu": _combo(raw.get("processor_count"), raw.get("processor_model"), "CPU"),
        "gpu": _gpu_combo(raw),
        "memory_gib": raw.get("memory_gib"),
        "bios_version": raw.get("bios_version"),
        "bmc_firmware": raw.get("bmc_firmware"),
        "firmware": _tooltip_firmware(
            raw.get("firmware"), raw.get("bios_version"), raw.get("bmc_firmware")
        ),
        "height_u": raw.get("height_u"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _tooltip_firmware(
    firmware: object, bios: str | None = None, bmc: str | None = None
) -> dict | None:
    """A few *additional* component versions for the tooltip, or None.

    Excludes anything already shown as its own row — the primary BIOS/BMC (and
    their Backup/Golden/Staging variants) and any entry whose value duplicates
    the dedicated BIOS/BMC rows — plus placeholder values like "Not Present", so
    firmware never appears twice.
    """
    if not isinstance(firmware, dict) or not firmware:
        return None
    dedicated = {v for v in (bios, bmc) if v}
    out: dict[str, str] = {}
    for name, ver in firmware.items():
        nm, v = str(name), str(ver)
        if v.strip().casefold() in _FW_PLACEHOLDERS:
            continue
        if v in dedicated or nm.casefold().startswith(("bios", "bmc")):
            continue  # already covered by the BIOS / BMC rows
        out[nm] = v
        if len(out) >= _TOOLTIP_FIRMWARE_LIMIT:
            break
    return out or None
