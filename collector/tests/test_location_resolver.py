# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for placement source precedence and resolution."""

from src.location import Placement, should_overwrite


def test_should_overwrite_precedence():
    # manual (3) > redfish (2) > hostname (1) > none (0)
    assert should_overwrite(None, "hostname") is True
    assert should_overwrite("hostname", "redfish") is True
    assert should_overwrite("redfish", "manual") is True
    # Equal rank may refresh (a fresh read from the same kind of source).
    assert should_overwrite("manual", "manual") is True
    # Lower-trust never clobbers higher-trust.
    assert should_overwrite("manual", "redfish") is False
    assert should_overwrite("redfish", "hostname") is False
    assert should_overwrite("manual", "hostname") is False


def test_placement_to_columns_roundtrip():
    p = Placement(
        site="s",
        hall="2",
        row="3",
        rack="05",
        rack_u=12,
        height_u=2,
        unit_type="EIA_310",
        source="manual",
    )
    cols = p.to_columns()
    assert cols["loc_rack"] == "05"
    assert cols["loc_rack_u"] == 12
    assert cols["loc_source"] == "manual"
    # Column keys line up with what from_target reads back.
    from types import SimpleNamespace

    target = SimpleNamespace(**cols)
    back = Placement.from_target(target)
    assert back == p
