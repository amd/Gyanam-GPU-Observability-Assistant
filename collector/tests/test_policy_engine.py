# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the automated diagnostic-log-collection policy engine."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.config import PolicyConfig
from src.policy import PolicyEngine

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _FakeCollector:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    async def collect_single(self, target_id, *, trigger="manual", trigger_message_id=None):
        self.calls.append((target_id, trigger, trigger_message_id))
        return {"success": self.success, "error": None if self.success else "boom"}


class _FakeRepo:
    def __init__(self, last_policy_time=None):
        self._last = last_policy_time

    async def get_last_policy_collection_time(self, target_id):
        return self._last


def _alert(severity="Critical", message_id="Fault.1", target_id=7):
    return SimpleNamespace(severity=severity, message_id=message_id, target_id=target_id)


def _engine(cfg=None, collector=None, repo=None):
    return PolicyEngine(
        repo or _FakeRepo(), collector or _FakeCollector(), cfg or PolicyConfig(), now=lambda: _NOW
    )


# ---- triggering ------------------------------------------------------------


async def test_critical_event_triggers_collection():
    col = _FakeCollector()
    eng = _engine(collector=col)
    assert await eng.on_alert(_alert(severity="Critical")) is True
    assert col.calls == [(7, "policy", "Fault.1")]


async def test_fatal_event_triggers_collection():
    col = _FakeCollector()
    assert await _engine(collector=col).on_alert(_alert(severity="Fatal")) is True
    assert col.calls[0][1] == "policy"


async def test_warning_event_does_not_trigger():
    col = _FakeCollector()
    assert await _engine(collector=col).on_alert(_alert(severity="Warning")) is False
    assert col.calls == []


# ---- enablement / operating mode -------------------------------------------


async def test_disabled_policy_never_collects():
    col = _FakeCollector()
    eng = _engine(PolicyConfig(enabled=False), collector=col)
    assert await eng.on_alert(_alert()) is False
    assert col.calls == []


async def test_operating_mode_disabled_never_collects():
    col = _FakeCollector()
    eng = _engine(PolicyConfig(operating_mode="Disabled"), collector=col)
    assert await eng.on_alert(_alert()) is False
    assert col.calls == []


async def test_alert_only_mode_evaluates_but_does_not_collect():
    col = _FakeCollector()
    eng = _engine(PolicyConfig(operating_mode="AlertOnly"), collector=col)
    assert await eng.on_alert(_alert()) is False
    assert col.calls == []


# ---- rearm (hysteresis) ----------------------------------------------------


async def test_rearm_active_suppresses_collection():
    # Last policy collection 1h ago; rearm window is 2h -> suppressed.
    repo = _FakeRepo(last_policy_time=_NOW - timedelta(hours=1))
    col = _FakeCollector()
    eng = _engine(PolicyConfig(rearm_seconds=7200), collector=col, repo=repo)
    assert await eng.on_alert(_alert()) is False
    assert col.calls == []


async def test_rearm_expired_allows_collection():
    repo = _FakeRepo(last_policy_time=_NOW - timedelta(hours=3))  # older than 2h
    col = _FakeCollector()
    eng = _engine(PolicyConfig(rearm_seconds=7200), collector=col, repo=repo)
    assert await eng.on_alert(_alert()) is True
    assert len(col.calls) == 1


async def test_rearm_handles_naive_db_timestamp():
    # SQLite func.now() yields a naive timestamp; engine must normalize it.
    repo = _FakeRepo(last_policy_time=(_NOW - timedelta(minutes=30)).replace(tzinfo=None))
    eng = _engine(PolicyConfig(rearm_seconds=7200), repo=repo)
    assert await eng.on_alert(_alert()) is False  # 30min < 2h -> suppressed


# ---- message-id allow-list -------------------------------------------------


async def test_message_id_allow_list_matches():
    col = _FakeCollector()
    eng = _engine(PolicyConfig(trigger_message_ids=["Fault.1", "Fault.2"]), collector=col)
    assert await eng.on_alert(_alert(message_id="Fault.2")) is True


async def test_message_id_allow_list_excludes():
    col = _FakeCollector()
    eng = _engine(PolicyConfig(trigger_message_ids=["Fault.1"]), collector=col)
    assert await eng.on_alert(_alert(message_id="Other.9")) is False
    assert col.calls == []


# ---- robustness ------------------------------------------------------------


async def test_missing_target_id_is_ignored():
    col = _FakeCollector()
    eng = _engine(collector=col)
    assert (
        await eng.on_alert(SimpleNamespace(severity="Critical", message_id="x", target_id=None))
        is False
    )


async def test_collector_failure_reported_as_false():
    col = _FakeCollector(success=False)
    assert await _engine(collector=col).on_alert(_alert()) is False
    assert len(col.calls) == 1  # attempted


async def test_engine_never_raises_on_bad_repo():
    class _Boom:
        async def get_last_policy_collection_time(self, tid):
            raise RuntimeError("db down")

    eng = _engine(repo=_Boom())
    # Should swallow and return False, not propagate.
    assert await eng.on_alert(_alert()) is False


# ---- repository rearm-time tracking (real DB) ------------------------------


async def test_repo_tracks_policy_collection_time(repo):
    t = await repo.create_target(name="n", host="h", username="u", password="p")
    assert await repo.get_last_policy_collection_time(t.id) is None

    # A policy collection counts...
    await repo.create_collected_log(
        target_id=t.id,
        target_name="n",
        target_host="h",
        filename="f1",
        file_path="/p1",
        status="completed",
        trigger="policy",
        trigger_message_id="M.1",
    )
    assert await repo.get_last_policy_collection_time(t.id) is not None


async def test_repo_rearm_ignores_manual_and_failed(repo):
    t = await repo.create_target(name="n2", host="h2", username="u", password="p")
    # A manual collection must not hold off a policy collection.
    await repo.create_collected_log(
        target_id=t.id,
        target_name="n2",
        target_host="h2",
        filename="m1",
        file_path="/m1",
        status="completed",
        trigger="manual",
    )
    # A failed policy collection must not count either.
    await repo.create_collected_log(
        target_id=t.id,
        target_name="n2",
        target_host="h2",
        filename="p1",
        file_path="/p1",
        status="failed",
        trigger="policy",
    )
    assert await repo.get_last_policy_collection_time(t.id) is None
