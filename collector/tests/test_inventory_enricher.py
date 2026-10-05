# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the inventory enricher: staleness logic + persistence."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from src.config import InventoryConfig, LocationConfig
from src.inventory.enricher import InventoryEnricher, apply_inventory
from src.inventory.models import Inventory
from src.location.models import Placement
from src.redfish.client import RedfishClient

# A positional name pattern so target names parse predictably for reconciliation.
_CFG = LocationConfig(name_patterns=[r"^[^-]+-(?P<hall>[^-]+)-(?P<rack>[^-]+)-(?P<rack_u>\d+)$"])


def _due(updated_at, refresh_hours=24):
    enr = InventoryEnricher(None, InventoryConfig(refresh_interval_hours=refresh_hours), _CFG)
    return enr._is_due(SimpleNamespace(inventory_updated_at=updated_at))


def test_is_due_never_pulled():
    assert _due(None) is True


def test_is_due_fresh_is_skipped():
    assert _due(datetime.utcnow() - timedelta(hours=1)) is False


def test_is_due_stale_is_repulled():
    assert _due(datetime.utcnow() - timedelta(hours=48)) is True


def test_is_due_one_time_only_never_refreshes():
    # refresh_interval_hours = 0 -> pull once, never again.
    assert _due(datetime.utcnow() - timedelta(hours=100), refresh_hours=0) is False


def _inv(**overrides):
    base = {
        "model": "UBB8",
        "serial": "S1",
        "power_state": "On",
        "height_mm": 266.7,
        "height_u": 6,
    }
    base.update(overrides)
    return Inventory(**base)


async def test_apply_inventory_persists_and_matches(repo):
    target = await repo.create_target(
        name="cl-hallz-rackq-07", host="10.0.0.1", username="u", password="p"
    )
    inv = _inv(
        placement=Placement(
            hall="hallz", rack="rackq", rack_u=7, unit_type="EIA_310", source="redfish"
        )
    )
    await apply_inventory(repo, target, inv, _CFG)

    updated = await repo.get_target(target.id)
    assert updated.inventory_source == "redfish"
    assert updated.inventory_updated_at is not None
    payload = json.loads(updated.inventory_json)
    assert payload["model"] == "UBB8" and payload["bmc_location"]["rack"] == "rackq"
    # name (hallz/rackq) == BMC (hallz/rackq)
    assert updated.location_check == "match"
    # BMC placement + chassis height applied.
    assert updated.loc_rack == "rackq" and updated.loc_rack_u == 7
    assert updated.loc_rack_u_height == 6 and updated.loc_source == "redfish"


async def test_apply_inventory_records_mismatch(repo):
    target = await repo.create_target(
        name="cl-hallz-rackq-07", host="10.0.0.1", username="u", password="p"
    )
    inv = _inv(placement=Placement(hall="otherhall", rack="rackq", rack_u=7, source="redfish"))
    await apply_inventory(repo, target, inv, _CFG)
    updated = await repo.get_target(target.id)
    assert updated.location_check == "mismatch"


async def test_apply_inventory_does_not_clobber_manual(repo):
    target = await repo.create_target(
        name="cl-hallz-rackq-07", host="10.0.0.1", username="u", password="p"
    )
    # Operator manually placed it elsewhere.
    await repo.set_target_location(
        target.id, Placement(rack="99", rack_u=1, height_u=3, source="manual"), force=True
    )
    target = await repo.get_target(target.id)
    inv = _inv(placement=Placement(hall="hallz", rack="rackq", rack_u=7, source="redfish"))
    await apply_inventory(repo, target, inv, _CFG)

    updated = await repo.get_target(target.id)
    # Inventory still recorded...
    assert updated.inventory_json is not None
    # ...but the manual placement/height is preserved.
    assert updated.loc_rack == "99" and updated.loc_source == "manual"
    assert updated.loc_rack_u_height == 3


# ---- _build_client / _enrich_one / _run_pass / run ----


class _FakeClient:
    def __init__(self, ok=True, reports=None):
        self.ok = ok
        self._reports = reports  # Member URIs for the MetricReports collection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def test_connection(self):
        return (self.ok, "ok" if self.ok else "unreachable")

    async def get_metric_report(self, uri):
        if uri.endswith("/MetricReports") and self._reports is not None:
            members = [{"@odata.id": u} for u in self._reports]
            return SimpleNamespace(success=True, content=json.dumps({"Members": members}).encode())
        return SimpleNamespace(success=False, content=b"")


def _enricher(repo):
    return InventoryEnricher(repo, InventoryConfig(), _CFG)


async def test_build_client_direct(repo):
    target = await repo.create_target(name="d", host="h", username="u", password="p")
    client = _enricher(repo)._build_client(target)
    assert isinstance(client, RedfishClient)


async def test_enrich_one_unreachable_skips(repo, monkeypatch):
    target = await repo.create_target(name="u", host="h", username="u", password="p")
    enr = _enricher(repo)
    monkeypatch.setattr(enr, "_build_client", lambda t: _FakeClient(ok=False))
    await enr._enrich_one(target)
    assert (await repo.get_target(target.id)).inventory_updated_at is None  # not persisted


