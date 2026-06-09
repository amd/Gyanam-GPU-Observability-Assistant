# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for AlertManager helpers: rate limiting, queueing, webhook, URL checks."""

from datetime import UTC, datetime

from src.alert_manager import AlertManager, RateLimiter, _is_unreachable_from_bmc
from src.redfish.alert_subscriber import AlertEvent


class _FakeRepo:
    def __init__(self):
        self.written = []

    async def create_alerts_batch(self, batch):
        self.written.extend(batch)
        return len(batch)


def _mgr(**kw):
    return AlertManager(repository=_FakeRepo(), **kw)


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


# ---- RateLimiter ----


def test_rate_limiter_allows_up_to_limit():
    rl = RateLimiter(max_alerts_per_minute=2)
    assert rl.allow(1) is True
    assert rl.allow(1) is True
    assert rl.allow(1) is False  # third within the window
    # a different target has its own budget
    assert rl.allow(2) is True


# ---- webhook URL validation ----


def test_is_unreachable_from_bmc():
    assert _is_unreachable_from_bmc("http://localhost:8081/x")[0] is True
    assert _is_unreachable_from_bmc("http://0.0.0.0:8081/x")[0] is True
    assert _is_unreachable_from_bmc("http://127.0.0.1/x")[0] is True
    assert _is_unreachable_from_bmc("http://collector:8081/x")[0] is False
    assert _is_unreachable_from_bmc("not-a-url")[0] is True


# ---- enqueue / rate-limit / baseline bypass ----


def test_on_alert_enqueues():
    mgr = _mgr()
    mgr._on_alert(_alert())
    assert mgr._alert_queue.qsize() == 1
    assert mgr._alerts_received == 1


def test_on_alert_rate_limited_drops():
    mgr = _mgr(max_alerts_per_minute=1)
    mgr._on_alert(_alert())
    mgr._on_alert(_alert())  # dropped by rate limit
    assert mgr._alert_queue.qsize() == 1
    assert mgr._alerts_dropped == 1


def test_baseline_alert_bypasses_rate_limit():
    mgr = _mgr(max_alerts_per_minute=1)
    for _ in range(5):
        mgr._on_baseline_alert(_alert())
    # No rate-limit drops for baseline.
    assert mgr._alert_queue.qsize() == 5
    assert mgr._alerts_dropped == 0


# ---- batch write ----


async def test_write_batch_counts_written():
    mgr = _mgr()
    await mgr._write_batch([_alert(), _alert()])
    assert mgr._alerts_written == 2


async def test_write_batch_empty_noop():
    mgr = _mgr()
    await mgr._write_batch([])
    assert mgr._alerts_written == 0


# ---- webhook event processing ----


async def test_process_webhook_event_enqueues():
    mgr = _mgr()

    class _Stub:
        target_name = "n1"

        def parse_webhook_event(self, data):
            return [_alert(), _alert()]

    mgr._webhook_subscribers[1] = _Stub()
    await mgr.process_webhook_event(1, {"Events": []})
    assert mgr._alert_queue.qsize() == 2


async def test_process_webhook_unknown_target_noop():
    mgr = _mgr()
    await mgr.process_webhook_event(999, {"Events": []})
    assert mgr._alert_queue.qsize() == 0


# ---- stats ----


def test_get_stats_shape():
    mgr = _mgr()
    stats = mgr.get_stats()
    for key in (
        "enabled",
        "running",
        "active_subscriptions",
        "alerts_received",
        "alerts_written",
        "alerts_baseline_pulled",
    ):
        assert key in stats
