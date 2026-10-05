# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for Redfish inventory collection (standard GETs)."""

import json
from types import SimpleNamespace

from src.inventory import collect_inventory
from src.inventory.models import tooltip_fields


class _FakeClient:
    """Stand-in for RedfishClient.get_metric_report (a JSON GET)."""

    def __init__(self, responses: dict):
        self._responses = responses

    async def get_metric_report(self, uri: str):
        body = self._responses.get(uri)
        if body is None:
            return SimpleNamespace(success=False, content=b"")
        return SimpleNamespace(success=True, content=json.dumps(body).encode())


def _full_fleet():
    return {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {
            "Manufacturer": "ACME",
            "Model": "UBB8-Node",
            "SKU": "SKU1",
            "SerialNumber": "SN123",
            "PartNumber": "PN9",
            "ChassisType": "RackMount",
            "PowerState": "On",
            "HeightMm": 266.7,
            "AssetTag": "AT-42",
            "Status": {"Health": "OK"},
            "Location": {
                "Placement": {
                    "Rack": "a11",
                    "Row": "North",
                    "RackOffset": 4,
                    "RackOffsetUnits": "EIA_310",
                },
                "PostalAddress": {"Room": "odcdh3"},
            },
        },
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
        "/redfish/v1/Systems/1": {
            "BiosVersion": "1.2.3",
            "PowerState": "On",
            "ProcessorSummary": {"Count": 2, "Model": "EPYC"},
            "MemorySummary": {"TotalSystemMemoryGiB": 1536},
            "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
        },
        "/redfish/v1/Systems/1/Processors": {
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1/Processors/CPU0"},
                {"@odata.id": "/redfish/v1/Systems/1/Processors/GPU0"},
                {"@odata.id": "/redfish/v1/Systems/1/Processors/GPU1"},
            ]
        },
        "/redfish/v1/Systems/1/Processors/CPU0": {"ProcessorType": "CPU", "Model": "EPYC"},
        "/redfish/v1/Systems/1/Processors/GPU0": {
            "ProcessorType": "GPU",
            "Model": "UBB8-GPU",
            "ProcessorMemory": [{"CapacityMiB": 196608}],  # 192 GiB HBM
        },
        "/redfish/v1/Systems/1/Processors/GPU1": {
            "ProcessorType": "Accelerator",
            "Model": "UBB8-GPU",
            "ProcessorMemory": [{"CapacityMiB": 196608}],
        },
        "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        "/redfish/v1/Managers/1": {"FirmwareVersion": "bmc-5.6"},
        "/redfish/v1/UpdateService/FirmwareInventory": {
            "Members": [
                {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BMC"},
                {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
                {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/CPLD"},
            ]
        },
        "/redfish/v1/UpdateService/FirmwareInventory/BMC": {"Name": "BMC", "Version": "5.6"},
        "/redfish/v1/UpdateService/FirmwareInventory/BIOS": {"Name": "BIOS", "Version": "1.2.3"},
        "/redfish/v1/UpdateService/FirmwareInventory/CPLD": {"Id": "MB_CPLD", "Version": "A.1"},
    }


async def test_collect_full_inventory():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv is not None
    d = inv.to_dict()
    assert d["manufacturer"] == "ACME" and d["model"] == "UBB8-Node"
    assert d["serial"] == "SN123" and d["bios_version"] == "1.2.3"
    assert d["processor_count"] == 2 and d["processor_model"] == "EPYC"
    assert d["memory_gib"] == 1536 and d["bmc_firmware"] == "bmc-5.6"
    assert d["height_mm"] == 266.7


async def test_height_u_derived_from_height_mm():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv.height_u == 6  # round(266.7 / 44.45)


async def test_placement_extracted_for_reconcile():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv.placement is not None
    assert inv.placement.rack == "a11" and inv.placement.hall == "odcdh3"
    assert inv.placement.source == "redfish"


async def test_gpu_inventory_counted():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv.gpu_count == 2  # one GPU + one Accelerator
    assert inv.gpu_model == "UBB8-GPU"
    assert inv.gpu_memory_gib == 192.0  # from ProcessorMemory CapacityMiB


async def test_gpu_memory_absent_degrades_silently():
    fleet = _full_fleet()
    fleet["/redfish/v1/Systems/1/Processors/GPU0"] = {"ProcessorType": "GPU", "Model": "UBB8-GPU"}
    fleet["/redfish/v1/Systems/1/Processors/GPU1"] = {
        "ProcessorType": "Accelerator",
        "Model": "UBB8-GPU",
    }
    inv = await collect_inventory(_FakeClient(fleet))
    assert inv.gpu_count == 2 and inv.gpu_memory_gib is None
    assert inv.tooltip()["gpu"] == "2x UBB8-GPU"  # no memory suffix


async def test_firmware_inventory_collected():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv.firmware == {"BMC": "5.6", "BIOS": "1.2.3", "MB_CPLD": "A.1"}


async def test_firmware_absent_returns_none():
    fleet = _full_fleet()
    del fleet["/redfish/v1/UpdateService/FirmwareInventory"]
    inv = await collect_inventory(_FakeClient(fleet))
    assert inv.firmware is None
    assert "firmware" not in inv.tooltip()


async def test_firmware_skips_non_scalar_version():
    # Some BMCs expose an aggregate member whose Version is a nested list/dict —
    # it must be skipped, not stringified into the inventory.
    fleet = _full_fleet()
    fleet["/redfish/v1/UpdateService/FirmwareInventory"]["Members"].append(
        {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Agg"}
    )
    fleet["/redfish/v1/UpdateService/FirmwareInventory/Agg"] = {
        "Name": "Software Inventory",
        "Version": [{"Name": "x", "Version": "1"}],
    }
    inv = await collect_inventory(_FakeClient(fleet))
    assert "Software Inventory" not in inv.firmware
    assert inv.firmware["BMC"] == "5.6"  # scalar versions still collected


async def test_gpu_count_falls_back_to_config():
    # BMC with no GPU processors, but a configured per-system GPU count.
    resp = {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {"Model": "M"},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
        "/redfish/v1/Systems/1": {"ProcessorSummary": {"Count": 2, "Model": "EPYC"}},
    }
    inv = await collect_inventory(_FakeClient(resp), default_gpu_count=8)
    assert inv.gpu_count == 8 and inv.gpu_model is None
    assert inv.tooltip()["gpu"] == "8 GPUs"


async def test_health_and_asset_tag_collected():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    assert inv.health == "OK" and inv.asset_tag == "AT-42"


async def test_tooltip_subset():
    inv = await collect_inventory(_FakeClient(_full_fleet()))
    tip = inv.tooltip()
    assert tip["cpu"] == "2x EPYC"
    assert tip["gpu"] == "2x UBB8-GPU · 192 GiB"  # GPU memory folded in
    assert tip["health"] == "OK" and tip["asset_tag"] == "AT-42"
    assert tip["bios_version"] == "1.2.3"
    assert tip["memory_gib"] == 1536 and tip["height_u"] == 6
    # Firmware tooltip shows only *additional* components — BIOS/BMC are their own
    # rows, so they're not repeated here (no double occurrence).
    assert tip["firmware"] == {"MB_CPLD": "A.1"}
    assert "serial" in tip and "part_number" not in tip  # compact subset only


async def test_partial_only_chassis():
    resp = {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {"Model": "M", "HeightMm": 88.9},
    }
    inv = await collect_inventory(_FakeClient(resp))
    assert inv is not None
    assert inv.model == "M" and inv.height_u == 2  # round(88.9/44.45)
    assert inv.bmc_firmware is None and inv.processor_count is None


async def test_no_data_returns_none():
    assert await collect_inventory(_FakeClient({})) is None


def test_tooltip_firmware_capped_to_four():
    raw = {"model": "M", "firmware": {f"C{i}": str(i) for i in range(6)}}
    tip = tooltip_fields(raw)
    assert len(tip["firmware"]) == 4  # compact subset only


def test_tooltip_firmware_no_double_occurrence():
    # Real-world shape: FirmwareInventory repeats BIOS/BMC (shown as their own
    # rows) plus "Not Present" placeholder slots. None of those should appear in
    # the firmware list — only the genuinely-additional components.
    raw = {
        "bios_version": "Ver 1.8",
        "bmc_firmware": "11.06",
        "firmware": {
            "BMC": "11.06",  # dup of bmc_firmware (also starts "bmc")
            "BIOS": "Ver 1.8",  # dup of bios_version (also starts "bios")
            "BMC Backup": "Not Present",  # placeholder
            "BIOS Golden": "Ver 1.4",  # BIOS variant -> excluded
            "CPLD Motherboard": "F2.65",  # additional component -> kept
            "Power Supply 1": "REV1.0",  # additional component -> kept
        },
    }
    fw = tooltip_fields(raw)["firmware"]
    assert fw == {"CPLD Motherboard": "F2.65", "Power Supply 1": "REV1.0"}


def test_tooltip_gpu_memory_without_count_or_model():
    # Memory present but no model/count -> no GPU line to attach it to.
    assert "gpu" not in tooltip_fields({"gpu_memory_gib": 192.0})


async def test_bool_fields_not_misread_as_numbers():
    resp = {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {"Model": "M", "HeightMm": True},  # bogus bool
    }
    inv = await collect_inventory(_FakeClient(resp))
    assert inv is not None and inv.height_mm is None and inv.height_u is None
