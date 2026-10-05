# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional coverage for TargetRepository data methods.

Focuses on the alert CRUD / CPER lifecycle, log-cursor upsert, batch
IntegrityError fallbacks, collected-log helpers, placement helpers, credential
decryption, and the metadata migration — all exercised against throwaway SQLite
(targets + alerts) via the shared ``repo`` fixture.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from src.database import repository as repo_mod
from src.database.models import Alert, LogCursor
from src.database.repository import (
    CredentialEncryption,
    _compute_alert_dedup_key,
    _cper_eligible,
    _to_naive_utc,
)
from src.location.models import Placement
from src.redfish.alert_subscriber import AlertEvent

# ---- small pure-function helpers -------------------------------------------


def test_to_naive_utc_variants():
    assert _to_naive_utc(None) is None
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _to_naive_utc(aware).tzinfo is None
    naive = datetime(2026, 1, 1, 12, 0)
    assert _to_naive_utc(naive) == naive  # passed through unchanged


def test_cper_eligible_branches():
    assert _cper_eligible(None) is False
    assert _cper_eligible("notadict") is False
    assert _cper_eligible({"DiagnosticDataType": "CPER"}) is False  # no URI
    assert _cper_eligible({"AdditionalDataURI": "/u", "DiagnosticDataType": "CPER"}) is True
    assert _cper_eligible({"AdditionalDataURI": "/u", "Resolution": "Collect the CPER log"}) is True
    assert _cper_eligible({"AdditionalDataURI": "/u", "DiagnosticDataType": "SELFTEST"}) is False


def test_compute_dedup_key_fallback_paths():
    # event_timestamp present
    k1 = _compute_alert_dedup_key(1, "M.1", "boom", datetime(2026, 1, 1, tzinfo=UTC), None)
    # source_id fallback (no timestamp)
    k2 = _compute_alert_dedup_key(1, None, None, None, None, "/entry/9")
    # received_at last-resort fallback
    k3 = _compute_alert_dedup_key(1, "M.1", "boom", None, datetime(2026, 1, 2, tzinfo=UTC))
    # received_at missing entirely -> empty ts component
    k4 = _compute_alert_dedup_key(1, "M.1", "boom", None, None)
    assert len({k1, k2, k3, k4}) == 4  # all distinct, all hex
    assert all(len(k) == 64 for k in (k1, k2, k3, k4))


def test_credential_encryption_requires_key():
    with pytest.raises(ValueError):
        CredentialEncryption("")


# ---- AlertEvent helper ------------------------------------------------------


def _mk(target_id=1, message="m", severity="Critical", event_ts=None, source_id=None, raw=None):
    return AlertEvent(
        target_id=target_id,
        target_name=f"n{target_id}",
        target_bmc=f"10.0.0.{target_id}",
        severity=severity,
        message=message,
        message_id=message,
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=event_ts if event_ts is not None else datetime.now(UTC),
        received_at=datetime.now(UTC),
        source_id=source_id,
        raw=raw or {"Message": message},
    )


def _cper_raw(uri="/redfish/v1/Systems/UBB/LogServices/x/Entries/1/attachment"):
    return {"AdditionalDataURI": uri, "DiagnosticDataType": "CPER", "Message": "cper"}


# ---- update_target encryption / serialization branches ----------------------


async def test_update_target_encrypts_and_serializes(repo):
    t = await repo.create_target(name="n", host="synthhost-01", username="u", password="pw")
    updated = await repo.update_target(
        t.id,
        password="newpw",
        token="tok",
        tags={"role": "gpu"},
        metric_reports_override=["/r/1"],
    )
    assert repo.decrypt_password(updated) == "newpw"
    assert repo.decrypt_token(updated) == "tok"
    assert repo.get_target_tags(updated) == {"role": "gpu"}
    assert repo.get_target_metric_reports(updated) == ["/r/1"]
    # Clearing token via a falsy value hits the None branch.
    cleared = await repo.update_target(t.id, token="")
    assert repo.decrypt_token(cleared) is None


async def test_decrypt_password_failure_raises(repo):
    from cryptography.fernet import InvalidToken

    t = await repo.create_target(name="n", host="synthhost-02", username="u", password="pw")
    t.encrypted_password = "not-a-valid-fernet-token"
    with pytest.raises(InvalidToken):
        repo.decrypt_password(t)


async def test_get_target_tags_and_reports_default_empty(repo):
    t = await repo.create_target(name="n", host="synthhost-03", username="u", password="pw")
    assert repo.get_target_tags(t) == {}
    assert repo.get_target_metric_reports(t) is None


# ---- placement / inventory / poll-status helpers ----------------------------


