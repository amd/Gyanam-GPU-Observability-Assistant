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
"""Data Hall view — renders where monitored systems sit in the racks.

Groups systems by hall -> row -> rack and lays each rack out as a column of
rack-unit (U) slots. Placement comes from the persisted ``loc_*`` columns
(seeded automatically from the hostname / Redfish at registration, correctable
here). Systems with no rack fall into the "Unplaced" tray.
"""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...config import get_config
from ...inventory.models import tooltip_fields
from ...location import Placement
from ...location.hostname_parser import parse_system_location
from ...location.placement import (
    _RACK_PITCH_M,
    _ROW_PITCH_M,
    _UNKNOWN_HALL,
    _UNKNOWN_ROW,
    _assign_slots,
    _derive_row,
    _natural_key,
)
from ..auth import get_current_user
from ..csrf import generate_csrf_token, validate_csrf_token
from ..dependencies import get_repository

logger = logging.getLogger(__name__)

router = APIRouter()


def _inventory_tooltip(target) -> dict:
    """Compact inventory subset (for the 3D hover tooltip) from inventory_json."""
    if not target.inventory_json:
        return {}
    try:
        raw = json.loads(target.inventory_json)
    except (ValueError, TypeError):
        return {}
    # Shared formatter so the tooltip never drifts from Inventory.tooltip().
    return tooltip_fields(raw)


def _system_view(target) -> dict:
    """Flatten a Target into the fields the Data Hall template needs."""
    return {
        "id": target.id,
        "name": target.name,
        "host": target.host,
        "status": target.last_poll_status or "pending",
        "hall": target.loc_hall,
        "row": target.loc_row,
        "rack": target.loc_rack,
        "rack_u": target.loc_rack_u,
        "height": target.loc_rack_u_height,  # None -> caller applies the default
        "unit_type": target.loc_unit_type,
        "source": target.loc_source,
        "inventory": _inventory_tooltip(target),
        "location_check": target.location_check,
    }


def _build_view(
    targets, rack_height: int, default_height: int = 4
) -> tuple[list[dict], list[dict], dict]:
    """Build the nested hall/row/rack view model + the unplaced tray + stats."""
    placed: list = []
    unplaced: list = []
    for t in targets:
        (placed if (t.loc_rack and t.loc_rack.strip()) else unplaced).append(t)

    # hall -> row -> rack -> [systems]
    halls: dict[str, dict] = {}
    for t in placed:
        system = _system_view(t)
        hall_name = (t.loc_hall or "").strip() or _UNKNOWN_HALL
        rack_name = t.loc_rack.strip()
        row_name = _derive_row(t.loc_row, rack_name) or _UNKNOWN_ROW
        hall = halls.setdefault(hall_name, {"name": hall_name, "rows": {}})
        row = hall["rows"].setdefault(row_name, {"name": row_name, "racks": {}})
        rack = row["racks"].setdefault(rack_name, {"name": rack_name, "systems": []})
        rack["systems"].append(system)

    # Materialise sorted lists + per-rack slot layout.
    hall_list = []
    rack_count = 0
    for hall in sorted(halls.values(), key=lambda h: _natural_key(h["name"])):
        row_list = []
        for row in sorted(hall["rows"].values(), key=lambda r: _natural_key(r["name"])):
            rack_list = []
            for rack in sorted(row["racks"].values(), key=lambda r: _natural_key(r["name"])):
                rack_count += 1
                occupied = _assign_slots(rack["systems"], rack_height, default_height)
                slots = []
                for u in range(rack_height, 0, -1):
                    slot_system = occupied.get(u)
                    slots.append(
                        {
                            "u": u,
                            "system": slot_system,
                            "is_start": slot_system is not None and slot_system.get("start_u") == u,
                        }
                    )
                rack["slots"] = slots
                rack["overflow"] = [s for s in rack["systems"] if s.get("start_u") is None]
                rack_list.append(rack)
            row["racks"] = rack_list
            row_list.append(row)
        hall["rows"] = row_list
        hall_list.append(hall)

    unplaced_view = [_system_view(t) for t in unplaced]
    stats = {
        "total": len(targets),
        "placed": len(placed),
        "unplaced": len(unplaced_view),
        "halls": len(hall_list),
        "racks": rack_count,
    }
    return hall_list, unplaced_view, stats


