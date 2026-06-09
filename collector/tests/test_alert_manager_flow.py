# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for AlertManager start/stop, subscription refresh, and baseline pull."""

from types import SimpleNamespace

from src import alert_manager as am
from src.alert_manager import AlertManager
from src.redfish.webhook_subscriber import SubscriptionResult


class FakeRepo:
    def __init__(self, targets=None):
        self.targets = targets or []

    def decrypt_password(self, target):
        return "pw"

    async def get_all_targets(self, enabled_only=False):
        return self.targets


def _target(tid=1, **kw):
    d = {
        "id": tid,
        "name": f"n{tid}",
        "host": "10.0.0.1",
        "base_url": "https://bmc",
        "username": "u",
        "verify_ssl": False,
        "enable_alert_subscription": True,
        "enabled": True,
    }
    d.update(kw)
    return SimpleNamespace(**d)


async def test_start_and_stop():
    mgr = AlertManager(FakeRepo(), enabled=True)
    await mgr.start()
    assert mgr.is_running is True
    await mgr.stop()
    assert mgr.is_running is False


async def test_disabled_manager_does_not_start():
    mgr = AlertManager(FakeRepo(), enabled=False)
    await mgr.start()
    assert mgr.is_running is False


async def test_start_webhook_subscription(monkeypatch):
    class StubWH:
        def __init__(self, **kw):
            self.target_name = kw["target_name"]
            self.subscription_id = "1"
            self.is_subscribed = True

        async def create_subscription(self):
            return SubscriptionResult(success=True)

        async def delete_subscription(self):
            return True

    monkeypatch.setattr(am, "WebhookSubscriber", StubWH)
    mgr = AlertManager(FakeRepo(), enable_webhook_fallback=True)
    await mgr._start_webhook_subscription(_target(), "pw")
    assert 1 in mgr._webhook_subscribers


async def test_refresh_adds_new_targets(monkeypatch):
    repo = FakeRepo(targets=[_target(1), _target(2)])
    mgr = AlertManager(repo)
    started = []

    async def fake_start(t):
        started.append(t.id)

    monkeypatch.setattr(mgr, "_start_subscription", fake_start)
    await mgr._refresh_subscriptions()
    assert sorted(started) == [1, 2]


async def test_refresh_removes_stale_subscribers():
    repo = FakeRepo(targets=[])
    mgr = AlertManager(repo)

    class StubSub:
        stopped = False

        async def stop(self):
            self.stopped = True

    stub = StubSub()
    mgr._subscribers[7] = stub
    await mgr._refresh_subscriptions()
    assert 7 not in mgr._subscribers
    assert stub.stopped is True


async def test_pull_baseline_for(monkeypatch):
    async def fake_pull(**kwargs):
        return 3

    monkeypatch.setattr(am, "pull_baseline_alerts", fake_pull)
    mgr = AlertManager(FakeRepo())
    mgr._subscribers[1] = SimpleNamespace(
        target_id=1,
        target_name="n1",
        target_bmc="b",
        base_url="https://bmc",
        username="u",
        password="pw",
        verify_ssl=False,
    )
    await mgr._pull_baseline_for(1)
    assert mgr._alerts_baseline_pulled == 3


async def test_pull_baseline_for_unknown_target_noop(monkeypatch):
    mgr = AlertManager(FakeRepo())
    await mgr._pull_baseline_for(999)  # no subscriber -> no crash
    assert mgr._alerts_baseline_pulled == 0