async def test_placement_helpers(repo):
    t = await repo.create_target(name="n", host="synthhost-04", username="u", password="pw")

    # First placement writes (no existing source).
    placed = await repo.set_target_location(t.id, Placement(rack="R1", rack_u=5, source="redfish"))
    assert placed.loc_rack == "R1"

    # A lower-trust hostname source is skipped (manual/redfish > hostname).
    skipped = await repo.set_target_location(t.id, Placement(rack="R9", source="hostname"))
    assert skipped.loc_rack == "R1"  # unchanged

    # force bypasses the precedence guard.
    forced = await repo.set_target_location(
        t.id, Placement(rack="R9", source="hostname"), force=True
    )
    assert forced.loc_rack == "R9"

    # height + clear
    h = await repo.set_target_height(t.id, 2, source="redfish")
    assert h is not None
    cleared = await repo.clear_target_location(t.id)
    assert cleared.loc_rack is None
    # Clear is STICKY: loc_source becomes the highest-rank "pinned" sentinel, so
    # an automated hostname/redfish resolve can't silently re-place it...
    assert cleared.loc_source == "pinned"
    reresolve = await repo.set_target_location(t.id, Placement(rack="RX", source="redfish"))
    assert reresolve.loc_rack is None  # auto-resolve suppressed
    # ...but an explicit manual placement (force) still wins.
    manual = await repo.set_target_location(t.id, Placement(rack="RX", source="manual"), force=True)
    assert manual.loc_rack == "RX"

    # missing-target variants return None
    assert await repo.set_target_location(999999, Placement(source="manual")) is None
    assert await repo.set_target_height(999999, 1) is None
    assert await repo.clear_target_location(999999) is None


async def test_set_target_inventory(repo):
    t = await repo.create_target(name="n", host="synthhost-05", username="u", password="pw")
    now = datetime.now(UTC)
    out = await repo.set_target_inventory(t.id, '{"sku": "x"}', "redfish", "match", now)
    assert out.inventory_json == '{"sku": "x"}'
    assert out.inventory_source == "redfish"
    assert await repo.set_target_inventory(999999, "{}", "redfish", None, now) is None


async def test_update_poll_status_success_and_error(repo):
    t = await repo.create_target(name="n", host="synthhost-06", username="u", password="pw")
    await repo.update_poll_status(t.id, "error", "boom")
    await repo.update_poll_status(t.id, "error", "boom2")
    errored = await repo.get_target(t.id)
    assert errored.consecutive_failures == 2
    assert errored.last_error_message == "boom2"
    await repo.update_poll_status(t.id, "success")
    ok = await repo.get_target(t.id)
    assert ok.consecutive_failures == 0
    assert ok.last_error_message is None


# ---- collected-log extra branches ------------------------------------------


async def test_collected_log_misc(repo):
    assert await repo.update_collected_log(123456, status="completed") is None
    assert await repo.delete_collected_log(123456) is None
    assert await repo.count_collected_logs() == 0

    for i in range(3):
        await repo.create_collected_log(
            target_id=1,
            target_name="n1",
            target_host="10.0.0.1",
            filename=f"f{i}.gz",
            file_path=f"/d/f{i}.gz",
        )
    assert await repo.count_collected_logs() == 3
    # limit is honored (and capped internally); offset path exercised.
    page = await repo.get_all_collected_logs(limit=2, offset=1)
    assert len(page) == 2


async def test_delete_expired_logs(repo):
    log = await repo.create_collected_log(
        target_id=1,
        target_name="n1",
        target_host="10.0.0.1",
        filename="old.gz",
        file_path="/d/old.gz",
    )
    # Backdate collected_at so it falls outside the retention window.
    async with repo.session_factory() as s:
        from src.database.models import CollectedLog

        row = await s.get(CollectedLog, log.id)
        row.collected_at = datetime.now(UTC) - timedelta(days=40)
        await s.commit()

    removed = await repo.delete_expired_logs(max_age_days=30)
    assert {r.filename for r in removed} == {"old.gz"}
    assert await repo.count_collected_logs() == 0


# ---- alert CRUD / search / stats -------------------------------------------


