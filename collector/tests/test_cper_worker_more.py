# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Loop + decode-path coverage for CperEnrichmentWorker.

Complements test_cper_worker.py by driving the periodic _loop (resume count,
maintenance, batch drain, adaptive pacing, cancellation) and the remaining
_enrich_one branches (CperGoneError, successful decode, decode-None failure).
"""

import asyncio

from src import cper_worker
from src.cper_worker import CperEnrichmentWorker
from src.database.repository import PendingCper


class _LoopRepo:
    """Fake repository that feeds the worker one batch, then stops the loop."""

    def __init__(self):
        self.calls = 0
        self.worker = None
        self.batches = []
        self.requeued = False
        self.finalized = False

    async def count_cper_by_status(self):
        return {"pending": 1, "decoded": 2, "fetch_failed": 1}

    async def requeue_failed_cper(self, older_than_minutes, limit=0):
        self.requeued = True
        return 1  # non-zero -> triggers the "Requeued N" log line

    async def finalize_exhausted_cper(self, max_attempts):
        self.finalized = True
        return 0

    async def get_pending_cper_alerts(self, limit, max_attempts, attempt_cooldown_seconds):
        self.calls += 1
        if self.calls == 1:
            return [PendingCper(id=1, target_id=1, uri=None, attempts=0, has_decoded=False)]
        # Second cycle: stop the loop and report an empty backlog.
        self.worker._running = False
        return []

    async def set_cper_results_batch(self, results):
        self.batches.append(results)
        return len(results)


async def test_loop_drains_batch_then_stops():
    repo = _LoopRepo()
    # batch_size=1 so the single-row batch is "full" -> adaptive fast path (0.1s).
    worker = CperEnrichmentWorker(
        repo,
        enabled=True,
        poll_interval_seconds=0,
        batch_size=1,
        maintenance_every_cycles=1,
    )
    repo.worker = worker
    worker._running = True
    await worker._loop()

    # Resume count surfaced, maintenance ran, and the no_data row was persisted.
    assert worker.stats()["cper_backlog"] == {"pending": 1, "decoded": 2, "fetch_failed": 1}
    assert repo.requeued is True and repo.finalized is True
    assert repo.batches[0] == [{"id": 1, "status": "no_data"}]


class _ErrLoopRepo(_LoopRepo):
    async def get_pending_cper_alerts(self, limit, max_attempts, attempt_cooldown_seconds):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("db exploded")  # inner try/except -> logged, loop continues
        self.worker._running = False
        return []


async def test_loop_guarded_enrich_crash_still_advances_attempt(monkeypatch):
    # An UNEXPECTED exception from _enrich_one must still yield an outcome that
    # increments the attempt counter, so a poison row finalizes instead of being
    # re-selected forever.
    repo = _LoopRepo()
    worker = CperEnrichmentWorker(
        repo,
        enabled=True,
        poll_interval_seconds=0,
        batch_size=1,
        maintenance_every_cycles=1,
        max_attempts=3,
    )
    repo.worker = worker

    async def _boom(row):
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(worker, "_enrich_one", _boom)
    worker._running = True
    await worker._loop()
    assert repo.batches[0] == [{"id": 1, "status": "pending", "increment_attempt": True}]


async def test_loop_swallows_cycle_exception():
    repo = _ErrLoopRepo()
    worker = CperEnrichmentWorker(
        repo,
        enabled=True,
        poll_interval_seconds=0,
        batch_size=5,
        maintenance_every_cycles=1,
    )
    repo.worker = worker
    worker._running = True
    await worker._loop()  # must not raise despite the first-cycle error
    assert repo.calls == 2


class _BlockingRepo:
    """Keeps the loop parked in an await so stop() can cancel it."""

    async def count_cper_by_status(self):
        return {}

    async def requeue_failed_cper(self, older_than_minutes, limit=0):
        return 0

    async def finalize_exhausted_cper(self, max_attempts):
        return 0

    async def get_pending_cper_alerts(self, limit, max_attempts, attempt_cooldown_seconds):
        await asyncio.sleep(3600)  # park here until cancelled
        return []

    async def set_cper_results_batch(self, results):
        return 0


async def test_start_then_stop_cancels_loop():
    worker = CperEnrichmentWorker(_BlockingRepo(), enabled=True, maintenance_every_cycles=1)
    await worker.start()
    assert worker._task is not None
    await asyncio.sleep(0.02)  # let the loop reach the blocking await
    await worker.stop()  # cancels the task -> CancelledError handled internally
    assert worker._task is None


# ---- _enrich_one: remaining fetch/decode branches ---------------------------


class _OneRepo:
    def __init__(self, target):
        self._target = target

    async def get_target(self, target_id):
        return self._target

    def decrypt_password(self, target):
        return "pw"


class _Target:
    base_url = "https://10.0.0.5"
    username = "u"
    verify_ssl = False


def _worker(repo):
    return CperEnrichmentWorker(repo, enabled=True, max_attempts=3)


async def test_enrich_one_cper_gone_is_unavailable(monkeypatch):
    async def _gone(**kwargs):
        raise cper_worker.cper_decoder.CperGoneError("rotated out")

    monkeypatch.setattr(cper_worker.cper_decoder, "fetch_cper_attachment", _gone)
    w = _worker(_OneRepo(_Target()))
    row = PendingCper(id=9, target_id=1, uri="/a", attempts=0, has_decoded=False)
    assert await w._enrich_one(row) == {"id": 9, "status": "unavailable"}


async def test_enrich_one_successful_decode(monkeypatch):
    async def _fetch(**kwargs):
        return b"RAWCPER"

    async def _decode(data, cper_convert_path, timeout):
        return {"sections": [{"type": "x"}]}

    monkeypatch.setattr(cper_worker.cper_decoder, "fetch_cper_attachment", _fetch)
    monkeypatch.setattr(cper_worker.cper_decoder, "decode_cper", _decode)
    monkeypatch.setattr(cper_worker.cper_decoder, "enrich_amd_sections", lambda d: d)
    monkeypatch.setattr(cper_worker.cper_decoder, "summarize_cper", lambda d: "REFINED")

    w = _worker(_OneRepo(_Target()))
    row = PendingCper(id=10, target_id=1, uri="/a", attempts=0, has_decoded=False)
    res = await w._enrich_one(row)
    assert res["id"] == 10 and res["status"] == "decoded"
    assert res["refined_message"] == "REFINED" and res["decoded"] == {"sections": [{"type": "x"}]}
    assert w.stats()["cper_decoded"] == 1


async def test_enrich_one_decode_none_failure(monkeypatch):
    async def _fetch(**kwargs):
        return b"RAWCPER"

    async def _decode(data, cper_convert_path, timeout):
        return None

    monkeypatch.setattr(cper_worker.cper_decoder, "fetch_cper_attachment", _fetch)
    monkeypatch.setattr(cper_worker.cper_decoder, "decode_cper", _decode)

    w = _worker(_OneRepo(_Target()))
    # attempts=2 -> this is the 3rd (== max) -> terminal decode_failed.
    row = PendingCper(id=11, target_id=1, uri="/a", attempts=2, has_decoded=False)
    res = await w._enrich_one(row)
    assert res == {"id": 11, "status": "decode_failed", "increment_attempt": True}
    assert w.stats()["cper_failed"] == 1
    # attempts=0 -> not terminal -> stays pending.
    row2 = PendingCper(id=12, target_id=1, uri="/a", attempts=0, has_decoded=False)
    res2 = await w._enrich_one(row2)
    assert res2["status"] == "pending" and res2["increment_attempt"] is True
