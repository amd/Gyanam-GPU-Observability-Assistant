# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for log-collector filename sanitization, path-traversal defense, and
the collect orchestration (single/all/_collect_from_target)."""

import pytest
from src.log_collector import LogCollector, sanitize_filename


def test_sanitize_filename_strips_unsafe_chars():
    # Only [A-Za-z0-9.-] survive; everything else becomes '_' (collapsed).
    assert sanitize_filename("gpu node/01") == "gpu_node_01"
    assert sanitize_filename("!!!") == "target"  # falls back when empty
    # The security invariant: no path separators survive (can't traverse).
    traversal = sanitize_filename("../../etc/passwd")
    assert "/" not in traversal and "\\" not in traversal
    assert "/" not in sanitize_filename("a/b/c")


def _collector(tmp_path):
    return LogCollector(repository=None, storage_dir=str(tmp_path / "logs"))


def test_delete_file_within_storage(tmp_path):
    lc = _collector(tmp_path)
    f = lc.storage_dir / "bundle.tar.gz"
    f.write_bytes(b"data")
    assert lc.delete_file(str(f)) is True
    assert not f.exists()


def test_delete_file_rejects_traversal(tmp_path):
    lc = _collector(tmp_path)
    # A secret file outside the storage dir must never be deletable.
    outside = tmp_path / "secret.txt"
    outside.write_text("keep me")
    assert lc.delete_file(str(outside)) is False
    assert lc.delete_file(str(lc.storage_dir / ".." / "secret.txt")) is False
    assert outside.exists()


def test_delete_missing_file_returns_false(tmp_path):
    lc = _collector(tmp_path)
    assert lc.delete_file(str(lc.storage_dir / "nope.gz")) is False


# ---- collect orchestration (download monkeypatched, DB via repo fixture) ----


@pytest.fixture
def lc(repo, tmp_path):
    return LogCollector(repository=repo, storage_dir=str(tmp_path / "logs"))


async def _mk(repo, name="n1", host="h1"):
    return await repo.create_target(name=name, host=host, username="u", password="p")


async def test_collect_single_target_not_found(lc):
    r = await lc.collect_single(999999)
    assert r["success"] is False and r["error"] == "Target not found"


async def test_collect_single_happy_writes_file(lc, repo, monkeypatch):
    target = await _mk(repo)
    monkeypatch.setattr(lc, "_collect_via_redfish", lambda _t: _async(b"tarball-bytes"))
    r = await lc.collect_single(target.id)
    assert r["success"] is True and r["file_size_bytes"] == len(b"tarball-bytes")
    assert (lc.storage_dir / r["filename"]).read_bytes() == b"tarball-bytes"


async def test_collect_single_lock_busy(lc, repo):
    target = await _mk(repo)
    lock = lc._get_target_lock(target.id)
    await lock.acquire()
    try:
        r = await lc.collect_single(target.id)
        assert r["success"] is False and "in progress" in r["error"]
    finally:
        lock.release()


async def test_collect_from_target_failure_records_db(lc, repo, monkeypatch):
    target = await _mk(repo)

    async def boom(_t):
        raise RuntimeError("download exploded")

    monkeypatch.setattr(lc, "_collect_via_redfish", boom)
    r = await lc.collect_single(target.id)
    assert r["success"] is False and r["error"] == "RuntimeError"  # type name only
    log = await repo.get_collected_log(r["log_id"])
    assert log.status == "failed"


async def test_collect_all_no_targets(lc):
    r = await lc.collect_all()
    assert r["success"] is True and "No active targets" in r["message"]


async def test_collect_all_bulk_guard(lc):
    lc._bulk_in_progress = True
    r = await lc.collect_all()
    assert r["success"] is False and "already in progress" in r["error"]


async def test_collect_all_summary_mixed(lc, repo, monkeypatch):
    await _mk(repo, name="ok", host="h-ok")
    await _mk(repo, name="bad", host="h-bad")

    async def fake(target):
        if target.name == "bad":
            raise RuntimeError("nope")
        return b"data"

    monkeypatch.setattr(lc, "_collect_via_redfish", fake)
    r = await lc.collect_all()
    assert r["total"] == 2 and r["succeeded"] == 1 and r["failed"] == 1


async def _async(value):
    return value


# ---- _collect_via_redfish (RedfishClient faked) + per-node prune ----


class _FakeRFResp:
    def __init__(self, success=True, content=b"blob", error_message=None, status_code=0):
        self.success = success
        self.content = content
        self.error_message = error_message
        self.status_code = status_code


class _FakeRFClient:
    def __init__(self, **kw):
        self._kw = kw
        self._last_initiate_status = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def collect_diagnostic_data(self, collect_endpoint=None, collect_body=None):
        self._last_initiate_status = 202
        return _FakeRFResp(success=True, content=b"redfish-blob")


async def test_collect_via_redfish_success(lc, repo, monkeypatch):
    target = await _mk(repo)
    monkeypatch.setattr("src.log_collector.RedfishClient", _FakeRFClient)
    assert await lc._collect_via_redfish(target) == b"redfish-blob"


async def test_collect_via_redfish_failure_raises(lc, repo, monkeypatch):
    target = await _mk(repo)

    class _Fail(_FakeRFClient):
        async def collect_diagnostic_data(self, collect_endpoint=None, collect_body=None):
            return _FakeRFResp(success=False, error_message="collection failed")

    monkeypatch.setattr("src.log_collector.RedfishClient", _Fail)
    with pytest.raises(RuntimeError):
        await lc._collect_via_redfish(target)


async def test_collect_via_redfish_rediscovers_on_404(lc, repo, monkeypatch):
    """A 404 on the configured endpoint triggers discovery, persist, and retry."""
    target = await _mk(repo)
    discovered = (
        "/redfish/v1/Managers/AMC/LogServices/Dump/Actions/LogService.CollectDiagnosticData"
    )
    calls = []

    class _Rediscover(_FakeRFClient):
        async def collect_diagnostic_data(self, collect_endpoint=None, collect_body=None):
            calls.append(collect_endpoint)
            if len(calls) == 1:  # original (wrong) endpoint -> initiate 404
                self._last_initiate_status = 404
                return _FakeRFResp(success=False, status_code=404, error_message="not found")
            self._last_initiate_status = 202
            return _FakeRFResp(success=True, content=b"after-discovery")

        async def discover_collect_endpoint(self):
            return discovered

    monkeypatch.setattr("src.log_collector.RedfishClient", _Rediscover)
    assert await lc._collect_via_redfish(target) == b"after-discovery"
    # Second attempt used the discovered endpoint, and it was persisted.
    assert len(calls) == 2 and calls[1] == discovered and calls[0] != discovered
    assert (await repo.get_target(target.id)).telemetry_endpoint == discovered


async def test_collect_via_redfish_404_no_discovery_raises(lc, repo, monkeypatch):
    """A 404 with nothing discoverable surfaces the failure (no silent success)."""
    target = await _mk(repo)
    original = target.telemetry_endpoint

    class _NoDisc(_FakeRFClient):
        async def collect_diagnostic_data(self, collect_endpoint=None, collect_body=None):
            self._last_initiate_status = 404
            return _FakeRFResp(success=False, status_code=404, error_message="not found")

        async def discover_collect_endpoint(self):
            return None

    monkeypatch.setattr("src.log_collector.RedfishClient", _NoDisc)
    with pytest.raises(RuntimeError):
        await lc._collect_via_redfish(target)
    # Endpoint left unchanged when discovery finds nothing.
    assert (await repo.get_target(target.id)).telemetry_endpoint == original


async def test_collect_via_redfish_attachment_404_does_not_rediscover(lc, repo, monkeypatch):
    """A 404 AFTER a successful initiate (e.g. expired attachment) must NOT
    rediscover/clobber the endpoint — the action URI was correct."""
    target = await _mk(repo)
    original = target.telemetry_endpoint
    discover_called = []

    class _AttachMiss(_FakeRFClient):
        async def collect_diagnostic_data(self, collect_endpoint=None, collect_body=None):
            self._last_initiate_status = 202  # initiate succeeded
            return _FakeRFResp(success=False, status_code=404, error_message="attachment gone")

        async def discover_collect_endpoint(self):
            discover_called.append(True)
            return "/some/other/endpoint"

    monkeypatch.setattr("src.log_collector.RedfishClient", _AttachMiss)
    with pytest.raises(RuntimeError):
        await lc._collect_via_redfish(target)
    assert discover_called == []  # rediscovery NOT triggered
    assert (await repo.get_target(target.id)).telemetry_endpoint == original


async def test_collect_prunes_older_same_node_log(lc, repo, monkeypatch):
    target = await _mk(repo)
    lc.storage_dir.mkdir(parents=True, exist_ok=True)
    old_path = lc.storage_dir / "old.gz"
    old_path.write_bytes(b"old")
    old = await repo.create_collected_log(
        target_id=target.id,
        target_name=target.name,
        target_host=target.host,
        filename="old.gz",
        file_path=str(old_path),
    )
    await repo.update_collected_log(old.id, status="completed", file_size_bytes=3)

    monkeypatch.setattr(lc, "_collect_via_redfish", lambda _t: _async(b"new-bytes"))
    r = await lc.collect_single(target.id)

    assert r["success"] is True
    # Older same-node bundle (and its file) are superseded and removed.
    assert await repo.get_collected_log(old.id) is None
    assert not old_path.exists()