async def test_alert_get_delete_and_stats(repo):
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [
            _mk(1, "crit", "Critical", now, "/e/1"),
            _mk(1, "warn", "Warning", now, "/e/2"),
            _mk(2, "ok", "OK", now, "/e/3"),
        ]
    )
    rows = await repo.get_alerts()
    assert len(rows) == 3
    one = await repo.get_alert(rows[0].id)
    assert one is not None
    assert await repo.get_alert(999999) is None

    stats = await repo.get_alert_stats()
    assert stats["total"] == 3
    assert stats["critical"] == 1
    assert stats["warning"] == 1
    assert stats["ok"] == 1
    assert stats["last_24h"] == 3

    grouped = await repo.count_alerts_grouped_by_severity()
    assert grouped.get("Critical") == 1
    assert grouped.get("Warning") == 1

    # search + severity_not_in filter branches
    assert await repo.count_alerts(severity_not_in=["OK"]) == 2
    searched = await repo.get_alerts(search="warn")
    assert [r.message for r in searched] == ["warn"]


async def test_ping_alert_store(repo):
    assert await repo.ping_alert_store() is True


async def test_delete_alerts_before_chunks(repo):
    old = datetime.now(UTC) - timedelta(days=60)
    await repo.create_alerts_batch([_mk(1, "old", "Critical", old, "/e/old")])
    # Backdate received_at (retention keys off received time).
    async with repo.alert_session_factory() as s:
        row = await s.scalar(select(Alert))
        row.received_at = old.replace(tzinfo=None)
        await s.commit()
    purged = await repo.delete_alerts_before(datetime.now(UTC) - timedelta(days=1))
    assert purged == 1
    assert await repo.count_alerts() == 0


# ---- CPER lifecycle ---------------------------------------------------------


async def test_cper_pending_and_result_batch(repo):
    now = datetime.now(UTC)
    await repo.create_alerts_batch(
        [
            _mk(1, "cper1", "Critical", now, "/e/c1", raw=_cper_raw("/a/1")),
            _mk(1, "cper2", "Critical", now - timedelta(minutes=1), "/e/c2", raw=_cper_raw("/a/2")),
        ]
    )
    pending = await repo.get_pending_cper_alerts(limit=10)
    assert len(pending) == 2
    assert all(p.uri for p in pending)
    assert all(not p.has_decoded for p in pending)

    counts = await repo.count_cper_by_status()
    assert counts.get("pending") == 2

    # Batch write: one decoded, one increment-only failure.
    first, second = pending[0], pending[1]
    written = await repo.set_cper_results_batch(
        [
            {
                "id": first.id,
                "status": "decoded",
                "refined_message": "fixed",
                "decoded": {"section": "ok"},
            },
            {"id": second.id, "status": "fetch_failed", "increment_attempt": True},
        ]
    )
    assert written == 2
    assert await repo.set_cper_results_batch([]) == 0

    assert await repo.get_alert_cper(first.id) == {"section": "ok"}
    assert await repo.get_alert_cper(999999) is None


async def test_cper_single_result_finalize_and_requeue(repo):
    now = datetime.now(UTC)
    await repo.create_alerts_batch([_mk(1, "cper", "Critical", now, "/e/c", raw=_cper_raw())])
    pending = await repo.get_pending_cper_alerts()
    aid = pending[0].id

    await repo.set_cper_result(aid, status="fetch_failed", increment_attempt=True)
    await repo.set_cper_result(999999, status="decoded")  # no-op for missing id

    # Requeue stale fetch_failed rows back to pending (both unbounded + bounded).
    assert await repo.requeue_failed_cper(older_than_minutes=0) == 1
    # Now pending again; drive attempts up to the max then finalize.
    await repo.set_cper_result(aid, status="pending", increment_attempt=True)
    async with repo.alert_session_factory() as s:
        row = await s.get(Alert, aid)
        row.cper_attempts = 5
        await s.commit()
    finalized = await repo.finalize_exhausted_cper(max_attempts=3)
    assert finalized == 1

    # Bounded requeue variant (limit>0).
    assert await repo.requeue_failed_cper(older_than_minutes=0, limit=10) == 1


async def test_get_pending_cper_cooldown_excludes_recent(repo):
    now = datetime.now(UTC)
    await repo.create_alerts_batch([_mk(1, "cper", "Critical", now, "/e/c", raw=_cper_raw())])
    pending = await repo.get_pending_cper_alerts()
    # Stamp cper_attempted_at = now (stays 'pending').
    await repo.set_cper_result(pending[0].id, status="pending", increment_attempt=False)
    # Just attempted -> excluded by a positive cooldown window.
    cooled = await repo.get_pending_cper_alerts(attempt_cooldown_seconds=3600)
    assert cooled == []
    # No cooldown -> still selectable.
    assert len(await repo.get_pending_cper_alerts(attempt_cooldown_seconds=0)) == 1


