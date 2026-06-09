# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for the extracted CperEnrichmentWorker (isolated from AlertManager)."""

from src import cper_worker
from src.cper_worker import CperEnrichmentWorker
from src.database.repository import PendingCper


class _FakeRepo:
    """Minimal repository stand-in for exercising worker logic in isolation."""

    def __init__(self, decoded=None, target=None):
        self._decoded = decoded
        self._target = target
        self.results = []

    async def get_alert_cper(self, alert_id):
        return self._decoded

    async def get_target(self, target_id):
        return self._target


def _worker(repo):
    return CperEnrichmentWorker(repo, enabled=True, max_attempts=3)


async def test_start_noop_when_disabled():
    w = CperEnrichmentWorker(_FakeRepo(), enabled=False)
    await w.start()
    assert w._task is None
    await w.stop()  # safe no-op


def test_stats_shape():
    w = _worker(_FakeRepo())
    s = w.stats()
    assert s["cper_decoded"] == 0 and s["cper_failed"] == 0 and s["cper_backlog"] == {}


def test_refresh_backlog_caches_counts():
    w = _worker(_FakeRepo())
    w._refresh_backlog({"pending": 5, "decoded": 10})
    assert w.stats()["cper_backlog"] == {"pending": 5, "decoded": 10}


async def test_enrich_one_no_data():
    w = _worker(_FakeRepo())
    row = PendingCper(id=1, target_id=1, uri=None, attempts=0, has_decoded=False)
    assert await w._enrich_one(row) == {"id": 1, "status": "no_data"}


async def test_enrich_one_resummarize_from_stored(monkeypatch):
    # has_decoded=True -> re-summarize locally (no fetch), returns decoded result.
    monkeypatch.setattr(cper_worker.cper_decoder, "enrich_amd_sections", lambda d: d)
    monkeypatch.setattr(cper_worker.cper_decoder, "summarize_cper", lambda d: "SUMMARY")
    w = _worker(_FakeRepo(decoded={"sections": []}))
    row = PendingCper(id=7, target_id=1, uri="/a", attempts=0, has_decoded=True)
    res = await w._enrich_one(row)
    assert res["id"] == 7 and res["status"] == "decoded" and res["refined_message"] == "SUMMARY"
    assert w.stats()["cper_decoded"] == 1


async def test_enrich_one_target_gone_is_unavailable():
    w = _worker(_FakeRepo(target=None))
    row = PendingCper(id=3, target_id=99, uri="/a", attempts=0, has_decoded=False)
    assert await w._enrich_one(row) == {"id": 3, "status": "unavailable"}


async def test_enrich_one_fetch_failure_increments(monkeypatch):
    async def _boom(**kwargs):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(cper_worker.cper_decoder, "fetch_cper_attachment", _boom)

    class _T:
        base_url = "https://bmc"
        username = "u"
        verify_ssl = False

    repo = _FakeRepo(target=_T())
    repo.decrypt_password = lambda t: "pw"
    w = _worker(repo)
    # attempts=2 -> this failure is the 3rd (== max) -> terminal fetch_failed.
    row = PendingCper(id=4, target_id=1, uri="/a", attempts=2, has_decoded=False)
    res = await w._enrich_one(row)
    assert res == {"id": 4, "status": "fetch_failed", "increment_attempt": True}
    assert w.stats()["cper_failed"] == 1
    # attempts=0 -> not terminal yet -> stays pending for retry.
    row2 = PendingCper(id=5, target_id=1, uri="/a", attempts=0, has_decoded=False)
    res2 = await w._enrich_one(row2)
    assert res2["status"] == "pending" and res2["increment_attempt"] is True
