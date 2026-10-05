# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for parsing placement from a Redfish Chassis ``Location`` object.

(The Chassis member traversal that feeds this is covered end-to-end by
test_inventory_collector.py.)
"""

from src.location.redfish_location import placement_from_location


def test_full_placement():
    loc = {
        "PostalAddress": {"Room": "Hall-A"},
        "Placement": {
            "Row": "North",
            "Rack": "WEB43",
            "RackOffset": 12,
            "RackOffsetUnits": "EIA_310",
        },
    }
    p = placement_from_location(loc, "EIA_310")
    assert (p.hall, p.row, p.rack, p.rack_u) == ("Hall-A", "North", "WEB43", 12)
    assert p.unit_type == "EIA_310" and p.has_location()


def test_open_u_units():
    p = placement_from_location(
        {"Placement": {"Rack": "K2", "RackOffset": 3, "RackOffsetUnits": "OpenU"}}, "EIA_310"
    )
    assert p.unit_type == "OpenU" and p.rack == "K2"


def test_unknown_units_fall_back_to_default():
    p = placement_from_location(
        {"Placement": {"Rack": "R1", "RackOffset": 1, "RackOffsetUnits": "Weird"}}, "EIA_310"
    )
    assert p.unit_type == "EIA_310"


def test_none_and_empty_have_no_location():
    assert placement_from_location(None, "EIA_310").has_location() is False
    assert placement_from_location({}, "EIA_310").has_location() is False


def test_ignores_bool_rack_offset():
    # bool is an int subclass — must not be read as a rack unit.
    p = placement_from_location({"Placement": {"Rack": "R9", "RackOffset": True}}, "EIA_310")
    assert p.rack == "R9" and p.rack_u is None


def test_building_used_as_hall_fallback():
    p = placement_from_location({"PostalAddress": {"Building": "B1"}}, "EIA_310")
    assert p.hall == "B1"
