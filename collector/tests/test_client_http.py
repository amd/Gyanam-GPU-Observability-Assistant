# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""HTTP-path tests for RedfishClient using pytest-httpx."""

from src.redfish.client import RedfishClient

REPORT = "/redfish/v1/TelemetryService/MetricReports/All"


def _client():
    # A pre-set token makes connect() skip the SessionService POST (no mock needed).
    return RedfishClient(base_url="https://bmc", username="u", password="p", token="tok")


async def test_get_metric_report_success(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{REPORT}",
        status_code=200,
        json={"MetricValues": [{"MetricProperty": "T", "MetricValue": "1"}]},
        headers={"Content-Type": "application/json"},
    )
    async with _client() as c:
        res = await c.get_metric_report(REPORT)
    assert res.success is True
    assert res.status_code == 200
    assert b"MetricValues" in res.content


async def test_get_metric_report_404(httpx_mock):
    httpx_mock.add_response(method="GET", url=f"https://bmc{REPORT}", status_code=404, text="nope")
    async with _client() as c:
        res = await c.get_metric_report(REPORT)
    assert res.success is False
    assert res.status_code == 404


async def test_get_metric_report_not_connected():
    c = _client()  # never connected -> no httpx client
    res = await c.get_metric_report(REPORT)
    assert res.success is False
    assert "not connected" in res.error_message.lower()


async def test_test_connection_success(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://bmc/redfish/v1/",
        status_code=200,
        json={"Vendor": "AMD", "Product": "MI300", "RedfishVersion": "1.6.0"},
    )
    async with _client() as c:
        ok, msg = await c.test_connection()
    assert ok is True
    assert "AMD" in msg and "MI300" in msg


async def test_test_connection_failure_status(httpx_mock):
    httpx_mock.add_response(method="GET", url="https://bmc/redfish/v1/", status_code=401)
    async with _client() as c:
        ok, msg = await c.test_connection()
    assert ok is False
    assert "401" in msg


async def test_collect_diagnostic_data_task_flow(httpx_mock):
    endpoint = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )
    task = "/redfish/v1/TaskService/Tasks/1"
    # 1) initiate -> 202 with task Location
    httpx_mock.add_response(
        method="POST",
        url=f"https://bmc{endpoint}",
        status_code=202,
        headers={"Location": task},
    )
    # 2) poll task -> Completed (reusable in case it polls more than once)
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        json={"Id": "1", "TaskState": "Completed", "PercentComplete": 100, "@odata.id": task},
    )
    # 3) download attachment
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}/attachment",
        status_code=200,
        content=b"BLOBDATA",
        headers={"Content-Type": "application/octet-stream"},
    )
    # 4) cleanup delete
    httpx_mock.add_response(method="DELETE", url=f"https://bmc{task}", status_code=200)

    c = RedfishClient(
        base_url="https://bmc", username="u", password="p", token="tok", task_poll_interval=0
    )
    async with c:
        res = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert res.success is True
    assert res.content == b"BLOBDATA"


async def test_collect_diagnostic_data_initiate_failure(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(method="POST", url=f"https://bmc{endpoint}", status_code=500)
    c = RedfishClient(
        base_url="https://bmc", username="u", password="p", token="tok", task_poll_interval=0
    )
    async with c:
        res = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert res.success is False
