# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Async tests for the alert store (run against SQLite; no PostgreSQL needed)."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from src.database.repository import TargetRepository
from src.redfish.alert_subscriber import AlertEvent


def _mk(target_id, message, severity, event_ts, source_id, raw=None):
    return AlertEvent(
        target_id=target_id,
        target_name=f"n{target_id}",
        target_bmc=f"10.0.0.{target_id}",
        severity=severity,
        message=message,
        message_id=message,
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=event_ts,
        received_at=datetime.now(UTC),
        source_id=source_id,
        raw=raw or {"Message": message, "MessageArgs": ["x"]},
    )


async def _repo(tmp_path):
    repo = TargetRepository(
        database_url=f"sqlite:///{tmp_path}/targets.db",
        encryption_key=Fernet.generate_key().decode(),
        alerts_database_url=f"sqlite:///{tmp_path}/alerts.db",
    )
    await repo.init_db()
    return repo


def test_requires_alerts_database_url():
    with pytest.raises(ValueError):
        TargetRepository("sqlite:///x.db", Fernet.generate_key().decode(), alerts_database_url="")


async def test_dedup_by_source_id(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    ev = _mk(1, "crit", "Critical", now, "/e/1")
    assert await repo.create_alerts_batch([ev]) == 1
    assert await repo.create_alerts_batch([ev]) == 0  # re-pull dedups
    assert await repo.create_alerts_batch([ev, ev]) == 0  # in-batch dup too
    await repo.close()


async def test_dedup_without_source_id_uses_timestamp(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    a = _mk(1, "x", "Critical", now, None)
    b = _mk(1, "x", "Critical", now, None)  # same ts+msg -> same key
    assert await repo.create_alerts_batch([a]) == 1
    assert await repo.create_alerts_batch([b]) == 0
    await repo.close()


async def test_ordering_and_window(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [
            _mk(1, "A", "Critical", now - timedelta(hours=2), "/e/A"),
            _mk(1, "B", "Warning", now - timedelta(days=10), "/e/B"),
            _mk(1, "C", "Critical", now - timedelta(hours=1), "/e/C"),
        ]
    )
    # 7-day window, latest-first by occurrence; B (10d) excluded.
    rows = await repo.get_alerts(
        since=now - timedelta(hours=168), severity_in=["Critical", "Warning"]
    )
    assert [r.message for r in rows] == ["C", "A"]
    # All-time includes B, ordered last.
    allrows = await repo.get_alerts(since=None)
    assert [r.message for r in allrows] == ["C", "A", "B"]
    await repo.close()


async def test_count_and_grouped(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [
            _mk(1, "a", "Critical", now, "/e/a"),
            _mk(1, "b", "Warning", now, "/e/b"),
            _mk(2, "c", "Critical", now, "/e/c"),
        ]
    )
    assert await repo.count_alerts(severity_in=["Critical", "Warning"]) == 3
    assert await repo.count_alerts(severity="Critical") == 2
    assert await repo.count_alerts(search="b") == 1
    grouped = await repo.count_alerts_by_target_severity(since=now - timedelta(hours=168))
    assert grouped.get((1, "Critical")) == 1
    assert grouped.get((1, "Warning")) == 1
    assert grouped.get((2, "Critical")) == 1
    await repo.close()


async def test_pagination(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [_mk(1, f"m{i}", "Critical", now - timedelta(minutes=i), f"/e/{i}") for i in range(5)]
    )
    page1 = await repo.get_alerts(limit=2, offset=0)
    page2 = await repo.get_alerts(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r.message for r in page1}.isdisjoint({r.message for r in page2})
    await repo.close()


async def test_include_raw_vs_deferred(tmp_path):
    from sqlalchemy import inspect as sa_inspect

    repo = await _repo(tmp_path)
    await repo.create_alerts_batch([_mk(1, "r", "Critical", datetime.now(UTC), "/e/r")])
    deferred = (await repo.get_alerts(limit=1))[0]
    assert "raw_data" in sa_inspect(deferred).unloaded
    full = (await repo.get_alerts(limit=1, include_raw=True))[0]
    assert isinstance(full.raw_data, dict)
    await repo.close()


async def test_cursor_roundtrip(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    assert await repo.get_log_cursor(1, "/uri") is None
    await repo.set_log_cursor(1, "/uri", now)
    got = await repo.get_log_cursor(1, "/uri")
    assert got is not None
    # Monotonic: an older timestamp must not move the cursor back.
    await repo.set_log_cursor(1, "/uri", now - timedelta(hours=1))
    assert await repo.get_log_cursor(1, "/uri") >= got.replace(microsecond=got.microsecond)
    await repo.close()


async def test_bulk_delete(tmp_path):
    repo = await _repo(tmp_path)
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [
            _mk(1, "old", "Critical", now, "/e/old"),
            _mk(2, "new", "Warning", now, "/e/new"),
        ]
    )
    # delete_alerts_before keys off received_at (both ~now) -> nothing purged yet
    assert await repo.delete_alerts_before(now - timedelta(days=1)) == 0
    assert await repo.delete_alerts_by_target(1) == 1
    assert await repo.count_alerts() == 1
    await repo.close()
