# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra coverage for AlertManager: batch drain, dedup/retention, degraded/
cooldown stats shape, webhook Context verification, enqueue overflow, and the
subscription refresh fingerprint/permanent-failure handling.

Uses in-memory fakes for the repository and subscribers; pieces that require a
live SSE/network connection are intentionally not exercised here.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from src import alert_manager as am
from src.alert_manager import AlertManager
from src.redfish.alert_subscriber import AlertEvent
from src.redfish.webhook_subscriber import SubscriptionFailureType, SubscriptionResult


class FakeRepo:
    """Records batches and can be told to fail writes (DB-error path)."""

    def __init__(self, targets=None, fail=False):
        self.targets = targets or []
        self.fail = fail
        self.written = []
        self.deleted_before = []

    def decrypt_password(self, target):
        return "pw"

    async def get_all_targets(self, enabled_only=False):
        return self.targets

    async def get_active_targets(self):
        return self.targets

    async def create_alerts_batch(self, batch):
        if self.fail:
            raise RuntimeError("alert store down")
        self.written.extend(batch)
        return len(batch)

    async def delete_alerts_before(self, cutoff):
        self.deleted_before.append(cutoff)
        return 7


def _mgr(**kw):
    kw.setdefault("repository", FakeRepo())
    return AlertManager(**kw)


def _alert(tid=1, sev="Critical"):
    return AlertEvent(
        target_id=tid,
        target_name=f"n{tid}",
        target_bmc="10.0.0.1",
        severity=sev,
        message="m",
        message_id="T.1",
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )


# ---- enqueue overflow / custom callback ------------------------------------


def test_enqueue_queue_full_drops():
    mgr = _mgr(max_queue_size=1)
    mgr._enqueue(_alert())
    mgr._enqueue(_alert())  # queue full -> dropped
    assert mgr._alert_queue.qsize() == 1
    assert mgr._alerts_dropped == 1


def test_enqueue_invokes_custom_callback():
    seen = []
    mgr = _mgr(alert_callback=lambda a: seen.append(a.target_id))
    mgr._enqueue(_alert(tid=42))
    assert seen == [42]
    assert mgr._alerts_received == 1


# ---- webhook liveness verification (A2) ------------------------------------


async def test_verify_webhook_drops_dead_keeps_live():
    mgr = _mgr()
    deleted = []

    class _WH:
        def __init__(self, alive):
            self._alive = alive

        async def verify_subscription(self):
            return self._alive

        async def delete_subscription(self):
            deleted.append(True)

    mgr._webhook_subscribers[5] = _WH(alive=False)  # BMC GC'd it
    mgr._webhook_subscribers[6] = _WH(alive=True)
    mgr._subscription_types[5] = "webhook"
    mgr._subscription_types[6] = "webhook"
    mgr._webhook_verified_at[5] = 0.0  # due
    mgr._webhook_verified_at[6] = 0.0
    mgr._webhook_verify_interval = 0.0  # everything due
    await mgr._verify_webhook_subscriptions()
    # Dead one dropped (so the next refresh re-creates it); live one kept.
    assert 5 not in mgr._webhook_subscribers
    assert 6 in mgr._webhook_subscribers
    assert deleted == [True]


async def test_verify_webhook_seeds_new_without_probing():
    mgr = _mgr()

    class _WH:
        async def verify_subscription(self):
            raise AssertionError("a freshly-seen subscription must not be probed yet")

    mgr._webhook_subscribers[7] = _WH()
    await mgr._verify_webhook_subscriptions()  # first pass seeds, does not verify
    assert 7 in mgr._webhook_subscribers
    assert 7 in mgr._webhook_verified_at


# ---- policy engine dispatch (live alerts only) -----------------------------


async def test_on_alert_dispatches_to_policy_engine():
    seen = []

    class _FakeEngine:
        async def on_alert(self, alert):
            seen.append(alert.target_id)

    mgr = _mgr(policy_engine=_FakeEngine())
    mgr._on_alert(_alert(tid=9))  # live alert
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert seen == [9]


