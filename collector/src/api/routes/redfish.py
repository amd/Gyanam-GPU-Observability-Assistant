# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Minimal northbound Redfish tree exposing gyanam's PolicyService (read-only).

Rather than re-export a full aggregated Redfish tree, gyanam presents a
bare-minimum, conformant service: ServiceRoot -> PolicyService -> Policies -> a
single predefined Policy describing the automated function gyanam performs —
collect diagnostic logs on a fatal/critical event, with a per-target rearm
window. This is the *description*; the executing side is the collector's
PolicyEngine (src/policy/engine.py).

PolicyService/Policy follow the Redfish 2026.2 schema. The 2-hour rearm has no
native Policy property (the schema only has Response.DelaySeconds), so it is
carried as Oem.Gyanam.RearmSeconds and enforced by the engine.

Served read-only and behind the gyanam session (operator-facing). The payload
contains no secrets — only the policy configuration.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...config import get_config
from ..auth import get_current_user

router = APIRouter()

_SERVICE_ROOT = "/redfish/v1"
_POLICY_SERVICE = f"{_SERVICE_ROOT}/PolicyService"
_POLICIES = f"{_POLICY_SERVICE}/Policies"
_POLICY_ID = "DiagnosticLogOnCriticalEvent"
_POLICY = f"{_POLICIES}/{_POLICY_ID}"


@router.get("/redfish/v1", summary="Redfish ServiceRoot (minimal)")
async def service_root(user: str = Depends(get_current_user)) -> dict:
    """Minimal Redfish ServiceRoot advertising the PolicyService."""
    return {
        "@odata.id": _SERVICE_ROOT,
        "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
        "Id": "RootService",
        "Name": "Gyanam Redfish Service",
        "RedfishVersion": "1.20.0",
        "PolicyService": {"@odata.id": _POLICY_SERVICE},
        "Oem": {"Gyanam": {"Role": "Aggregator"}},
    }


@router.get("/redfish/v1/PolicyService", summary="Gyanam PolicyService")
async def policy_service(user: str = Depends(get_current_user)) -> dict:
    """PolicyService describing gyanam's automated functions."""
    cfg = get_config().policy
    return {
        "@odata.id": _POLICY_SERVICE,
        "@odata.type": "#PolicyService.v1_0_0.PolicyService",
        "Id": "PolicyService",
        "Name": "Gyanam Policy Service",
        "OperatingMode": cfg.operating_mode if cfg.enabled else "Disabled",
        "Status": {
            "State": "Enabled" if cfg.enabled else "Disabled",
            "Health": "OK",
        },
        "ConditionTypesSupported": ["Event"],
        "ActionsSupported": ["HTTP"],
        "Policies": {"@odata.id": _POLICIES},
    }


@router.get("/redfish/v1/PolicyService/Policies", summary="Policy collection")
async def policies(user: str = Depends(get_current_user)) -> dict:
    """Collection of gyanam's predefined policies."""
    return {
        "@odata.id": _POLICIES,
        "@odata.type": "#PolicyCollection.PolicyCollection",
        "Name": "Policies",
        "Members@odata.count": 1,
        "Members": [{"@odata.id": _POLICY}],
    }


def _trigger_condition(cfg) -> dict:
    """Build the Policy TriggerCondition from the configured triggers.

    Standard Redfish Event conditions key on a MessageId. When specific
    MessageIds are configured we express an Or of Event conditions; otherwise the
    policy fires on any MessageId at the configured severities, carried as an Oem
    qualifier (severity-class triggering is a gyanam extension).
    """
    if cfg.trigger_message_ids:
        return {
            "Type": "Or",
            "SubordinateConditions": [
                {"Type": "Event", "MessageId": mid} for mid in cfg.trigger_message_ids
            ],
        }
    return {
        "Type": "Event",
        "Oem": {"Gyanam": {"Severities": list(cfg.trigger_severities)}},
    }


@router.get(f"/redfish/v1/PolicyService/Policies/{_POLICY_ID}", summary="Diagnostic-log policy")
async def diagnostic_log_policy(user: str = Depends(get_current_user)) -> dict:
    """The predefined 'collect diagnostics on a fatal/critical event' policy."""
    config = get_config()
    cfg = config.policy
    return {
        "@odata.id": _POLICY,
        "@odata.type": "#Policy.v1_0_0.Policy",
        "Id": _POLICY_ID,
        "Name": "Collect diagnostics on fatal/critical event",
        "Predefined": True,
        "Enabled": cfg.enabled and cfg.operating_mode == "Enabled",
        "TriggerCondition": _trigger_condition(cfg),
        "Response": [
            {
                "ResponseAction": "HTTP",
                "TargetURI": config.redfish.collect_endpoint,
                "HTTP": {
                    "Operation": "POST",
                    "Body": config.redfish.collect_body,
                },
                "Purpose": "POST CollectDiagnosticData on the system that raised the event.",
            }
        ],
        "Oem": {
            "Gyanam": {
                # The 2h hysteresis has no native Policy property; enforced by the
                # engine and surfaced here for visibility/tuning.
                "RearmSeconds": cfg.rearm_seconds,
                "TriggerSeverities": list(cfg.trigger_severities),
            }
        },
    }
