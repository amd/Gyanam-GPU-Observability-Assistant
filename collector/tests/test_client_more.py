# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional coverage for RedfishClient uncovered paths.

Covers session-create fallbacks, _initiate_collection branches, _wait_for_task
state machine, _get_attachment_uri, _download_attachment errors, _delete_task
status handling, and test_connection.
"""

import httpx
import pytest
from src.redfish.client import RedfishClient, TaskState, TaskStatus

BASE = "https://bmc"
SESSIONS = f"{BASE}/redfish/v1/SessionService/Sessions"


def _client(**kw):
    kw.setdefault("token", "tok")  # skip SessionService auth
    kw.setdefault("task_poll_interval", 0)
    return RedfishClient(base_url=BASE, username="u", password="p", **kw)


# --------------------------------------------------------------------------
# connect / _create_session
# --------------------------------------------------------------------------


async def test_create_session_not_connected_raises():
    c = _client(token=None)  # not connected, no httpx client
    with pytest.raises(RuntimeError):
        await c._create_session()


async def test_create_session_exception_falls_back(httpx_mock):
    # POST to SessionService raises -> connect() swallows, falls back to basic.
    httpx_mock.add_exception(httpx.ConnectError("down"), method="POST", url=SESSIONS)
    async with RedfishClient(base_url=BASE, username="u", password="p") as c:
        assert c._session_token is None


# --------------------------------------------------------------------------
# collect_diagnostic_data guard
# --------------------------------------------------------------------------


async def test_collect_not_connected_raises():
    c = _client()  # never entered
    with pytest.raises(RuntimeError):
        await c.collect_diagnostic_data()


# --------------------------------------------------------------------------
# _initiate_collection branches
# --------------------------------------------------------------------------


async def test_initiate_202_task_uri_from_body_odata(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    # 202 with NO Location header; task id comes from body @odata.id.
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=202,
        json={"@odata.id": "/redfish/v1/TaskService/Tasks/42"},
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {"a": 1})
    assert uri == "/redfish/v1/TaskService/Tasks/42"


async def test_initiate_202_task_id_relative_normalized(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=202,
        json={"Id": "77"},  # bare id -> normalized to full Tasks path
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri == "/redfish/v1/TaskService/Tasks/77"


async def test_initiate_202_body_not_json(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=202,
        text="not-json",
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri is None  # no Location, body not JSON


async def test_initiate_200_with_odata(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=200,
        json={"@odata.id": "/redfish/v1/TaskService/Tasks/5"},
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri == "/redfish/v1/TaskService/Tasks/5"


async def test_initiate_200_not_json_returns_none(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(method="POST", url=f"{BASE}{endpoint}", status_code=201, text="ok")
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri is None


async def test_initiate_error_status_json_message(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=400,
        json={"error": {"message": "bad request body"}},
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri is None


async def test_initiate_error_status_non_json(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(method="POST", url=f"{BASE}{endpoint}", status_code=503, text="busy")
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri is None


async def test_initiate_request_error(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_exception(httpx.ConnectError("boom"), method="POST", url=f"{BASE}{endpoint}")
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
    assert uri is None


async def test_initiate_404_records_status_and_error(httpx_mock):
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=404,
        json={"error": {"message": "LogService.CollectDiagnosticData was not found"}},
    )
    async with _client() as c:
        uri = await c._initiate_collection(endpoint, {})
        assert uri is None
        assert c._last_initiate_status == 404
        assert "not found" in (c._last_initiate_error or "").lower()


async def test_collect_202_no_uri_logs_leak(httpx_mock, caplog):
    # 202 Accepted but no parseable task URI: the BMC created a task we can't
    # address to delete -> surface the leak loudly.
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST", url=f"{BASE}{endpoint}", status_code=202, text="not-json"
    )
    async with _client() as c:
        with caplog.at_level("ERROR"):
            resp = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert resp.success is False and resp.status_code == 202
    assert "may leak in TaskService" in caplog.text


async def test_collect_surfaces_initiation_404(httpx_mock):
    # collect_diagnostic_data must bubble the initiation status so the caller
    # can tell "action not found" apart from other failures and rediscover.
    endpoint = "/redfish/v1/X/Actions/Collect"
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}{endpoint}",
        status_code=404,
        json={"error": {"message": "LogService.CollectDiagnosticData was not found"}},
    )
    async with _client() as c:
        resp = await c.collect_diagnostic_data(collect_endpoint=endpoint)
    assert resp.success is False
    assert resp.status_code == 404
    assert "not found" in (resp.error_message or "").lower()


# --------------------------------------------------------------------------
# discover_collect_endpoint — LogService action walk
# --------------------------------------------------------------------------

_ACTION = "#LogService.CollectDiagnosticData"


def _collection(*ids):
    return {"Members": [{"@odata.id": i} for i in ids]}


async def test_discover_finds_action_under_systems(httpx_mock):
    target_uri = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems", json=_collection("/redfish/v1/Systems/UBB")
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB",
        json={"LogServices": {"@odata.id": "/redfish/v1/Systems/UBB/LogServices"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB/LogServices",
        json=_collection("/redfish/v1/Systems/UBB/LogServices/DiagLogs"),
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB/LogServices/DiagLogs",
        json={"Actions": {_ACTION: {"target": target_uri}}},
    )
    async with _client() as c:
        assert await c.discover_collect_endpoint() == target_uri


async def test_discover_falls_through_to_managers(httpx_mock):
    target_uri = (
        "/redfish/v1/Managers/AMC/LogServices/Dump/Actions/LogService.CollectDiagnosticData"
    )
    # Systems present but exposes no CollectDiagnosticData action.
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems", json=_collection("/redfish/v1/Systems/UBB")
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB",
        json={"LogServices": {"@odata.id": "/redfish/v1/Systems/UBB/LogServices"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB/LogServices",
        json=_collection("/redfish/v1/Systems/UBB/LogServices/SEL"),
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/UBB/LogServices/SEL", json={"Actions": {}}
    )
    # Managers carries it on a 'Dump' LogService.
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Managers", json=_collection("/redfish/v1/Managers/AMC")
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Managers/AMC",
        json={"LogServices": {"@odata.id": "/redfish/v1/Managers/AMC/LogServices"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Managers/AMC/LogServices",
        json=_collection("/redfish/v1/Managers/AMC/LogServices/Dump"),
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Managers/AMC/LogServices/Dump",
        json={"Actions": {_ACTION: {"target": target_uri}}},
    )
    async with _client() as c:
        assert await c.discover_collect_endpoint() == target_uri


async def test_discover_returns_none_when_absent(httpx_mock):
    # Both roots reachable but empty -> no action found.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems", json=_collection())
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json=_collection())
    async with _client() as c:
        assert await c.discover_collect_endpoint() is None


async def test_discover_skips_unreachable_root(httpx_mock):
    # Systems 404s (skipped); Managers has no members -> None, no raise.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json=_collection())
    async with _client() as c:
        assert await c.discover_collect_endpoint() is None


# --------------------------------------------------------------------------
# _wait_for_task state machine
# --------------------------------------------------------------------------


async def test_wait_for_task_running_then_completed(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/10"
    url = f"{BASE}{task}"
    # First poll Running, second Completed. Message from Messages array; location
    # from Payload.HttpHeaders (string form).
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=200,
        json={
            "Id": "10",
            "TaskState": "Running",
            "PercentComplete": 50,
            "Messages": [{"Message": "working"}],
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=200,
        json={
            "Id": "10",
            "TaskState": "Completed",
            "PercentComplete": 100,
            "TaskStatus": "OK",
            "Payload": {"HttpHeaders": ["Location: /redfish/v1/Dumps/1"]},
        },
    )
    async with _client() as c:
        status = await c._wait_for_task(task)
    assert status is not None
    assert status.state == TaskState.COMPLETED
    assert status.result_location == "/redfish/v1/Dumps/1"


async def test_wait_for_task_location_dict_header(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/11"
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{task}",
        status_code=200,
        json={
            "Id": "11",
            "TaskState": "Completed",
            "PercentComplete": 100,
            "Payload": {"HttpHeaders": [{"Location": "/redfish/v1/Dumps/9"}]},
        },
    )
    async with _client() as c:
        status = await c._wait_for_task(task)
    assert status.result_location == "/redfish/v1/Dumps/9"


async def test_wait_for_task_unknown_state_then_terminal(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/12"
    url = f"{BASE}{task}"
    # Unknown state coerces to RUNNING (non-terminal) -> loops.
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=200,
        json={"Id": "12", "TaskState": "Bogus", "PercentComplete": 10},
    )
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=200,
        json={
            "Id": "12",
            "TaskState": "Killed",
            "PercentComplete": 100,
            "TaskMonitor": "/redfish/v1/TaskMonitors/12",
        },
    )
    async with _client() as c:
        status = await c._wait_for_task(task)
    assert status.state == TaskState.KILLED
    assert status.result_location == "/redfish/v1/TaskMonitors/12"


async def test_wait_for_task_non_200_returns_none(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/13"
    httpx_mock.add_response(method="GET", url=f"{BASE}{task}", status_code=500)
    async with _client() as c:
        assert await c._wait_for_task(task) is None


async def test_wait_for_task_transient_errors_exhausted(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/14"
    httpx_mock.add_exception(
        httpx.ConnectError("flap"), method="GET", url=f"{BASE}{task}", is_reusable=True
    )
    async with _client() as c:
        assert await c._wait_for_task(task) is None


async def test_wait_for_task_parse_error_returns_none(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/15"
    # 200 but body isn't JSON -> response.json() raises -> generic except -> None.
    httpx_mock.add_response(method="GET", url=f"{BASE}{task}", status_code=200, text="<<notjson>>")
    async with _client() as c:
        assert await c._wait_for_task(task) is None


async def test_wait_for_task_timeout_returns_none(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/16"
    # task_timeout=0 -> the while loop body never runs -> immediate timeout.
    async with _client(task_timeout=0) as c:
        assert await c._wait_for_task(task) is None


# --------------------------------------------------------------------------
# _get_attachment_uri
# --------------------------------------------------------------------------


def test_get_attachment_uri_appends_suffix():
    c = _client()
    ts = TaskStatus(
        task_id="1",
        task_uri="/t/1",
        state=TaskState.COMPLETED,
        percent_complete=100,
        message="",
        result_location="/redfish/v1/Dumps/1",
    )
    assert c._get_attachment_uri(ts) == "/redfish/v1/Dumps/1/attachment"


def test_get_attachment_uri_already_has_suffix():
    c = _client()
    ts = TaskStatus(
        task_id="1",
        task_uri="/t/1",
        state=TaskState.COMPLETED,
        percent_complete=100,
        message="",
        result_location="/d/1/attachment",
    )
    assert c._get_attachment_uri(ts) == "/d/1/attachment"


def test_get_attachment_uri_fallback_to_task_uri():
    c = _client()
    ts = TaskStatus(
        task_id="1",
        task_uri="/redfish/v1/TaskService/Tasks/1",
        state=TaskState.COMPLETED,
        percent_complete=100,
        message="",
        result_location=None,
    )
    assert c._get_attachment_uri(ts) == "/redfish/v1/TaskService/Tasks/1/attachment"


# --------------------------------------------------------------------------
# _download_attachment
# --------------------------------------------------------------------------


async def test_download_attachment_success_absolute_url(httpx_mock):
    url = f"{BASE}/redfish/v1/Dumps/1/attachment"
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=200,
        content=b"ZIP",
        headers={"Content-Type": "application/zip"},
    )
    async with _client() as c:
        resp = await c._download_attachment(url)  # already absolute
    assert resp.success is True and resp.content == b"ZIP"


async def test_download_attachment_error_json_message(httpx_mock):
    uri = "/redfish/v1/Dumps/2/attachment"
    httpx_mock.add_response(
        method="GET", url=f"{BASE}{uri}", status_code=403, json={"error": {"message": "forbidden"}}
    )
    async with _client() as c:
        resp = await c._download_attachment(uri)
    assert resp.success is False and resp.error_message == "forbidden"


async def test_download_attachment_error_text_fallback(httpx_mock):
    uri = "/redfish/v1/Dumps/3/attachment"
    httpx_mock.add_response(method="GET", url=f"{BASE}{uri}", status_code=500, text="server boom")
    async with _client() as c:
        resp = await c._download_attachment(uri)
    assert resp.success is False and "server boom" in resp.error_message


async def test_download_attachment_timeout(httpx_mock):
    uri = "/redfish/v1/Dumps/4/attachment"
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), method="GET", url=f"{BASE}{uri}")
    async with _client() as c:
        resp = await c._download_attachment(uri)
    assert resp.success is False and "timed out" in resp.error_message.lower()


async def test_download_attachment_request_error(httpx_mock):
    uri = "/redfish/v1/Dumps/5/attachment"
    httpx_mock.add_exception(httpx.ConnectError("down"), method="GET", url=f"{BASE}{uri}")
    async with _client() as c:
        resp = await c._download_attachment(uri)
    assert resp.success is False and "failed" in resp.error_message.lower()


# --------------------------------------------------------------------------
# get_metric_report error branches
# --------------------------------------------------------------------------


async def test_get_metric_report_error_json_message(httpx_mock):
    uri = "/redfish/v1/TelemetryService/MetricReports/All"
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{uri}",
        status_code=404,
        json={"error": {"message": "no such report"}},
    )
    async with _client() as c:
        resp = await c.get_metric_report(uri)
    assert resp.success is False and resp.error_message == "no such report"


async def test_get_metric_report_unexpected_content_type(httpx_mock):
    uri = "/redfish/v1/TelemetryService/MetricReports/All"
    # 200 but not JSON content-type -> unexpected content-type error.
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{uri}",
        status_code=200,
        content=b"<html/>",
        headers={"Content-Type": "text/html"},
    )
    async with _client() as c:
        resp = await c.get_metric_report(uri)
    assert resp.success is False and "content-type" in resp.error_message.lower()


# --------------------------------------------------------------------------
# _delete_task status handling
# --------------------------------------------------------------------------


async def test_delete_task_not_connected_returns_false():
    c = _client()  # not entered
    assert await c._delete_task("/redfish/v1/TaskService/Tasks/1") is False


async def test_delete_task_404_treated_as_success(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/20"
    httpx_mock.add_response(method="DELETE", url=f"{BASE}{task}", status_code=404)
    async with _client() as c:
        assert await c._delete_task(task) is True


async def test_delete_task_405_treated_as_success(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/21"
    httpx_mock.add_response(method="DELETE", url=f"{BASE}{task}", status_code=405)
    async with _client() as c:
        assert await c._delete_task(task) is True


async def test_delete_task_other_status_false(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/22"
    httpx_mock.add_response(method="DELETE", url=f"{BASE}{task}", status_code=500)
    async with _client() as c:
        assert await c._delete_task(task) is False


async def test_delete_task_request_error_false(httpx_mock):
    task = "/redfish/v1/TaskService/Tasks/23"
    httpx_mock.add_exception(httpx.ConnectError("down"), method="DELETE", url=f"{BASE}{task}")
    async with _client() as c:
        assert await c._delete_task(task) is False


# --------------------------------------------------------------------------
# test_connection
# --------------------------------------------------------------------------


async def test_test_connection_lazy_connect(httpx_mock):
    # No token, no prior connect -> test_connection connects first (creates session),
    # then GETs the service root.
    httpx_mock.add_response(
        method="POST", url=SESSIONS, status_code=201, headers={"X-Auth-Token": "s1"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/redfish/v1/",
        status_code=200,
        json={"Vendor": "AMD", "Product": "MI300", "RedfishVersion": "1.6"},
    )
    httpx_mock.add_response(
        method="DELETE", url=f"{SESSIONS}/Self", status_code=200, is_optional=True
    )
    c = RedfishClient(base_url=BASE, username="u", password="p")
    try:
        ok, msg = await c.test_connection()
    finally:
        await c.close()
    assert ok is True and "AMD" in msg


async def test_test_connection_request_error(httpx_mock):
    httpx_mock.add_exception(
        httpx.ConnectError("no route"), method="GET", url=f"{BASE}/redfish/v1/"
    )
    async with _client() as c:
        ok, msg = await c.test_connection()
    assert ok is False and "Connection failed" in msg
