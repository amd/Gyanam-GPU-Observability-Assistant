# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the Data Hall routes (render, placement, clear, resolve)."""

from types import SimpleNamespace

from src.api.csrf import generate_csrf_token
from src.api.routes.datahall import _assign_slots, _build_layout, _build_view


async def _make_target(repo, name="sys1", host="plain-host"):
    return await repo.create_target(name=name, host=host, username="u", password="p")


def _fake_target(**overrides):
    base = {
        "id": 1,
        "name": "n",
        "host": "h",
        "last_poll_status": "success",
        "loc_site": None,
        "loc_hall": None,
        "loc_row": None,
        "loc_rack": None,
        "loc_rack_u": None,
        "loc_rack_u_height": None,
        "loc_unit_type": None,
        "loc_source": None,
        "inventory_json": None,
        "location_check": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- Pure view-model logic ----


def test_assign_slots_collision_and_floating():
    systems = [
        {"name": "a", "rack_u": 12, "height": 2},  # occupies 12,13
        {"name": "c", "rack_u": 12, "height": 1},  # collides -> bumped to 14
        {"name": "b", "rack_u": None, "height": 1},  # floating -> lowest free (1)
    ]
    occupied = _assign_slots(systems, rack_height=42)
    starts = {s["name"]: s["start_u"] for s in systems}
    assert starts["a"] == 12 and occupied[13]["name"] == "a"
    assert starts["c"] == 14
    assert starts["b"] == 1


def test_assign_slots_clamps_tall_system_to_fit():
    # A 6U system whose slot would push it past the top is clamped down to fit.
    systems = [{"name": "ubb", "rack_u": 40, "height": 6}]
    _assign_slots(systems, rack_height=42)
    assert systems[0]["start_u"] == 37  # 42 - 6 + 1


def test_assign_slots_overflow_when_taller_than_rack():
    systems = [{"name": "huge", "rack_u": 1, "height": 50}]
    _assign_slots(systems, rack_height=42)
    assert systems[0]["start_u"] is None


def test_assign_slots_default_height_applies():
    # No explicit height -> default form factor (6U) is used.
    systems = [{"name": "n", "rack_u": 1, "height": None}]
    occ = _assign_slots(systems, rack_height=42, default_height=6)
    assert systems[0]["start_u"] == 1
    assert occ[6]["name"] == "n" and 7 not in occ  # spans U1..6


def test_build_view_groups_and_splits_unplaced():
    targets = [
        _fake_target(id=1, name="a", loc_hall="2", loc_row="3", loc_rack="05", loc_rack_u=12),
        _fake_target(id=2, name="b", loc_hall="2", loc_row="3", loc_rack="05", loc_rack_u=None),
        _fake_target(
            id=3, name="c", loc_rack="  ", last_poll_status="error"
        ),  # blank rack -> unplaced
        _fake_target(id=4, name="d"),  # fully unplaced
    ]
    halls, unplaced, stats = _build_view(targets, rack_height=42)
    assert stats == {"total": 4, "placed": 2, "unplaced": 2, "halls": 1, "racks": 1}
    rack = halls[0]["rows"][0]["racks"][0]
    assert rack["name"] == "05"
    starts = {s["system"]["name"]: s["u"] for s in rack["slots"] if s["is_start"]}
    assert starts == {"a": 12, "b": 1}
    assert {u["name"] for u in unplaced} == {"c", "d"}


def test_build_view_sorts_racks_naturally():
    # Racks "2", "9", "10" must order numerically, not lexically ("10" last).
    targets = [
        _fake_target(id=i, name=f"s{r}", loc_hall="1", loc_row="1", loc_rack=r, loc_rack_u=1)
        for i, r in enumerate(["10", "2", "9"], start=1)
    ]
    halls, _unplaced, _stats = _build_view(targets, rack_height=42)
    rack_names = [rack["name"] for rack in halls[0]["rows"][0]["racks"]]
    assert rack_names == ["2", "9", "10"]


def test_build_view_uses_unassigned_placeholders():
    # Rack known but hall/row absent -> grouped under the Unassigned placeholders.
    targets = [_fake_target(id=1, name="a", loc_rack="7", loc_rack_u=1)]
    halls, _unplaced, _stats = _build_view(targets, rack_height=42)
    assert halls[0]["name"] == "Unassigned Hall"
    assert halls[0]["rows"][0]["name"] == "Unassigned Row"


# ---- 3D layout model ----


def test_build_layout_positions_and_grouping():
    targets = [
        _fake_target(
            id=1,
            name="a",
            loc_hall="2",
            loc_row="3",
            loc_rack="05",
            loc_rack_u=12,
            loc_rack_u_height=2,
        ),
        _fake_target(
            id=2, name="b", loc_hall="2", loc_row="3", loc_rack="06", last_poll_status="error"
        ),
        _fake_target(id=3, name="c"),  # unplaced
    ]
    layout = _build_layout(targets, rack_height=42, default_unit_type="EIA_310")
    assert layout["halls"] == ["2"]
    assert layout["rack_height_u"] == 42
    assert layout["default_unit_type"] == "EIA_310"
    assert {r["rack"] for r in layout["racks"]} == {"05", "06"}

    r05 = next(r for r in layout["racks"] if r["rack"] == "05")
    r06 = next(r for r in layout["racks"] if r["rack"] == "06")
    # Two racks in the same row -> same z, stepped x.
    assert r05["z"] == r06["z"]
    assert r05["x"] != r06["x"]
    sys_a = r05["systems"][0]
    assert sys_a["name"] == "a" and sys_a["start_u"] == 12 and sys_a["height_u"] == 2
    assert sys_a["unit_type"] == "EIA_310"  # filled from default
    assert [u["name"] for u in layout["unplaced"]] == ["c"]
    assert layout["counts"]["placed"] == 2


def test_build_layout_json_serializable():
    import json

    targets = [_fake_target(id=1, name="a", loc_rack="9", loc_rack_u=1)]
    json.dumps(_build_layout(targets, 42, "EIA_310"))  # must not raise


def test_build_layout_drops_overflow_from_drawable_systems():
    # A system taller than the rack can't fit -> excluded from drawables and counted.
    targets = [
        _fake_target(
            id=1,
            name="big",
            loc_hall="1",
            loc_row="1",
            loc_rack="1",
            loc_rack_u=1,
            loc_rack_u_height=50,
        ),  # > 42U
    ]
    layout = _build_layout(targets, rack_height=42, default_unit_type="EIA_310")
    rack = layout["racks"][0]
    assert rack["systems"] == []
    assert rack["overflow"] == 1
    # The overflowing system is still surfaced (identified, with location) so it
    # is never invisible — not just reduced to a count.
    assert len(layout["overflow"]) == 1
    entry = layout["overflow"][0]
    assert entry["id"] == 1 and entry["name"] == "big" and entry["overflow"] is True
    assert "1 / 1 / 1" in entry["location"]


async def test_layout_endpoint_returns_json(client, repo):
    target = await _make_target(repo, name="srv", host="h1")
    await client.post(
        f"/datahall/api/{target.id}/placement",
        data={
            "hall": "2",
            "row": "3",
            "rack": "05",
            "rack_u": "10",
            "csrf_token": generate_csrf_token(),
        },
    )
    resp = await client.get("/datahall/api/layout")
    assert resp.status_code == 200
    body = resp.json()
    assert "racks" in body and "unplaced" in body and "halls" in body
    rack = next(r for r in body["racks"] if r["rack"] == "05")
    assert rack["systems"][0]["name"] == "srv"


async def test_layout_endpoint_requires_auth(noauth_client, repo):
    resp = await noauth_client.get("/datahall/api/layout")
    assert resp.status_code in (401, 403)


async def test_datahall_page_has_twin_scaffold(client, repo):
    await _make_target(repo)
    resp = await client.get("/datahall")
    assert resp.status_code == 200
    # three.js import map + module + scene host are present.
    assert "importmap" in resp.text
    assert "/static/vendor/three/three.module.min.js" in resp.text
    assert 'id="twin-canvas"' in resp.text
    assert "twin-layout-data" in resp.text


# ---- Integration render of a populated hall ----


async def test_datahall_renders_placed_system(client, repo):
    target = await _make_target(repo, name="paintme", host="plain-host")
    await client.post(
        f"/datahall/api/{target.id}/placement",
        data={
            "hall": "2",
            "row": "3",
            "rack": "05",
            "rack_u": "10",
            "csrf_token": generate_csrf_token(),
        },
    )
    resp = await client.get("/datahall")
    assert resp.status_code == 200
    assert "paintme" in resp.text
    assert "Rack 05" in resp.text


async def test_datahall_page_renders(client, repo):
    await _make_target(repo)
    resp = await client.get("/datahall")
    assert resp.status_code == 200
    assert "Data Hall" in resp.text


async def test_layout_carries_inventory_tooltip(client, repo):
    import json as _json
    from datetime import datetime

    target = await _make_target(repo, name="inv-sys", host="h")
    await client.post(
        f"/datahall/api/{target.id}/placement",
        data={
            "hall": "2",
            "row": "3",
            "rack": "05",
            "rack_u": "10",
            "csrf_token": generate_csrf_token(),
        },
    )
    await repo.set_target_inventory(
        target.id,
        inventory_json=_json.dumps(
            {
                "model": "UBB8-Node",
                "serial": "SN1",
                "power_state": "On",
                "processor_count": 2,
                "processor_model": "EPYC",
                "gpu_count": 8,
                "gpu_model": "UBB8-GPU",
                "memory_gib": 1536,
                "bmc_firmware": "bmc-5.6",
                "height_u": 6,
            }
        ),
        source="redfish",
        location_check="mismatch",
        updated_at=datetime.utcnow(),
    )
    resp = await client.get("/datahall/api/layout")
    assert resp.status_code == 200
    rack = next(r for r in resp.json()["racks"] if r["rack"] == "05")
    sys = rack["systems"][0]
    assert sys["location_check"] == "mismatch"
    inv = sys["inventory"]
    assert inv["model"] == "UBB8-Node" and inv["cpu"] == "2x EPYC"
    assert inv["gpu"] == "8x UBB8-GPU"
    assert inv["memory_gib"] == 1536 and inv["height_u"] == 6


async def test_datahall_requires_auth(noauth_client, repo):
    resp = await noauth_client.get("/datahall")
    assert resp.status_code != 200  # redirect to login / 401


async def test_set_placement_persists_as_manual(client, repo):
    target = await _make_target(repo)
    resp = await client.post(
        f"/datahall/api/{target.id}/placement",
        data={
            "hall": "2",
            "row": "3",
            "rack": "05",
            "rack_u": "12",
            "height": "2",
            "unit_type": "EIA_310",
            "csrf_token": generate_csrf_token(),
        },
    )
    assert resp.status_code == 200
    updated = await repo.get_target(target.id)
    assert updated.loc_rack == "05"
    assert updated.loc_rack_u == 12
    assert updated.loc_rack_u_height == 2
    assert updated.loc_source == "manual"


async def test_set_placement_missing_csrf_rejected(client, repo):
    target = await _make_target(repo)
    resp = await client.post(
        f"/datahall/api/{target.id}/placement",
        data={"rack": "05"},  # no csrf_token form field
    )
    assert resp.status_code in (400, 403, 422)


async def test_set_placement_invalid_rack_u(client, repo):
    target = await _make_target(repo)
    resp = await client.post(
        f"/datahall/api/{target.id}/placement",
        data={"rack": "05", "rack_u": "abc", "csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 400


async def test_set_placement_unknown_target_404(client, repo):
    resp = await client.post(
        "/datahall/api/999999/placement",
        data={"rack": "05", "csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 404


async def test_clear_placement_unplaces(client, repo):
    target = await _make_target(repo)
    await client.post(
        f"/datahall/api/{target.id}/placement",
        data={"rack": "05", "csrf_token": generate_csrf_token()},
    )
    resp = await client.post(
        f"/datahall/api/{target.id}/placement/clear",
        data={"csrf_token": generate_csrf_token()},
    )
    assert resp.status_code == 200
    updated = await repo.get_target(target.id)
    # Coordinates cleared; source becomes the "pinned" sentinel so auto-resolve
    # can't silently re-place it (sticky unplaced).
    assert updated.loc_rack is None and updated.loc_source == "pinned"


async def test_resolve_fills_from_hostname(client, repo):
    # Created directly via repo (no route-layer seed), so placement starts empty.
    target = await _make_target(repo, name="srv", host="srv-rack7-u2")
    assert (await repo.get_target(target.id)).loc_rack is None

    resp = await client.post("/datahall/api/resolve", data={"csrf_token": generate_csrf_token()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] >= 1
    updated = await repo.get_target(target.id)
    assert updated.loc_rack == "7"
    assert updated.loc_rack_u == 2
    assert updated.loc_source == "hostname"


async def test_resolve_uses_system_name_via_config_pattern(client, repo):
    # Location lives in the system NAME; the host is a bare BMC IP. This also
    # exercises the positional name_pattern shipped in config.yaml.
    target = await repo.create_target(
        name="demo-hallz-rackq-07", host="10.0.0.1", username="u", password="p"
    )
    await client.post("/datahall/api/resolve", data={"csrf_token": generate_csrf_token()})
    updated = await repo.get_target(target.id)
    # Everything before rack+slot becomes the hall name (shipped config pattern).
    assert updated.loc_hall == "demo-hallz"
    assert updated.loc_rack == "rackq"
    assert updated.loc_rack_u == 7
    assert updated.loc_rack_u_height == 4  # default form factor (4U)
    assert updated.loc_source == "hostname"


async def test_resolve_does_not_clobber_manual(client, repo):
    target = await _make_target(repo, name="srv", host="srv-rack7-u2")
    # Operator manually places it somewhere else first.
    await client.post(
        f"/datahall/api/{target.id}/placement",
        data={"rack": "99", "csrf_token": generate_csrf_token()},
    )
    await client.post("/datahall/api/resolve", data={"csrf_token": generate_csrf_token()})
    updated = await repo.get_target(target.id)
    assert updated.loc_rack == "99" and updated.loc_source == "manual"


# ---- (#4) row derivation from the rack label's alpha prefix ----


def test_derive_row_from_rack_prefix():
    from src.api.routes.datahall import _derive_row

    # Explicit row (hostname/Redfish) always wins.
    assert _derive_row("R3", "G12") == "R3"
    assert _derive_row("  ", "G12") == "G"  # blank row -> alpha prefix
    assert _derive_row(None, "n07") == "N"  # uppercased
    assert _derive_row(None, "aa10b") == "AA"  # leading run only
    assert _derive_row(None, "12") is None  # no alpha prefix
    assert _derive_row(None, "") is None


def test_build_view_groups_by_derived_row():
    # No loc_row, but rack labels encode the row in their letter prefix.
    targets = [
        _fake_target(id=1, name="a", loc_hall="h1", loc_rack="G12", loc_rack_u=6),
        _fake_target(id=2, name="b", loc_hall="h1", loc_rack="G16", loc_rack_u=10),
        _fake_target(id=3, name="c", loc_hall="h1", loc_rack="N07", loc_rack_u=6),
    ]
    halls, _unplaced, _stats = _build_view(targets, rack_height=48)
    rows = {r["name"] for r in halls[0]["rows"]}
    assert rows == {"G", "N"}
    g_row = next(r for r in halls[0]["rows"] if r["name"] == "G")
    assert {rk["name"] for rk in g_row["racks"]} == {"G12", "G16"}
