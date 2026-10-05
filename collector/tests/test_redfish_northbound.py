# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the read-only northbound Redfish PolicyService tree."""

_ROOT = "/redfish/v1"
_POLICY = "/redfish/v1/PolicyService/Policies/DiagnosticLogOnCriticalEvent"


async def test_service_root_advertises_policy_service(client):
    r = await client.get(_ROOT)
    assert r.status_code == 200
    body = r.json()
    assert body["@odata.type"].startswith("#ServiceRoot")
    assert body["PolicyService"]["@odata.id"] == "/redfish/v1/PolicyService"


async def test_policy_service_shape(client):
    r = await client.get("/redfish/v1/PolicyService")
    assert r.status_code == 200
    body = r.json()
    assert body["@odata.type"] == "#PolicyService.v1_0_0.PolicyService"
    assert body["OperatingMode"] == "Enabled"  # default config
    assert body["ConditionTypesSupported"] == ["Event"]
    assert body["Policies"]["@odata.id"] == "/redfish/v1/PolicyService/Policies"


async def test_policies_collection_lists_the_policy(client):
    r = await client.get("/redfish/v1/PolicyService/Policies")
    assert r.status_code == 200
    body = r.json()
    assert body["Members@odata.count"] == 1
    assert body["Members"][0]["@odata.id"] == _POLICY


async def test_policy_resource_shape(client):
    r = await client.get(_POLICY)
    assert r.status_code == 200
    body = r.json()
    assert body["@odata.type"] == "#Policy.v1_0_0.Policy"
    assert body["Predefined"] is True
    assert body["Enabled"] is True  # default config: enabled + Enabled mode
    # Trigger: Event condition over the configured severities.
    assert body["TriggerCondition"]["Type"] == "Event"
    assert "Critical" in body["TriggerCondition"]["Oem"]["Gyanam"]["Severities"]
    # Response: HTTP POST to CollectDiagnosticData.
    resp = body["Response"][0]
    assert resp["ResponseAction"] == "HTTP" and resp["HTTP"]["Operation"] == "POST"
    assert "CollectDiagnosticData" in resp["TargetURI"]
    # The 2h rearm is carried as an Oem property (no native Policy field).
    assert body["Oem"]["Gyanam"]["RearmSeconds"] == 7200


async def test_redfish_tree_requires_auth(noauth_client):
    for path in (_ROOT, "/redfish/v1/PolicyService", _POLICY):
        r = await noauth_client.get(path)
        assert r.status_code in (401, 403), path