async def test_baseline_alert_does_not_dispatch_policy():
    seen = []

    class _FakeEngine:
        async def on_alert(self, alert):
            seen.append(alert.target_id)

    mgr = _mgr(policy_engine=_FakeEngine())
    mgr._on_baseline_alert(_alert(tid=9))  # historical re-pull -> must NOT collect
    await asyncio.sleep(0)
    assert seen == []


def test_dispatch_policy_without_running_loop_is_safe():
    # Called from a sync context with no running loop -> swallowed, no crash.
    mgr = _mgr(policy_engine=object())
    mgr._dispatch_policy(_alert())


# ---- flush / write-batch success + failure ---------------------------------


async def test_flush_batch_drains_queue():
    mgr = _mgr()
    mgr._enqueue(_alert())
    mgr._enqueue(_alert())
    await mgr._flush_batch()
    assert mgr._alerts_written == 2
    assert mgr._alert_queue.empty()


async def test_flush_batch_empty_noop():
    mgr = _mgr()
    await mgr._flush_batch()
    assert mgr._alerts_written == 0


async def test_write_batch_db_failure_returns_false():
    mgr = _mgr(repository=FakeRepo(fail=True))
    ok = await mgr._write_batch([_alert()])
    assert ok is False
    assert mgr._alerts_written == 0


# ---- batch processor loop drains then exits on stop ------------------------


async def test_batch_processor_loop_drains_and_stops():
    mgr = _mgr(batch_size=1, batch_interval=0.01)
    mgr._running = True
    mgr._enqueue(_alert())
    task = asyncio.create_task(mgr._batch_processor_loop())
    # Give the loop time to pick up and write the queued alert.
    for _ in range(50):
        if mgr._alerts_written >= 1:
            break
        await asyncio.sleep(0.02)
    mgr._running = False
    await asyncio.wait_for(task, timeout=2)
    assert mgr._alerts_written >= 1


async def test_batch_processor_retries_on_db_error():
    repo = FakeRepo(fail=True)
    mgr = _mgr(repository=repo, batch_size=1, batch_interval=0.01)
    mgr._running = True
    mgr._enqueue(_alert())
    task = asyncio.create_task(mgr._batch_processor_loop())
    await asyncio.sleep(0.1)  # let it attempt + fail at least once
    mgr._running = False
    # Recovery: allow writes again so the retained batch flushes on shutdown.
    repo.fail = False
    await asyncio.wait_for(task, timeout=2)
    assert mgr._alerts_written >= 1


# ---- cleanup / retention ----------------------------------------------------


async def test_cleanup_old_alerts_calls_delete():
    repo = FakeRepo()
    mgr = _mgr(repository=repo, retention_days=5)
    await mgr._cleanup_old_alerts()
    assert len(repo.deleted_before) == 1


# ---- webhook Context verification ------------------------------------------


class _WHStub:
    target_name = "n1"

    def parse_webhook_event(self, data):
        return [_alert(), _alert()]


async def test_webhook_context_match_enqueues():
    mgr = _mgr()
    mgr._webhook_subscribers[1] = _WHStub()
    n = await mgr.process_webhook_event(1, {"Context": "target_1", "Events": []})
    assert n == 2
    assert mgr._alert_queue.qsize() == 2


async def test_webhook_context_mismatch_rejected():
    mgr = _mgr()
    mgr._webhook_subscribers[1] = _WHStub()
    n = await mgr.process_webhook_event(1, {"Context": "target_999", "Events": []})
    assert n == 0
    assert mgr._alert_queue.qsize() == 0


async def test_webhook_missing_context_rejected():
    # A POST that omits Context entirely must be rejected, not waved through —
    # otherwise forged alerts bypass the check by simply leaving the field out.
    mgr = _mgr()
    mgr._webhook_subscribers[1] = _WHStub()
    n = await mgr.process_webhook_event(1, {"Events": []})  # no Context key
    assert n == 0
    assert mgr._alert_queue.qsize() == 0


