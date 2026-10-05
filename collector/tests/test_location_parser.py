# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for hostname-based placement parsing.

All hostnames here are synthetic fixtures invented for the test; they do not
represent any real site's naming scheme.
"""

from src.config import LocationConfig, LocationTokenRule
from src.location.hostname_parser import parse_hostname, parse_system_location


def _cfg(**overrides) -> LocationConfig:
    """A LocationConfig with the shipped default token rules, plus overrides."""
    return LocationConfig(**overrides)


def test_full_prefix_tagged_scheme():
    p = parse_hostname("edge-dh2-row3-rack05-u12", _cfg())
    assert p is not None
    assert (p.hall, p.row, p.rack, p.rack_u) == ("2", "3", "05", 12)
    assert p.source == "hostname"
    assert p.height_u == 4  # default form factor (4U) when not encoded
    assert p.unit_type == "OpenU"  # OCP Open Rack default


def test_partial_placement_rack_and_unit_only():
    p = parse_hostname("node-r7-u3", _cfg())
    assert p is not None
    assert p.rack == "7"
    assert p.rack_u == 3
    assert p.hall is None and p.row is None


def test_fqdn_domain_is_stripped():
    p = parse_hostname("box-rack9-u2.dc.example.com", _cfg())
    assert p is not None
    assert p.rack == "9" and p.rack_u == 2


def test_rack_unit_is_integer():
    p = parse_hostname("x-rack1-u015", _cfg())
    assert p is not None
    assert isinstance(p.rack_u, int) and p.rack_u == 15


def test_unparseable_hostname_returns_none():
    assert parse_hostname("gpu-node-001", _cfg()) is None


def test_blank_hostname_returns_none():
    assert parse_hostname("", _cfg()) is None
    assert parse_hostname("   ", _cfg()) is None


def test_positional_name_pattern_wins():
    # A site whose fields are positional, not prefix-tagged.
    cfg = _cfg(name_patterns=[r"^(?P<rack>\d+)x(?P<rack_u>\d+)$"])
    p = parse_hostname("12x5", cfg)
    assert p is not None
    assert p.rack == "12" and p.rack_u == 5


def test_custom_token_rule_for_site_specific_prefix():
    # Operators extend rules for their own prefixes (e.g. a pod token).
    cfg = _cfg(
        token_rules=[
            LocationTokenRule(field="row", pattern=r"pod(\d+)"),
            LocationTokenRule(field="rack", pattern=r"rk(\d+)"),
        ]
    )
    p = parse_hostname("site-pod6-rk34", cfg)
    assert p is not None
    assert p.row == "6" and p.rack == "34"


def test_noise_tokens_are_ignored():
    p = parse_hostname("prod-hv-rack02-u8-spare", _cfg())
    assert p is not None
    assert p.rack == "02" and p.rack_u == 8


def test_invalid_regex_rule_is_skipped_not_fatal():
    cfg = _cfg(name_patterns=["(unbalanced"])  # bad regex
    # Should not raise; falls through to token rules.
    p = parse_hostname("n-rack3-u1", cfg)
    assert p is not None and p.rack == "3"


def test_invalid_token_rule_regex_is_skipped():
    cfg = _cfg(
        token_rules=[
            LocationTokenRule(field="rack", pattern="(unbalanced"),  # bad regex
            LocationTokenRule(field="rack", pattern=r"rack(\d+)"),
        ]
    )
    p = parse_hostname("n-rack8", cfg)  # bad rule skipped, good rule matches
    assert p is not None and p.rack == "8"


def test_token_rule_without_capture_group_uses_whole_token():
    cfg = _cfg(token_rules=[LocationTokenRule(field="rack", pattern="spine")])
    p = parse_hostname("dc-spine-x", cfg)
    assert p is not None and p.rack == "spine"


def test_name_pattern_non_numeric_rack_u_is_dropped():
    # rack_u capture isn't an int -> that field is dropped, rack still set.
    cfg = _cfg(name_patterns=[r"^(?P<rack>\d+)-(?P<rack_u>[a-z]+)$"])
    p = parse_hostname("5-xyz", cfg)
    assert p is not None
    assert p.rack == "5" and p.rack_u is None


def test_openu_default_unit_type_is_respected():
    cfg = _cfg(default_unit_type="OpenU")
    p = parse_hostname("n-rack2-u4", cfg)
    assert p is not None and p.unit_type == "OpenU"


# ---- positional {cluster}-{datahall}-{rack}-{slot} scheme ----


def _positional_cfg() -> LocationConfig:
    # Skip the leading cluster token; capture data hall, rack, and numeric slot.
    return _cfg(name_patterns=[r"^[^-]+-(?P<hall>[^-]+)-(?P<rack>[^-]+)-(?P<rack_u>\d+)$"])


def test_positional_scheme_decodes_hall_rack_slot():
    cfg = _positional_cfg()
    p = parse_hostname("clusterx-hallz-rackq-04.site.example", cfg)
    assert p is not None
    assert p.hall == "hallz" and p.rack == "rackq" and p.rack_u == 4


def test_positional_scheme_supports_multiple_data_halls():
    cfg = _positional_cfg()
    a = parse_hostname("cl-aaa-r1-01", cfg)
    b = parse_hostname("cl-bbb-r2-02", cfg)
    assert a.hall == "aaa" and b.hall == "bbb"  # distinct halls -> selectable in the view


def test_positional_scheme_requires_trailing_numeric_slot():
    cfg = _positional_cfg()
    # Non-numeric final token -> pattern doesn't match -> no (false) placement.
    assert parse_hostname("cl-hall-rack-xx", cfg) is None


def test_greedy_hall_captures_everything_before_rack_and_slot():
    # Shipped scheme: last two tokens are rack + numeric slot; the rest is the hall.
    cfg = _cfg(name_patterns=[r"^(?P<hall>.+)-(?P<rack>[^-]+)-(?P<rack_u>\d+)$"])
    p4 = parse_hostname("cluster-dh3-a11-04", cfg)
    assert (p4.hall, p4.rack, p4.rack_u) == ("cluster-dh3", "a11", 4)
    # Variable leading token count is handled uniformly.
    p5 = parse_hostname("cl-region-dh3-b12-12", cfg)
    assert (p5.hall, p5.rack, p5.rack_u) == ("cl-region-dh3", "b12", 12)


# ---- parse_system_location: name first, host fallback ----


def test_system_location_prefers_name_over_host():
    cfg = _positional_cfg()
    # Name encodes location; host is a bare BMC IP.
    p = parse_system_location("cl-hallz-rackq-07", "10.1.2.3", cfg)
    assert p is not None and p.hall == "hallz" and p.rack_u == 7


def test_system_location_falls_back_to_host():
    cfg = _cfg()  # default token rules
    p = parse_system_location("friendly-name-no-location", "node-rack8-u2", cfg)
    assert p is not None and p.rack == "8" and p.rack_u == 2


def test_system_location_none_when_neither_encodes():
    cfg = _cfg()
    assert parse_system_location("plain", "10.0.0.5", cfg) is None
