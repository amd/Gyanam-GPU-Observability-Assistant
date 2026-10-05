# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra route coverage for logs.py: paginated listing, HTML page, download
(found + 404 variants), collect single / collect-all, and the delete form.
"""

from unittest.mock import AsyncMock, MagicMock

from src.api import dependencies
from src.api.csrf import generate_csrf_token


def _install_collector(**methods):
    """Swap in a log collector whose async methods are controllable."""
    lc = MagicMock()
    for name, value in methods.items():
        setattr(lc, name, value)
    dependencies.app_state["log_collector"] = lc
    return lc


async def _make_log(repo, **overrides):
    kwargs = {
        "target_id": 1,
        "target_name": "node-1",
        "target_host": "10.0.0.5",
        "filename": "bundle.gz",
        "file_path": "/data/bundle.gz",
        "status": "completed",
    }
    kwargs.update(overrides)
    return await repo.create_collected_log(**kwargs)


# ---------------------------------------------------------------------------
# Paginated JSON listing
# ---------------------------------------------------------------------------


async def test_list_logs_api_pagination_shape(client, repo):
    await _make_log(repo, filename="a.gz")
    await _make_log(repo, filename="b.gz")
    r = await client.get("/logs/api?limit=1&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["logs"]) == 1


# ---------------------------------------------------------------------------
# HTML page with pagination context
# ---------------------------------------------------------------------------


async def test_logs_page_renders(client, repo):
    await _make_log(repo, filename="p.gz")
    r = await client.get("/logs")
    assert r.status_code == 200


async def test_logs_page_with_offset(client, repo):
    await _make_log(repo, filename="q.gz")
    r = await client.get("/logs?offset=0")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# collect single / collect-all
# ---------------------------------------------------------------------------


async def test_collect_single_success(client):
    _install_collector(
        collect_single=AsyncMock(
            return_value={"success": True, "target_id": 1, "log_id": 7, "filename": "x.gz"}
        )
    )
    r = await client.post("/logs/api/1/collect")
    assert r.status_code == 200
    assert r.json()["log_id"] == 7


async def test_collect_single_failure(client):
    _install_collector(
        collect_single=AsyncMock(return_value={"success": False, "error": "RedfishError"})
    )
    r = await client.post("/logs/api/1/collect")
    assert r.status_code == 400
    assert r.json()["detail"] == "RedfishError"


async def test_collect_all_success(client):
    _install_collector(collect_all=AsyncMock(return_value={"success": True, "collected": 3}))
    r = await client.post("/logs/api/collect-all")
    assert r.status_code == 200
    assert r.json()["collected"] == 3


async def test_collect_all_failure(client):
    _install_collector(collect_all=AsyncMock(return_value={"success": False, "error": "nope"}))
    r = await client.post("/logs/api/collect-all")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Download endpoint
# ---------------------------------------------------------------------------


async def test_download_log_success(client, repo, tmp_path):
    f = tmp_path / "bundle.gz"
    f.write_bytes(b"gzipped-bytes")
    log = await _make_log(repo, file_path=str(f), status="completed", file_size_bytes=13)
    r = await client.get(f"/logs/api/{log.id}/download")
    assert r.status_code == 200
    assert r.content == b"gzipped-bytes"


async def test_download_log_not_found(client, repo):
    r = await client.get("/logs/api/999999/download")
    assert r.status_code == 404


async def test_download_log_not_ready(client, repo):
    log = await _make_log(repo, status="pending")
    r = await client.get(f"/logs/api/{log.id}/download")
    assert r.status_code == 400


async def test_download_log_file_missing_on_disk(client, repo):
    log = await _make_log(repo, file_path="/data/does-not-exist.gz", status="completed")
    r = await client.get(f"/logs/api/{log.id}/download")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete (JSON 404 + HTML form)
# ---------------------------------------------------------------------------


async def test_delete_log_api_success(client, repo):
    _install_collector(delete_file=MagicMock())
    log = await _make_log(repo, filename="rm.gz")
    r = await client.delete(f"/logs/api/{log.id}")
    assert r.status_code == 200
    assert r.json()["message"] == "Log deleted successfully"


async def test_delete_log_api_not_found(client):
    _install_collector(delete_file=MagicMock())
    r = await client.delete("/logs/api/999999")
    assert r.status_code == 404


async def test_delete_log_form_success(client, repo):
    _install_collector(delete_file=MagicMock())
    log = await _make_log(repo, filename="del.gz")
    r = await client.post(
        f"/logs/{log.id}/delete",
        data={"csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 303


async def test_delete_log_form_not_found(client):
    _install_collector(delete_file=MagicMock())
    r = await client.post(
        "/logs/999999/delete",
        data={"csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 404