# ---- get_stats including subscriber + webhook detail -----------------------


def test_get_stats_includes_subscriber_detail():
    mgr = _mgr()
    mgr._subscribers[1] = SimpleNamespace(
        target_id=1,
        target_name="n1",
        is_running=True,
        state=SimpleNamespace(value="connected"),
        consecutive_failures=0,
        failure_reason=None,
        time_in_current_state=1.5,
        next_retry_time=None,
        last_event_time=datetime.now(UTC),
    )
    mgr._webhook_subscribers[2] = SimpleNamespace(
        target_id=2, target_name="n2", is_subscribed=True, subscription_id="sub-2"
    )
    mgr._permanently_failed[3] = datetime.max.replace(tzinfo=UTC)
    stats = mgr.get_stats()
    assert stats["sse_subscriptions"] == 1
    assert stats["webhook_subscriptions"] == 1
    assert stats["active_subscriptions"] == 2
    assert stats["permanently_failed"] == 1
    # Folded-in CPER worker counters + per-subscriber detail.
    assert "cper_decoded" in stats
    types = {s["subscription_type"] for s in stats["subscribers"]}
    assert types == {"sse", "webhook"}


# ---- subscription refresh: fingerprint change + permanent-failure skip ------


def _target(tid=1, **kw):
    d = {
        "id": tid,
        "name": f"n{tid}",
        "host": "10.0.0.1",
        "base_url": "https://bmc",
        "username": "u",
        "encrypted_password": "enc",
        "alert_sse_endpoint": "/sse",
        "verify_ssl": False,
        "connection_mode": "direct",
        "enable_alert_subscription": True,
        "enabled": True,
    }
    d.update(kw)
    return SimpleNamespace(**d)


async def test_refresh_skips_permanently_failed(monkeypatch):
    repo = FakeRepo(targets=[_target(1)])
    mgr = _mgr(repository=repo)
    mgr._permanently_failed[1] = datetime.max.replace(tzinfo=UTC)
    started = []
    monkeypatch.setattr(mgr, "_start_subscription", lambda t: started.append(t.id))
    await mgr._refresh_subscriptions()
    assert started == []  # permanently-failed target skipped


async def test_refresh_resubscribes_on_fingerprint_change(monkeypatch):
    t = _target(1)
    repo = FakeRepo(targets=[t])
    mgr = _mgr(repository=repo)

    # Pretend target 1 is already subscribed with a stale fingerprint.
    class StubSub:
        target_id = 1
        stopped = False

        async def stop(self):
            self.stopped = True

    mgr._subscribers[1] = StubSub()
    mgr._sub_fingerprints[1] = "stale-fingerprint"

    restarted = []

    async def fake_start(target):
        restarted.append(target.id)

    monkeypatch.setattr(mgr, "_start_subscription", fake_start)
    await mgr._refresh_subscriptions()
    # Changed config -> old subscriber torn down and re-added.
    assert 1 not in mgr._subscribers
    assert restarted == [1]


async def test_start_webhook_permanent_failure_marks_target(monkeypatch):
    class StubWH:
        def __init__(self, **kw):
            self.target_name = kw["target_name"]

        async def create_subscription(self):
            return SubscriptionResult(
                success=False,
                failure_type=SubscriptionFailureType.PERMANENT,
                error_message="bad config",
            )

        async def delete_subscription(self):
            return True

    monkeypatch.setattr(am, "WebhookSubscriber", StubWH)
    mgr = _mgr(enable_webhook_fallback=True, baseline_pull_enabled=False)
    await mgr._start_webhook_subscription(_target(1), "pw")
    assert 1 in mgr._permanently_failed
    assert 1 not in mgr._webhook_subscribers


