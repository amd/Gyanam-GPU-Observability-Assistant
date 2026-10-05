# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Gyanam policy engine: the automated functions gyanam performs on Redfish events.

Currently one policy — automated diagnostic-log collection on a fatal/critical
event, with a per-target rearm window — exposed read-only as a standard Redfish
PolicyService (2026.2) at /redfish/v1/PolicyService.
"""

from .engine import PolicyEngine

__all__ = ["PolicyEngine"]
