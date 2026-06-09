# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for RedfishClient pure helpers (no network)."""

import httpx
from src.redfish.client import RedfishClient, TaskState, TaskStatus


def _client(**kw):
    return RedfishClient(base_url="https://bmc.example.com/", username="u", password="p", **kw)


def test_base_url_stripped():
    assert _client().base_url == "https://bmc.example.com"


def test_auth_headers_basic_vs_token():
    c = _client()
    h = c._get_auth_headers()
    assert "X-Auth-Token" not in h
    assert "Accept" in h and "Content-Type" in h
    c._session_token = "tok123"
    assert c._get_auth_headers()["X-Auth-Token"] == "tok123"


def test_get_auth_uses_basic_without_token():
    c = _client()
    auth = c._get_auth()
    assert isinstance(auth, httpx.BasicAuth)
    c._session_token = "tok"
    assert c._get_auth() is None


def test_ssh_auth_tuple():
    c = _client()
    assert c._get_ssh_auth() == ("u", "p")
    c._session_token = "tok"
    assert c._get_ssh_auth() is None


def _task(result_location=None, task_uri="/redfish/v1/TaskService/Tasks/5"):
    return TaskStatus(
        task_id="5",
        task_uri=task_uri,
        state=TaskState.COMPLETED,
        percent_complete=100,
        message="done",
        result_location=result_location,
    )


def test_attachment_uri_from_result_location():
    c = _client()
    assert c._get_attachment_uri(_task(result_location="/loc/1")) == "/loc/1/attachment"
    # already suffixed -> not doubled
    assert c._get_attachment_uri(_task(result_location="/loc/1/attachment")) == "/loc/1/attachment"


def test_attachment_uri_fallback_to_task_uri():
    c = _client()
    assert c._get_attachment_uri(_task()) == "/redfish/v1/TaskService/Tasks/5/attachment"


def test_is_connected():
    c = _client()
    assert c._is_connected() is False
    c._client = object()  # simulate an open httpx client
    assert c._is_connected() is True