async def test_start_webhook_temporary_failure_retries_later(monkeypatch):
    class StubWH:
        def __init__(self, **kw):
            self.target_name = kw["target_name"]

        async def create_subscription(self):
            return SubscriptionResult(
                success=False,
                failure_type=SubscriptionFailureType.TEMPORARY,
                error_message="timeout",
            )

        async def delete_subscription(self):
            return True

    monkeypatch.setattr(am, "WebhookSubscriber", StubWH)
    mgr = _mgr(enable_webhook_fallback=True, baseline_pull_enabled=False)
    await mgr._start_webhook_subscription(_target(1), "pw")
    assert 1 not in mgr._permanently_failed  # temporary -> not permanent
    assert 1 not in mgr._webhook_subscribers


# ---- start() loopback-webhook guard disables fallback ----------------------


async def test_start_disables_loopback_webhook(monkeypatch):
    # localhost webhook base is unreachable from a BMC -> fallback disabled.
    monkeypatch.delenv("GYANAM_ALLOW_LOOPBACK_WEBHOOK", raising=False)
    mgr = _mgr(
        webhook_base_url="http://localhost:8081/redfish-webhook",
        enable_webhook_fallback=True,
        baseline_pull_enabled=False,
    )
    await mgr.start()
    try:
        assert mgr.enable_webhook_fallback is False
        assert mgr.force_webhook_mode is False
    finally:
        await mgr.stop()


# ---- RateLimiter sliding-window eviction -----------------------------------


def test_rate_limiter_evicts_old_timestamps():
    from datetime import timedelta

    rl = am.RateLimiter(max_alerts_per_minute=1)
    # Seed an old timestamp directly, then a fresh allow() should evict it.
    rl.windows[1].append(datetime.now(UTC) - timedelta(minutes=5))
    assert rl.allow(1) is True  # old entry evicted, budget available
    assert rl.allow(1) is False  # now at limit


@pytest.mark.parametrize("sev", ["Critical", "Warning", "OK"])
def test_on_alert_variants(sev):
    mgr = _mgr()
    mgr._on_alert(_alert(sev=sev))
    assert mgr._alert_queue.qsize() == 1


# ---- stop() tears down subscribers, webhooks, tasks ------------------------


async def test_stop_tears_down_subscribers_and_webhooks():
    mgr = _mgr(baseline_pull_enabled=False)

    class StubSub:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    class StubWH:
        def __init__(self):
            self.deleted = False

        async def delete_subscription(self):
            self.deleted = True

    order: list[str] = []

    class StubWHOrdered(StubWH):
        async def delete_subscription(self):
            order.append("delete")
            await super().delete_subscription()

    s, w = StubSub(), StubWHOrdered()
    mgr._subscribers[1] = s
    mgr._webhook_subscribers[2] = w
    mgr._running = True
    # Record flush order relative to the (slow) webhook deletes.
    orig_flush = mgr._flush_batch

    async def _rec_flush():
        order.append("flush")
        await orig_flush()

    mgr._flush_batch = _rec_flush
    # A queued alert must be flushed during stop().
    mgr._enqueue(_alert())
    await mgr.stop()
    assert s.stopped and w.deleted
    assert mgr.is_running is False
    assert not mgr._subscribers and not mgr._webhook_subscribers
    assert mgr._alerts_written == 1
    # O3: queued alerts are flushed BEFORE the slow webhook teardown, so a
    # SIGKILL during teardown can't lose them.
    assert order == ["flush", "delete"]


# ---- _start_subscription: force-webhook + SSE-supported routes -------------


async def test_start_subscription_force_webhook(monkeypatch):
    called = {}

    async def fake_webhook(target, password):
        called["id"] = target.id

    mgr = _mgr(force_webhook_mode=True, enable_webhook_fallback=True)
    monkeypatch.setattr(mgr, "_start_webhook_subscription", fake_webhook)
    await mgr._start_subscription(_target(1))
    assert called["id"] == 1


async def test_start_subscription_force_webhook_but_fallback_disabled():
    # force_webhook_mode on but fallback disabled -> logs + returns without sub.
    mgr = _mgr(force_webhook_mode=True, enable_webhook_fallback=False)
    await mgr._start_subscription(_target(1))
    assert 1 not in mgr._webhook_subscribers
    assert 1 not in mgr._subscribers


