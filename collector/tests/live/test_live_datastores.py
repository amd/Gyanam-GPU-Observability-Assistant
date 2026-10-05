# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Live datastore round-trips against the REAL PostgreSQL + InfluxDB services.

Unit tests use SQLite for the alert store and never touch InfluxDB, so they
can't catch dialect drift (PostgreSQL ``ON CONFLICT``, unique indexes, JSONB)
or a broken InfluxDB write path. These tests exercise the real services.

They WRITE to the real stores (synthetic rows/points that are cleaned up or
harmless under retention), so they're gated behind GYANAM_LIVE_MUTATE=1 and only
run on a throwaway/CI stack — never a live fleet by default.

Env (set by scripts/smoke-test.sh from .env):
  GYANAM_ALERTS_DB_URL   postgresql+asyncpg://…@postgres:5432/…
  GYANAM_ENCRYPTION_KEY  a Fernet key (any valid one)
  GYANAM_INFLUXDB_URL / _TOKEN / _ORG / _BUCKET
"""

import os
from datetime import UTC, datetime

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GYANAM_LIVE_MUTATE") != "1",
        reason="datastore round-trips write to the real stores; set GYANAM_LIVE_MUTATE=1",
    ),
]

# A synthetic target id far outside any real fleet, so rows are easy to isolate
# and clean up without touching production alerts.
_SYNTH_TARGET = 2_000_000_001


def _alert(message_id: str, message: str):
    from src.redfish.alert_subscriber import AlertEvent

    return AlertEvent(
        target_id=_SYNTH_TARGET,
        target_name="smoke-synth",
        target_bmc="10.255.255.254",
        severity="Warning",
        message=message,
        message_id=message_id,
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )


async def test_postgres_alert_dedup_dialect(tmp_path):
    """create_alerts_batch's unique-index / insert path against real PostgreSQL."""
    db_url = os.environ.get("GYANAM_ALERTS_DB_URL")
    key = os.environ.get("GYANAM_ENCRYPTION_KEY")
    if not db_url or not key:
        pytest.skip("GYANAM_ALERTS_DB_URL / GYANAM_ENCRYPTION_KEY not provided")

    from src.database.repository import TargetRepository

    repo = TargetRepository(
        database_url=f"sqlite:///{tmp_path}/targets.db",
        encryption_key=key,
        alerts_database_url=db_url,
    )
    await repo.init_db()
    try:
        # Clean slate for the synthetic target.
        await repo.delete_alerts_by_target(_SYNTH_TARGET)

        a, b = _alert("SMOKE.A", "first"), _alert("SMOKE.B", "second")
        # Two distinct alerts -> both inserted.
        assert await repo.create_alerts_batch([a, b]) == 2
        # Re-submitting an identical alert -> deduped by the unique index
        # (the real PostgreSQL conflict path), nothing new inserted.
        assert await repo.create_alerts_batch([a]) == 0
        assert await repo.count_alerts(target_id=_SYNTH_TARGET) == 2
    finally:
        # Always clean up the synthetic rows.
        await repo.delete_alerts_by_target(_SYNTH_TARGET)
        await repo.close()


async def test_influxdb_write_read_roundtrip():
    """Our exporter's real write path + a Flux read-back against real InfluxDB."""
    url = os.environ.get("GYANAM_INFLUXDB_URL")
    token = os.environ.get("GYANAM_INFLUXDB_TOKEN")
    org = os.environ.get("GYANAM_INFLUXDB_ORG")
    bucket = os.environ.get("GYANAM_INFLUXDB_BUCKET")
    if not (url and token and org and bucket):
        pytest.skip("InfluxDB env (GYANAM_INFLUXDB_URL/_TOKEN/_ORG/_BUCKET) not provided")

    from src.exporters.base import Metric
    from src.exporters.influxdb import InfluxDBExporter

    exporter = InfluxDBExporter(url=url, token=token, org=org, bucket=bucket)
    await exporter.connect()
    try:
        marker = "gyanam_smoke_marker"
        point = Metric(
            name=marker,
            value=42.0,
            timestamp=datetime.now(UTC),
            tags={"source": "smoke"},
        )
        assert await exporter.write_immediate([point]) is True

        # Read it back via Flux to prove the full write path landed.
        flux = (
            f'from(bucket: "{bucket}") |> range(start: -5m) '
            f'|> filter(fn: (r) => r._measurement == "{marker}") '
            f"|> last()"
        )
        rows = await exporter.query(flux)
        assert any(r.get("_value") == 42.0 for r in rows), "written point not found in InfluxDB"
    finally:
        await exporter.close()
