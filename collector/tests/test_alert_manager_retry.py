# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for auto-retry of permanently-failed webhook alert subscriptions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from src import alert_manager as am
from src.alert_manager import AlertManager
from src.api.routes.alerts import _permanent_failure_retries
from src.config import AlertsConfig
from src.redfish.webhook_subscriber import SubscriptionFailureType, SubscriptionResult


class _FakeRepo:
    def __init__(self, targets):
        self.targets = targets

    async def get_all_targets(self, enabled_only=False):
        return self.targets

    def decrypt_password(self, target):
        return "pw"


def _target(tid=1):
    return SimpleNamespace(
        id=tid,
        name=f"n{tid}",
        host="10.0.0.1",
        base_url="https://10.0.0.1",
        username="admin",
        encrypted_password="enc",
        alert_sse_endpoint=None,
        verify_ssl=False,
        connection_mode="redfish",
        enabled=True,
        enable_alert_subscription=True,
    )


def _mgr(targets, **kw):
    """Build a manager that records started target IDs."""
    mgr = AlertManager(repository=_FakeRepo(targets), **kw)
    started: list[int] = []

    async def _record(target):
        started.append(target.id)

    mgr._start_subscription = _record  # type: ignore[method-assign]
    return mgr, started


async def test_elapsed_cooldown_retries_subscription():
    target = _target()
    mgr, started = _mgr([target], permanent_failure_retry_hours=6)
    mgr._permanently_failed[target.id] = datetime.now(UTC) - timedelta(minutes=1)

    await mgr._refresh_subscriptions()

    assert target.id not in mgr._permanently_failed
    assert started == [target.id]


async def test_pending_cooldown_keeps_target_skipped():
    target = _target()
    mgr, started = _mgr([target], permanent_failure_retry_hours=6)
    retry_at = datetime.now(UTC) + timedelta(hours=5)
    mgr._permanently_failed[target.id] = retry_at

    await mgr._refresh_subscriptions()

    assert mgr._permanently_failed[target.id] == retry_at
    assert started == []


async def test_auto_retry_disabled_never_retries():
    target = _target()
    mgr, started = _mgr([target], permanent_failure_retry_hours=0)
    sentinel = datetime.now(UTC) - timedelta(days=365)
    mgr._permanently_failed[target.id] = sentinel

    await mgr._refresh_subscriptions()

    assert mgr._permanently_failed[target.id] == sentinel
    assert started == []


class _FailingWebhookSubscriber:
    def __init__(self, **kw):
        pass

    async def create_subscription(self):
        return SubscriptionResult(
            success=False,
            failure_type=SubscriptionFailureType.PERMANENT,
            error_message="PropertyValueFormatError",
        )


async def test_permanent_failure_records_retry_deadline(monkeypatch):
    monkeypatch.setattr(am, "WebhookSubscriber", _FailingWebhookSubscriber)
    target = _target()
    mgr, _ = _mgr([target], permanent_failure_retry_hours=6)

    before = datetime.now(UTC)
    await mgr._start_webhook_subscription(target, "pw")
    after = datetime.now(UTC)

    retry_at = mgr._permanently_failed[target.id]
    assert before + timedelta(hours=6) <= retry_at <= after + timedelta(hours=6)
    # Failed setup must not leave a subscriber registered.
    assert target.id not in mgr._webhook_subscribers


async def test_permanent_failure_with_auto_retry_off_stores_sentinel(monkeypatch):
    monkeypatch.setattr(am, "WebhookSubscriber", _FailingWebhookSubscriber)
    target = _target()
    mgr, _ = _mgr([target], permanent_failure_retry_hours=0)

    await mgr._start_webhook_subscription(target, "pw")

    assert mgr._permanently_failed[target.id] == datetime.max.replace(tzinfo=UTC)


def test_get_stats_reports_retry_deadlines():
    mgr, _ = _mgr([])
    retry_at = datetime.now(UTC) + timedelta(hours=6)
    mgr._permanently_failed[7] = retry_at

    stats = mgr.get_stats()

    assert stats["permanently_failed"] == 1
    assert stats["permanently_failed_targets"] == [7]
    assert stats["permanent_failure_retries"] == [
        {"target_id": 7, "next_retry_at": retry_at.isoformat()}
    ]


def test_retry_deadlines_handle_disabled_auto_retry():
    retry_at = datetime.now(UTC) + timedelta(hours=6)
    stats = {
        "permanent_failure_retries": [
            {"target_id": 7, "next_retry_at": retry_at.isoformat()},
            {
                "target_id": 8,
                "next_retry_at": datetime.max.replace(tzinfo=UTC).isoformat(),
            },
        ]
    }

    assert _permanent_failure_retries(stats) == {
        7: retry_at.isoformat(),
        8: None,
    }


def test_retry_deadlines_accept_old_stats_shape():
    assert _permanent_failure_retries({"permanently_failed_targets": [7]}) == {}


def test_negative_retry_hours_are_rejected():
    with pytest.raises(ValidationError):
        AlertsConfig(permanent_failure_retry_hours=-1)