async def test_start_subscription_sse_supported(monkeypatch):
    async def fake_check(**kwargs):
        return SimpleNamespace(support=am.SSESupport.SUPPORTED, reason="ok")

    monkeypatch.setattr(am, "check_sse_capability", fake_check)
    sse_started = {}

    async def fake_sse(target, password):
        sse_started["id"] = target.id

    mgr = _mgr(baseline_pull_enabled=False)
    monkeypatch.setattr(mgr, "_start_sse_subscription", fake_sse)
    await mgr._start_subscription(_target(1))
    assert sse_started["id"] == 1


async def test_start_subscription_sse_unsupported_falls_back(monkeypatch):
    async def fake_check(**kwargs):
        return SimpleNamespace(support=am.SSESupport.NOT_SUPPORTED, reason="no sse")

    monkeypatch.setattr(am, "check_sse_capability", fake_check)
    wh_started = {}

    async def fake_wh(target, password):
        wh_started["id"] = target.id

    mgr = _mgr(enable_webhook_fallback=True, baseline_pull_enabled=False)
    monkeypatch.setattr(mgr, "_start_webhook_subscription", fake_wh)
    await mgr._start_subscription(_target(1))
    assert wh_started["id"] == 1


async def test_start_subscription_skips_permanently_failed():
    mgr = _mgr()
    mgr._permanently_failed[1] = datetime.max.replace(tzinfo=UTC)
    await mgr._start_subscription(_target(1))  # returns early, no crash
    assert 1 not in mgr._subscribers


async def test_start_sse_subscription_with_stub(monkeypatch):
    class StubSubscriber:
        def __init__(self, **kw):
            self.target_id = kw["target_id"]
            self.started = False

        async def start(self):
            self.started = True

    monkeypatch.setattr(am, "AlertSubscriber", StubSubscriber)
    mgr = _mgr(baseline_pull_enabled=False)
    await mgr._start_sse_subscription(_target(1), "pw")
    assert 1 in mgr._subscribers
    assert mgr._subscription_types[1] == "sse"


# ---- background loops run briefly with tiny intervals ----------------------


async def test_cleanup_loop_runs_once(monkeypatch):
    repo = FakeRepo()
    mgr = _mgr(repository=repo)
    mgr.cleanup_interval_hours = 0  # first_delay -> 0, interval -> 0
    mgr._running = True
    task = asyncio.create_task(mgr._cleanup_loop())
    for _ in range(50):
        if repo.deleted_before:
            break
        await asyncio.sleep(0.02)
    mgr._running = False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert len(repo.deleted_before) >= 1


async def test_baseline_repull_loop_pulls(monkeypatch):
    mgr = _mgr()
    mgr.baseline_repull_interval_minutes = 0  # interval -> 0 -> rapid cycling
    pulled = []

    async def fake_pull(tid):
        pulled.append(tid)

    monkeypatch.setattr(mgr, "_pull_baseline_for", fake_pull)
    mgr._subscribers[1] = SimpleNamespace()
    mgr._running = True
    task = asyncio.create_task(mgr._baseline_repull_loop())
    for _ in range(50):
        if pulled:
            break
        await asyncio.sleep(0.02)
    mgr._running = False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert 1 in pulled


# ---- batch processor overflow drops oldest when DB persistently fails ------


async def test_batch_processor_overflow_drops_oldest():
    repo = FakeRepo(fail=True)
    mgr = _mgr(repository=repo, batch_size=1, batch_interval=0.0)
    mgr._running = True
    # Enqueue far more than the retry cap (batch_size * 5 = 5) so overflow trims.
    for _ in range(20):
        mgr._enqueue(_alert())
    task = asyncio.create_task(mgr._batch_processor_loop())
    for _ in range(100):
        if mgr._alerts_dropped > 0:
            break
        await asyncio.sleep(0.02)
    mgr._running = False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert mgr._alerts_dropped > 0
