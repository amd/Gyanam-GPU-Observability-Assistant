# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for target name/host validators (SSRF + input hardening)."""

import pytest
from src.api.routes.targets import validate_host, validate_name

# ---- validate_name ----


def test_valid_name():
    assert validate_name("  gpu-node 01.rack_2 ") == "gpu-node 01.rack_2"


def test_empty_name_rejected():
    with pytest.raises(ValueError):
        validate_name("")
    with pytest.raises(ValueError):
        validate_name("   ")


def test_too_long_name_rejected():
    with pytest.raises(ValueError):
        validate_name("a" * 256)


def test_name_with_illegal_chars_rejected():
    for bad in ["a/b", "a;b", "a$(x)", "<script>", "a\nb"]:
        with pytest.raises(ValueError):
            validate_name(bad)


# ---- validate_host (SSRF prevention) ----


def test_valid_private_ip_and_hostname():
    assert validate_host("10.0.0.5") == "10.0.0.5"
    assert validate_host("bmc-1.example.com") == "bmc-1.example.com"


def test_empty_host_rejected():
    with pytest.raises(ValueError):
        validate_host("  ")


def test_localhost_blocked():
    with pytest.raises(ValueError):
        validate_host("localhost")


def test_loopback_ip_blocked():
    with pytest.raises(ValueError):
        validate_host("127.0.0.1")