async def test_mark_eligible_cper_pending(repo):
    # Insert an eligible alert with NULL cper_status directly (bypassing the
    # auto-pending path in create_alerts_batch) so the backfill has work to do.
    naive = datetime.now(UTC).replace(tzinfo=None)
    async with repo.alert_session_factory() as s:
        s.add(
            Alert(
                target_id=1,
                target_name="n1",
                target_bmc="10.0.0.1",
                severity="Critical",
                message="legacy",
                message_id="L.1",
                event_type="Alert",
                received_at=naive,
                occurred_at=naive,
                dedup_key="legacy-key-1",
                raw_data=_cper_raw(),
                cper_status=None,
            )
        )
        # A non-eligible NULL row is left alone.
        s.add(
            Alert(
                target_id=1,
                target_name="n1",
                target_bmc="10.0.0.1",
                severity="Warning",
                message="plain",
                message_id="P.1",
                event_type="Alert",
                received_at=naive,
                occurred_at=naive,
                dedup_key="legacy-key-2",
                raw_data={"Message": "plain"},
                cper_status=None,
            )
        )
        await s.commit()

    marked = await repo.mark_eligible_cper_pending()
    assert marked == 1
    counts = await repo.count_cper_by_status()
    assert counts.get("pending") == 1


# ---- batch IntegrityError fallback (simulated race) -------------------------


async def test_create_alerts_batch_integrity_fallback(repo, monkeypatch):
    now = datetime.now(UTC)
    ev = _mk(1, "dup", "Critical", now, "/e/dup")
    assert await repo.create_alerts_batch([ev]) == 1  # seed row with real key

    # Simulate a concurrent insert: force the pre-insert existence check to see
    # nothing, so the already-present dedup_key slips through to the INSERT and
    # collides on the unique index -> exercises the row-by-row fallback.
    real_select = repo_mod.select

    def blind_select(*args, **kwargs):
        return real_select(Alert.dedup_key).where(Alert.id < 0)

    monkeypatch.setattr(repo_mod, "select", blind_select)
    # Same event -> same real dedup_key -> unique collision on commit.
    inserted = await repo.create_alerts_batch([_mk(1, "dup", "Critical", now, "/e/dup")])
    monkeypatch.undo()
    assert inserted == 0  # collision skipped, batch not lost
    assert await repo.count_alerts() == 1


# ---- log-cursor upsert conflict path ---------------------------------------


async def test_set_log_cursor_update_existing(repo):
    now = datetime.now(UTC)
    await repo.set_log_cursor(1, "/uri", now)
    # Newer timestamp advances the high-water mark (update-existing branch).
    await repo.set_log_cursor(1, "/uri", now + timedelta(hours=1))
    got = await repo.get_log_cursor(1, "/uri")
    assert got is not None


async def test_set_log_cursor_integrity_conflict(repo, monkeypatch):
    now = datetime.now(UTC)
    await repo.set_log_cursor(2, "/uri2", now)  # seed existing cursor

    real_select = repo_mod.select

    def blind_select(*args, **kwargs):
        # Make the initial existence lookup (and the fallback re-read) miss,
        # so the method takes the INSERT path and hits the unique constraint.
        return real_select(LogCursor).where(LogCursor.id < 0)

    monkeypatch.setattr(repo_mod, "select", blind_select)
    # Should not raise: IntegrityError is caught and folded.
    await repo.set_log_cursor(2, "/uri2", now + timedelta(hours=2))
    monkeypatch.undo()
    # Cursor row still present and single.
    async with repo.alert_session_factory() as s:
        rows = (await s.execute(select(LogCursor))).scalars().all()
    assert len(rows) == 1


# ---- metadata migration (add missing column + index) ------------------------


async def test_migrate_metadata_adds_missing_column(repo):
    """Drop a column from the live alerts table, then re-run migration."""
    import sqlalchemy as sa

    # SQLite can't DROP COLUMN on old versions; recreate a stripped table and
    # let _migrate_metadata add back the missing column + indexes.
    async with repo.alert_engine.begin() as conn:
        await conn.execute(sa.text("DROP TABLE log_cursors"))
        # Recreate without the nullable ``last_created`` column and without the
        # unique index, so the migration has both a column and an index to add.
        # (updated_at is kept — its func.now() server_default can't be expressed
        # as an ADD COLUMN default on SQLite, which is not what we're testing.)
        await conn.execute(
            sa.text(
                "CREATE TABLE log_cursors ("
                "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, "
                "entries_uri VARCHAR(512) NOT NULL, updated_at DATETIME)"
            )
        )
        await conn.run_sync(lambda c: repo._migrate_metadata(c, LogCursor.metadata))

    # The restored column/index are usable again.
    await repo.set_log_cursor(5, "/u", datetime.now(UTC))
    assert await repo.get_log_cursor(5, "/u") is not None
