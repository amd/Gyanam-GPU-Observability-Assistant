# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for name-vs-BMC location reconciliation."""

from src.inventory import check_location
from src.location.models import Placement


def test_match_case_insensitive():
    name = Placement(hall="ODCDH3", rack="A11")
    bmc = Placement(hall="odcdh3", rack="a11")
    assert check_location(name, bmc) == "match"


def test_mismatch_on_rack():
    name = Placement(hall="odcdh3", rack="a11")
    bmc = Placement(hall="odcdh3", rack="b06")
    assert check_location(name, bmc) == "mismatch"


def test_none_when_one_side_missing():
    assert check_location(None, Placement(rack="a11")) is None
    assert check_location(Placement(rack="a11"), None) is None


def test_none_when_no_comparable_fields():
    # Name has only a rack, BMC has only a hall -> nothing comparable on both sides.
    assert check_location(Placement(rack="a11"), Placement(hall="odcdh3")) is None


def test_match_when_only_rack_present_on_both():
    assert check_location(Placement(rack="a11"), Placement(rack="A11")) == "match"