def _build_layout(
    targets, rack_height: int, default_unit_type: str, default_height: int = 4
) -> dict:
    """Flat, JSON-serializable scene model for the 3D twin.

    Reuses :func:`_build_view` (grouping + ``_assign_slots`` resolution), then
    flattens to a list of racks with floor positions and the systems seated in
    them. Overflowing systems (that don't fit the rack) are dropped from the
    drawable list and surfaced as an ``overflow`` count.
    """
    hall_list, unplaced_view, stats = _build_view(targets, rack_height, default_height)

    racks: list[dict] = []
    overflow_systems: list[dict] = []
    for hall in hall_list:
        for row_index, row in enumerate(hall["rows"]):
            for rack_index, rack in enumerate(row["racks"]):
                systems = [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "host": s["host"],
                        "status": s["status"],
                        "start_u": s["start_u"],
                        "height_u": s.get("height") or default_height,
                        "unit_type": s.get("unit_type") or default_unit_type,
                        "inventory": s.get("inventory") or {},
                        "location_check": s.get("location_check"),
                    }
                    for s in rack["systems"]
                    if s.get("start_u") is not None
                ]
                # Systems that couldn't be seated (rack full, or taller than the
                # rack) would otherwise vanish from the scene entirely. Capture
                # them identified, with their intended location, so they stay
                # visible (surfaced in the tray + fallback, not just a count).
                for s in rack.get("overflow", []):
                    overflow_systems.append(
                        {
                            "id": s["id"],
                            "name": s["name"],
                            "host": s["host"],
                            "status": s["status"],
                            "overflow": True,
                            "location": f"{hall['name']} / {row['name']} / {rack['name']}",
                        }
                    )
                racks.append(
                    {
                        "hall": hall["name"],
                        "row": row["name"],
                        "rack": rack["name"],
                        "row_index": row_index,
                        "rack_index": rack_index,
                        "x": round(rack_index * _RACK_PITCH_M, 3),
                        "z": round(row_index * _ROW_PITCH_M, 3),
                        "systems": systems,
                        "overflow": len(rack.get("overflow", [])),
                    }
                )

    unplaced = [
        {"id": u["id"], "name": u["name"], "host": u["host"], "status": u["status"]}
        for u in unplaced_view
    ]

    return {
        "halls": [h["name"] for h in hall_list],
        "racks": racks,
        "unplaced": unplaced,
        "overflow": overflow_systems,
        "rack_height_u": rack_height,
        "default_height_u": default_height,
        "counts": stats,
        "default_unit_type": default_unit_type,
        "unknown_hall": _UNKNOWN_HALL,
        "unknown_row": _UNKNOWN_ROW,
    }


@router.get("", response_class=HTMLResponse)
async def datahall_page(request: Request, user: str = Depends(get_current_user)):
    """Render the data-hall visualization."""
    repository = get_repository()
    targets = await repository.get_all_targets()
    location_cfg = get_config().location
    rack_height = location_cfg.rack_height_u

    default_height = location_cfg.default_system_height_u
    layout = _build_layout(targets, rack_height, location_cfg.default_unit_type, default_height)
    # halls/unplaced/stats also feed the no-WebGL fallback list in the template.
    halls, unplaced, stats = _build_view(targets, rack_height, default_height)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="datahall.html",
        context={
            "user": user,
            "csrf_token": generate_csrf_token(),
            "layout": layout,
            "halls": halls,
            "unplaced": unplaced,
            "stats": stats,
            "rack_height": rack_height,
            "default_unit_type": location_cfg.default_unit_type,
            "unknown_hall": _UNKNOWN_HALL,
            "unknown_row": _UNKNOWN_ROW,
        },
    )


@router.get("/api/layout", summary="Data-hall 3D scene layout (JSON)")
async def datahall_layout(user: str = Depends(get_current_user)):
    """Return the current placement layout for the 3D twin.

    The client fetches this after each assignment and on a periodic poll to
    rebuild the scene without a full reload (preserving the camera) and to keep
    status colours live.
    """
    repository = get_repository()
    targets = await repository.get_all_targets()
    cfg = get_config().location
    return _build_layout(
        targets, cfg.rack_height_u, cfg.default_unit_type, cfg.default_system_height_u
    )


