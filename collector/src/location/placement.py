# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Pure rack-placement geometry for the Data Hall view.

This is the model-free half of the data-hall layout: row derivation, natural
sorting, and per-rack slot assignment, plus the floor-geometry constants. It
has no dependency on the Target/inventory models, so it can be unit-tested and
reused independently of the FastAPI route that renders it (``api/routes/
datahall.py`` builds the view/layout on top of these helpers).
"""

import re

_UNKNOWN_HALL = "Unassigned Hall"
_UNKNOWN_ROW = "Unassigned Row"

_DIGITS = re.compile(r"(\d+)")
# Leading alphabetic run of a rack label (e.g. "G" in "G12") — many data halls
# encode the row in the rack name's letter prefix.
_RACK_ROW_PREFIX = re.compile(r"^([A-Za-z]+)")

# Floor geometry for the 3D twin (metres). Rack footprint ~0.6m wide; the pitch
# leaves a small gap between cabinets, and the row pitch leaves a walkable aisle.
_RACK_PITCH_M = 0.8
_ROW_PITCH_M = 2.2


def _derive_row(loc_row: str | None, rack_name: str | None) -> str | None:
    """Resolve a rack's row.

    Precedence: an explicit row (from the hostname scheme or the Redfish
    ``Location`` interface, persisted in ``loc_row``) always wins. When that's
    absent, fall back to the alphabetic prefix of the rack label — the common
    convention where the row is encoded in the rack number (e.g. rack ``G12`` is
    in row ``G``). Returns None if neither yields a row.
    """
    explicit = (loc_row or "").strip()
    if explicit:
        return explicit
    m = _RACK_ROW_PREFIX.match((rack_name or "").strip())
    return m.group(1).upper() if m else None


def _natural_key(name: str) -> list[tuple[int, object]]:
    """Sort key that orders embedded numbers numerically ("9" before "10").

    Splits a label into text/number runs; each run is tagged (0, text) or
    (1, int) so comparisons stay type-safe across mixed labels.
    """
    return [
        (1, int(part)) if part.isdigit() else (0, part.lower())
        for part in _DIGITS.split(name)
        if part != ""
    ]


def _assign_slots(
    systems: list[dict], rack_height: int, default_height: int = 4
) -> dict[int, dict]:
    """Map rack-unit -> system for one rack.

    Systems with an explicit ``rack_u`` are placed first (lowest first); any
    without are stacked into the lowest free slots. A system whose starting unit
    would push it past the top of the rack is clamped down so it still fits and
    stays visible (common once systems are multi-U). Each system's resolved
    bottom unit is written back as ``start_u`` (None only if the rack is full).
    """
    occupied: dict[int, dict] = {}

    def place(system: dict, start: int) -> None:
        height = max(1, int(system.get("height") or default_height))
        if height > rack_height:
            system["start_u"] = None  # taller than the rack — genuinely can't fit
            return
        max_start = rack_height - height + 1
        u = min(max(1, start), max_start)  # clamp so a tall system fits from the top
        while u <= max_start:
            if all((u + i) not in occupied for i in range(height)):
                for i in range(height):
                    occupied[u + i] = system
                system["start_u"] = u
                return
            u += 1
        system["start_u"] = None  # rack genuinely full — shown in an overflow note

    explicit = sorted((s for s in systems if s.get("rack_u")), key=lambda s: s["rack_u"])
    floating = [s for s in systems if not s.get("rack_u")]
    for system in explicit:
        place(system, int(system["rack_u"]))
    for system in floating:
        place(system, 1)
    return occupied
