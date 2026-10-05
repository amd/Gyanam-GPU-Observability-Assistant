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


SESSIONS = "https://bmc/redfish/v1/SessionService/Sessions"


async def test_session_auth_created(httpx_mock):
    # No pre-set token -> connect() creates a Redfish session.
    httpx_mock.add_response(
        method="POST",
        url=SESSIONS,
        status_code=201,
        headers={"X-Auth-Token": "sess123", "Location": f"{SESSIONS}/1"},
    )
    httpx_mock.add_response(method="DELETE", url=f"{SESSIONS}/Self", status_code=200)
    async with RedfishClient(base_url="https://bmc", username="u", password="p") as c:
        assert c._session_token == "sess123"


async def test_session_auth_no_token_header_falls_back(httpx_mock):
    httpx_mock.add_response(method="POST", url=SESSIONS, status_code=201)  # no X-Auth-Token
    async with RedfishClient(base_url="https://bmc", username="u", password="p") as c:
        assert c._session_token is None  # falls back to basic auth


async def test_session_auth_failure_status_falls_back(httpx_mock):
    httpx_mock.add_response(method="POST", url=SESSIONS, status_code=401)
    async with RedfishClient(base_url="https://bmc", username="u", password="p") as c:
        assert c._session_token is None


async def test_get_telemetry_wraps_collect(httpx_mock):
    endpoint = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )
    task = "/redfish/v1/TaskService/Tasks/9"
    httpx_mock.add_response(
        method="POST", url=f"https://bmc{endpoint}", status_code=202, headers={"Location": task}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        json={"Id": "9", "TaskState": "Completed", "PercentComplete": 100, "@odata.id": task},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}/attachment",
        status_code=200,
        content=b"TELEMETRY",
        headers={"Content-Type": "application/octet-stream"},
    )
    httpx_mock.add_response(method="DELETE", url=f"https://bmc{task}", status_code=200)
    c = RedfishClient(
        base_url="https://bmc", username="u", password="p", token="tok", task_poll_interval=0
    )
    async with c:
        res = await c.get_telemetry(endpoint)
    assert res.success is True and res.content == b"TELEMETRY"


async def test_collect_download_failure(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    task = "/redfish/v1/TaskService/Tasks/2"
    httpx_mock.add_response(
        method="POST", url=f"https://bmc{endpoint}", status_code=202, headers={"Location": task}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        json={"Id": "2", "TaskState": "Completed", "PercentComplete": 100, "@odata.id": task},
    )
    httpx_mock.add_response(
        method="GET", url=f"https://bmc{task}/attachment", status_code=500, is_reusable=True
    )
    # The task is now cleaned up on the failure path too (orphan-task fix), so a
    # DELETE is expected even though the download failed.
    httpx_mock.add_response(
        method="DELETE",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        is_optional=True,
    )
    c = RedfishClient(
        base_url="https://bmc", username="u", password="p", token="tok", task_poll_interval=0
    )
    async with c:
        res = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert res.success is False


async def test_get_metric_report_timeout(httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.ReadTimeout("slow"), method="GET", url=f"https://bmc{REPORT}")
    async with _client() as c:
        resp = await c.get_metric_report(REPORT)
    assert resp.success is False and "timed out" in (resp.error_message or "").lower()


async def test_get_metric_report_connect_error(httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("down"), method="GET", url=f"https://bmc{REPORT}")
    async with _client() as c:
        resp = await c.get_metric_report(REPORT)
    assert resp.success is False


async def test_collect_task_failed_state(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    task = "/redfish/v1/TaskService/Tasks/3"
    httpx_mock.add_response(
        method="POST", url=f"https://bmc{endpoint}", status_code=202, headers={"Location": task}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        json={"Id": "3", "TaskState": "Exception", "PercentComplete": 100, "@odata.id": task},
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"https://bmc{task}",
        status_code=200,
        is_reusable=True,
        is_optional=True,
    )
    c = RedfishClient(
        base_url="https://bmc", username="u", password="p", token="tok", task_poll_interval=0
    )
    async with c:
        res = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert res.success is False


async def test_delete_task(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/5"
    httpx_mock.add_response(method="DELETE", url=f"https://bmc{task}", status_code=200)
    async with _client() as c:
        assert await c._delete_task(task) is True