async def test_enrich_one_reachable_persists(repo, monkeypatch):
    target = await repo.create_target(name="r", host="h", username="u", password="p")
    enr = _enricher(repo)
    monkeypatch.setattr(enr, "_build_client", lambda t: _FakeClient(ok=True))

    async def fake_collect(client, unit_type, gpu_count):
        return Inventory(model="UBB8", height_u=6)

    monkeypatch.setattr("src.inventory.enricher.collect_inventory", fake_collect)
    await enr._enrich_one(target)
    updated = await repo.get_target(target.id)
    assert updated.inventory_json is not None and updated.inventory_source == "redfish"


async def test_enrich_one_discovers_reports_in_auto_mode(repo, monkeypatch):
    target = await repo.create_target(name="auto", host="h", username="u", password="p")
    assert target.metric_discovery_mode == "auto"  # column default
    enr = _enricher(repo)
    reports = [
        "/redfish/v1/TelemetryService/MetricReports/All",
        "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0",
    ]
    monkeypatch.setattr(enr, "_build_client", lambda t: _FakeClient(ok=True, reports=reports))

    async def fake_collect(client, unit_type, gpu_count):
        return None  # telemetry-only BMC: no chassis inventory, discovery still runs

    monkeypatch.setattr("src.inventory.enricher.collect_inventory", fake_collect)
    await enr._enrich_one(target)

    discovered = repo.get_discovered_metric_reports(await repo.get_target(target.id))
    assert discovered is not None
    assert {r["report_type"] for r in discovered} == {"All", "OAM_ProcessorMetrics_0"}
    assert discovered[-1]["report_type"] == "All"  # aggregate ordered last


async def test_enrich_one_manual_mode_skips_discovery(repo, monkeypatch):
    target = await repo.create_target(
        name="manual", host="h", username="u", password="p", metric_discovery_mode="manual"
    )
    enr = _enricher(repo)
    reports = ["/redfish/v1/TelemetryService/MetricReports/All"]
    monkeypatch.setattr(enr, "_build_client", lambda t: _FakeClient(ok=True, reports=reports))

    async def fake_collect(client, unit_type, gpu_count):
        return Inventory(model="M")

    monkeypatch.setattr("src.inventory.enricher.collect_inventory", fake_collect)
    await enr._enrich_one(target)
    assert (await repo.get_target(target.id)).discovered_reports is None  # not discovered


async def test_run_pass_dispatches_due(repo, monkeypatch):
    await repo.create_target(name="a", host="ha", username="u", password="p")
    await repo.create_target(name="b", host="hb", username="u", password="p")
    enr = _enricher(repo)
    seen = []
    monkeypatch.setattr(enr, "_enrich_one", lambda t: _noop(seen, t))
    await enr._run_pass()
    assert len(seen) == 2  # both are never-pulled -> due


def test_needs_discovery_true_when_auto_and_undiscovered():
    t = SimpleNamespace(metric_discovery_mode="auto", discovered_reports=None)
    assert InventoryEnricher._needs_discovery(t) is True


def test_needs_discovery_false_when_already_discovered():
    t = SimpleNamespace(metric_discovery_mode="auto", discovered_reports="[{}]")
    assert InventoryEnricher._needs_discovery(t) is False


def test_needs_discovery_false_when_manual():
    t = SimpleNamespace(metric_discovery_mode="manual", discovered_reports=None)
    assert InventoryEnricher._needs_discovery(t) is False


async def test_run_pass_includes_discovery_needed_despite_fresh_inventory(repo, monkeypatch):
    # Inventory fresh (not due) but no discovered reports yet -> still processed.
    await repo.create_target(name="a", host="ha", username="u", password="p")
    enr = _enricher(repo)
    monkeypatch.setattr(enr, "_is_due", lambda t: False)
    seen = []
    monkeypatch.setattr(enr, "_enrich_one", lambda t: _noop(seen, t))
    await enr._run_pass()
    assert len(seen) == 1


async def test_run_loops_once_then_cancels(monkeypatch):
    enr = InventoryEnricher(None, InventoryConfig(), _CFG)
    calls = []

    async def fake_pass():
        calls.append(1)

    async def cancel_sleep(_s):
        raise asyncio.CancelledError()

    monkeypatch.setattr(enr, "_run_pass", fake_pass)
    monkeypatch.setattr(asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await enr.run()
    assert calls == [1]


async def _noop(seen, t):
    seen.append(t)


# ---- height-only write goes through the precedence guard ----


async def test_apply_inventory_height_only_updates_non_manual(repo):
    target = await repo.create_target(name="ho", host="x", username="u", password="p")
    await repo.set_target_location(
        target.id, Placement(rack="r", rack_u=1, height_u=1, source="hostname")
    )
    target = await repo.get_target(target.id)
    await apply_inventory(repo, target, _inv(height_u=6, placement=None), _CFG)
    assert (await repo.get_target(target.id)).loc_rack_u_height == 6  # redfish > hostname


async def test_apply_inventory_height_only_preserves_manual(repo):
    target = await repo.create_target(name="hm", host="x", username="u", password="p")
    await repo.set_target_location(
        target.id, Placement(rack="99", rack_u=1, height_u=3, source="manual"), force=True
    )
    target = await repo.get_target(target.id)
    await apply_inventory(repo, target, _inv(height_u=6, placement=None), _CFG)
    # manual height is not clobbered by the redfish-sourced chassis height.
    assert (await repo.get_target(target.id)).loc_rack_u_height == 3