@router.get("/api/heatmap", summary="Data-hall heatmap values (JSON)")
async def datahall_heatmap(metric: str = "gpu_temp", user: str = Depends(get_current_user)):
    """Latest heatmap value per placed system, keyed by target id.

    Reads the collector-published snapshot from shared SQLite (fast, local — the
    collector's own event loop stalls during InfluxDB flushes, so we never call
    it synchronously here), maps host -> target_id, and attaches a colour domain
    (p5..p95, outlier-resistant) plus unit and critical threshold.
    """
    from ...metrics_cache import HEATMAP_METRICS, HEATMAP_SCALE_MAX

    spec = HEATMAP_METRICS.get(metric)
    if spec is None:
        return {"metric": metric, "values": {}, "domain": None, "unit": "", "critical": None}
    _raw_name, unit, critical = spec

    repository = get_repository()
    by_host = await repository.get_heatmap_snapshot(metric)
    if not by_host:
        return {"metric": metric, "values": {}, "domain": None, "unit": unit, "critical": critical}

    host_to_id = {t.host: t.id for t in await repository.get_all_targets()}
    values = {host_to_id[h]: v for h, v in by_host.items() if h in host_to_id}

    # Colour scale: the top is a STATIC per-component maximum (so colour reflects
    # absolute headroom, not fleet-relative rank); the bottom is the fleet's
    # cool/low end (p5), clamped below the max.
    nums = sorted(values.values())
    domain = None
    if nums:
        scale_max = HEATMAP_SCALE_MAX.get(metric, nums[-1])
        lo = nums[max(0, int(len(nums) * 0.05))]
        if lo >= scale_max:  # degenerate (e.g. a sensor over the rated max)
            lo = scale_max - 1
        domain = [lo, scale_max]

    return {
        "metric": metric,
        "unit": unit,
        "critical": critical,
        "values": values,
        "domain": domain,
    }


@router.post("/api/{target_id}/placement", summary="Set a system's placement")
async def set_placement(
    target_id: int,
    hall: str | None = Form(None),
    row: str | None = Form(None),
    rack: str | None = Form(None),
    rack_u: str | None = Form(None),
    height: str | None = Form(None),
    unit_type: str | None = Form(None),
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Assign/move a system to a rack position (operator action -> source=manual)."""
    validate_csrf_token(csrf_token)
    repository = get_repository()

    def _opt_int(raw: str | None) -> int | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        value = int(raw)  # ValueError -> 400 below
        if value < 1:
            raise ValueError("value must be >= 1")
        return value

    try:
        rack_u_val = _opt_int(rack_u)
        height_val = _opt_int(height)
    except ValueError:
        raise HTTPException(status_code=400, detail="rack_u and height must be positive integers")

    location_cfg = get_config().location
    placement = Placement(
        hall=(hall or "").strip() or None,
        row=(row or "").strip() or None,
        rack=(rack or "").strip() or None,
        rack_u=rack_u_val,
        height_u=height_val if height_val is not None else location_cfg.default_system_height_u,
        unit_type=(unit_type or "").strip() or location_cfg.default_unit_type,
        source="manual",
    )
    if not placement.has_location():
        raise HTTPException(
            status_code=400, detail="A rack (or at least one coordinate) is required"
        )

    # force=True: an explicit operator placement always wins.
    updated = await repository.set_target_location(target_id, placement, force=True)
    if not updated:
        raise HTTPException(status_code=404, detail="Target not found")
    return {"success": True, "message": "Placement updated"}


@router.post("/api/{target_id}/placement/clear", summary="Clear a system's placement")
async def clear_placement(
    target_id: int,
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Return a system to the Unplaced tray."""
    validate_csrf_token(csrf_token)
    repository = get_repository()
    updated = await repository.clear_target_location(target_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Target not found")
    return {"success": True, "message": "Placement cleared"}


@router.post("/api/resolve", summary="Auto-resolve placements from hostnames")
async def resolve_placements(
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Re-derive placement from hostnames for all targets.

    Honours source precedence, so manual (and Redfish) placements are preserved
    — only unplaced or hostname-sourced systems are (re)filled. Redfish-sourced
    placement is refreshed separately via Test Connection.
    """
    validate_csrf_token(csrf_token)
    repository = get_repository()
    targets = await repository.get_all_targets()
    updated = 0
    for t in targets:
        placement = parse_system_location(t.name, t.host)
        if not placement:
            continue
        result = await repository.set_target_location(t.id, placement)
        # Count only rows the precedence guard actually (re)placed from hostname.
        if result and result.loc_source == "hostname":
            updated += 1
    return {"success": True, "resolved": updated, "total": len(targets)}
