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
"""Collect basic inventory from a BMC via standard Redfish GETs.

All reads are plain authenticated GETs (no tasks/POSTs). Everything is
best-effort: a missing endpoint or field is skipped and never raises, so a
partially-responsive BMC still yields whatever inventory it does expose.
"""

from __future__ import annotations

import json
import logging

from ..location.redfish_location import placement_from_location
from .models import Inventory

logger = logging.getLogger(__name__)

_CHASSIS = "/redfish/v1/Chassis"
_SYSTEMS = "/redfish/v1/Systems"
_MANAGERS = "/redfish/v1/Managers"
_FIRMWARE_INVENTORY = "/redfish/v1/UpdateService/FirmwareInventory"
_MAX_MEMBERS = 16
# FirmwareInventory can list many softwarable components; bound the per-member
# GETs and the stored map so inventory_json stays small.
_MAX_FIRMWARE = 24
# A system's Processors collection may list CPUs + GPUs/accelerators; bound the
# per-member GETs (UBB8 is ~8 GPUs + a couple of CPUs).
_MAX_PROCESSORS = 32
_GPU_TYPES = {"GPU", "Accelerator", "FPGA"}


async def _get_json(client, uri: str) -> dict | None:
    """Authenticated JSON GET (reuses the client's generic report fetch)."""
    try:
        resp = await client.get_metric_report(uri)
        if not resp or not resp.success:
            return None
        data = json.loads(resp.content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
        logger.debug("Inventory GET %s failed: %s: %s", uri, type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001 — best-effort; never break the caller
        logger.debug("Inventory GET %s error: %s: %s", uri, type(e).__name__, e)
        return None


async def _first_member(client, collection_uri: str) -> dict | None:
    """GET a collection and return the first member resource that resolves."""
    collection = await _get_json(client, collection_uri)
    if not collection:
        return None
    for member in (collection.get("Members") or [])[:_MAX_MEMBERS]:
        uri = member.get("@odata.id") if isinstance(member, dict) else None
        if not uri:
            continue
        resource = await _get_json(client, uri)
        if resource:
            return resource
    return None


def _num(value):
    """Return an int/float as-is, or None for anything non-numeric (incl. bool)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _gpu_memory_gib(proc: dict) -> float | None:
    """Representative GPU memory (GiB) from a Processor's ``ProcessorMemory``.

    Standard Redfish exposes accelerator memory as ``Processor.ProcessorMemory``
    (array of {CapacityMiB, ...}); sum it. No standard single HBM-capacity
    property exists, so this is best-effort and returns None when absent.
    """
    pmem = proc.get("ProcessorMemory")
    if not isinstance(pmem, list):
        return None
    total_mib = 0.0
    for bank in pmem:
        cap = _num(bank.get("CapacityMiB")) if isinstance(bank, dict) else None
        if cap:
            total_mib += cap
    return round(total_mib / 1024.0, 1) if total_mib else None


async def _count_gpus(client, processors_uri: str) -> tuple[int | None, str | None, float | None]:
    """Count GPU/accelerator processors; capture a representative model + memory."""
    collection = await _get_json(client, processors_uri)
    if not collection:
        return None, None, None
    count = 0
    model: str | None = None
    memory_gib: float | None = None
    for member in (collection.get("Members") or [])[:_MAX_PROCESSORS]:
        uri = member.get("@odata.id") if isinstance(member, dict) else None
        if not uri:
            continue
        proc = await _get_json(client, uri)
        if not proc:
            continue
        if proc.get("ProcessorType") in _GPU_TYPES:
            count += 1
            if model is None:
                model = proc.get("Model") or proc.get("Manufacturer")
            if memory_gib is None:
                memory_gib = _gpu_memory_gib(proc)
    return (count or None), model, memory_gib


async def _collect_firmware(client) -> dict | None:
    """Component -> version map from ``/redfish/v1/UpdateService/FirmwareInventory``.

    Best-effort: returns None when the service/collection is absent or empty.
    """
    collection = await _get_json(client, _FIRMWARE_INVENTORY)
    if not collection:
        return None
    firmware: dict[str, str] = {}
    for member in (collection.get("Members") or [])[:_MAX_FIRMWARE]:
        uri = member.get("@odata.id") if isinstance(member, dict) else None
        if not uri:
            continue
        item = await _get_json(client, uri)
        if not item:
            continue
        version = item.get("Version")
        name = item.get("Name") or item.get("Id")
        # Only keep scalar versions — some BMCs expose aggregate members whose
        # "Version" is a nested list/dict, which must not be stringified into the
        # tooltip.
        if name and isinstance(version, str | int | float) and not isinstance(version, bool):
            firmware[str(name)] = str(version)
    return firmware or None


async def collect_inventory(
    client, default_unit_type: str = "EIA_310", default_gpu_count: int | None = None
) -> Inventory | None:
    """Read basic inventory from a connected RedfishClient, or None if nothing.

    Pulls from the first populated member of ``/redfish/v1/Chassis`` (hardware +
    location + height), ``/redfish/v1/Systems`` (CPU/GPU/memory/BIOS/power), and
    ``/redfish/v1/Managers`` (BMC firmware). When the BMC does not enumerate GPUs
    as Redfish Processors, ``default_gpu_count`` (the configured per-system GPU
    count) is used so the tooltip still reflects the expected GPUs.
    """
    inv = Inventory()

    chassis = await _first_member(client, _CHASSIS)
    if chassis:
        inv.manufacturer = chassis.get("Manufacturer")
        inv.model = chassis.get("Model")
        inv.sku = chassis.get("SKU")
        inv.serial = chassis.get("SerialNumber")
        inv.part_number = chassis.get("PartNumber")
        inv.chassis_type = chassis.get("ChassisType")
        inv.power_state = chassis.get("PowerState")
        inv.asset_tag = chassis.get("AssetTag") or None
        if isinstance(chassis.get("Status"), dict):
            inv.health = chassis["Status"].get("Health")
        inv.height_mm = _num(chassis.get("HeightMm"))
        location = chassis.get("Location")
        placement = placement_from_location(location, default_unit_type)
        if placement.has_location():
            placement.source = "redfish"
            inv.placement = placement
            if placement.unit_type:
                inv.unit_type = placement.unit_type

    system = await _first_member(client, _SYSTEMS)
    if system:
        inv.manufacturer = inv.manufacturer or system.get("Manufacturer")
        inv.model = inv.model or system.get("Model")
        inv.serial = inv.serial or system.get("SerialNumber")
        inv.part_number = inv.part_number or system.get("PartNumber")
        inv.sku = inv.sku or system.get("SKU")
        inv.bios_version = system.get("BiosVersion")
        inv.power_state = inv.power_state or system.get("PowerState")
        inv.asset_tag = inv.asset_tag or system.get("AssetTag") or None
        if inv.health is None and isinstance(system.get("Status"), dict):
            inv.health = system["Status"].get("Health")
        proc = system.get("ProcessorSummary")
        if isinstance(proc, dict):
            inv.processor_count = _num(proc.get("Count"))
            inv.processor_model = proc.get("Model")
        mem = system.get("MemorySummary")
        if isinstance(mem, dict):
            inv.memory_gib = _num(mem.get("TotalSystemMemoryGiB"))
        processors = system.get("Processors")
        proc_uri = processors.get("@odata.id") if isinstance(processors, dict) else None
        if proc_uri:
            inv.gpu_count, inv.gpu_model, inv.gpu_memory_gib = await _count_gpus(client, proc_uri)

    manager = await _first_member(client, _MANAGERS)
    if manager:
        inv.bmc_firmware = manager.get("FirmwareVersion")

    inv.firmware = await _collect_firmware(client)

    # Fall back to the configured per-system GPU count when the BMC doesn't
    # enumerate GPUs as Redfish Processors (common — GPUs are often only on the
    # telemetry/OAM path).
    if inv.gpu_count is None and default_gpu_count:
        inv.gpu_count = default_gpu_count

    inv.derive_height_u()
    return inv if inv.has_data() else None
