# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""SQLAlchemy models for target system configuration."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSON that becomes JSONB on PostgreSQL (binary, compact, indexable) and plain
# JSON/TEXT on SQLite.
_JSONB = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    """Base class for SQLite-backed models (targets, collected logs)."""

    pass


class AlertBase(DeclarativeBase):
    """Base for alert-store models.

    Kept separate from ``Base`` so alert tables are created only on the alert
    engine (PostgreSQL when ALERTS_DATABASE_URL is set), while targets/logs stay
    on SQLite. Alerts intentionally have no FK to targets (denormalized
    ``target_id``), so they can live in a different database.
    """

    pass


class Target(Base):
    """Target system configuration for Redfish polling."""

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Connection details
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)  # FQDN or IP
    port: Mapped[int] = mapped_column(Integer, default=443)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_ssl: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # Skip cert verification by default

    # Redfish action endpoint for diagnostic data collection
    telemetry_endpoint: Mapped[str] = mapped_column(
        String(512),
        default="/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData",
    )

    # Authentication - credentials are encrypted
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional token-based auth (encrypted)
    encrypted_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Connection mode: "direct" (default) or "sse". Redfish over HTTP(S) is the
    # transport; "sse" additionally subscribes to the EventService SSE stream.
    # server_default ensures ALTER TABLE migration works on existing SQLite rows.
    connection_mode: Mapped[str] = mapped_column(
        String(20), default="direct", server_default="direct"
    )

    # SSE settings (used when connection_mode == "sse")
    sse_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Alert subscription settings (SSE-based alerts)
    enable_alert_subscription: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    alert_sse_endpoint: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Override default /redfish/v1/EventService/SSE

    # Polling configuration
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Tags to add to all metrics from this target
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string

    # Per-target metric report URI overrides (JSON list of {"uri": ..., "report_type": ...}).
    # When set, this is an explicit operator pin that always wins over discovery.
    metric_reports_override: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Metric-report selection mode: "auto" (enumerate the target's
    # TelemetryService/MetricReports) or "manual" (use the override / global
    # default only). Auto-discovered reports are cached in discovered_reports.
    metric_discovery_mode: Mapped[str] = mapped_column(
        String(16), default="auto", server_default="auto"
    )
    # JSON list of {"uri": ..., "report_type": ...} discovered from the target's
    # TelemetryService; refreshed by the inventory enricher pass.
    discovered_reports: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Physical placement in the data hall. Resolved automatically from the host
    # naming convention or the Redfish Chassis Location at registration time, and
    # overridable from the Data Hall view. All nullable — a system with no known
    # placement simply renders in the "Unplaced" tray.
    loc_site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    loc_hall: Mapped[str | None] = mapped_column(String(64), nullable=True)
    loc_row: Mapped[str | None] = mapped_column(String(64), nullable=True)
    loc_rack: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Bottom-most rack unit the system occupies (Redfish Placement.RackOffset).
    loc_rack_u: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Height of the system in rack units (not in Redfish Placement; defaults to 1).
    loc_rack_u_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Rack-unit standard: "EIA_310" (1.75in U) or "OpenU" (OCP 48mm).
    loc_unit_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Provenance of the placement: "hostname", "redfish", or "manual". Drives the
    # overwrite precedence (manual > redfish > hostname) on re-resolution.
    loc_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Basic inventory read once from the BMC via standard Redfish GETs
    # (Chassis/Systems/Managers). JSON blob surfaced in the Data Hall tooltip.
    inventory_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Timestamp of the last successful inventory pull (drives the one-time /
    # staleness logic in the background enricher).
    inventory_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inventory_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Agreement between name-derived and BMC-derived location: "match" | "mismatch".
    location_check: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Status tracking
    last_poll_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_poll_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Target(id={self.id}, name='{self.name}', host='{self.host}')>"

    @property
    def base_url(self) -> str:
        """Get the base URL for this target."""
        protocol = "https" if self.use_ssl else "http"
        if (self.use_ssl and self.port == 443) or (not self.use_ssl and self.port == 80):
            return f"{protocol}://{self.host}"
        return f"{protocol}://{self.host}:{self.port}"


class CollectedLog(Base):
    """Record of a collected diagnostic log bundle."""

    __tablename__ = "collected_logs"
    # collected_at drives both the newest-first listing (ORDER BY) and the
    # retention sweep (WHERE collected_at < cutoff); without this index both
    # do a full-table scan as history grows.
    __table_args__ = (Index("ix_collected_logs_collected_at", "collected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Target reference (denormalized for history after target deletion)
    target_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_host: Mapped[str] = mapped_column(String(255), nullable=False)

    # File info
    filename: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status tracking
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # What triggered this collection: "manual" (operator), "policy" (auto on a
    # fatal/critical event), or "bulk". For "policy", trigger_message_id records
    # the Redfish MessageId that fired it. Also drives the policy rearm window.
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    trigger_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<CollectedLog(id={self.id}, target='{self.target_name}', status='{self.status}')>"


class HeatmapSnapshot(Base):
    """Latest heatmap value per host for one UI metric, published by the collector.

    The Data Hall heatmap reads this (fast, local SQLite) instead of calling the
    collector's in-process cache over HTTP — whose latency spikes when the
    collector event loop is busy flushing to InfluxDB. The collector refreshes
    these rows on a short interval from its in-memory HEATMAP cache.
    """

    __tablename__ = "heatmap_snapshots"

    # Composite key (collector_id, metric): with sharding, each collector owns and
    # replaces its own row for a metric; the API merges fresh rows across shards.
    collector_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    # UI metric key: "gpu_temp" | "board_temp" | "power".
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    # JSON object {host: value}.
    data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<HeatmapSnapshot(collector_id='{self.collector_id}', "
            f"metric='{self.metric}', updated_at='{self.updated_at}')>"
        )


class ShardLease(Base):
    """One row per target: which collector currently owns (polls) it.

    Collectors claim targets up to a per-shard cap and renew ``updated_at`` as a
    heartbeat. A lease whose heartbeat goes stale (owner died) is reclaimable by
    any collector, which is how work rebalances on crash / scale-down. Deboarded
    targets have their lease swept. Only used when sharding is enabled; a single
    collector owns everything without touching this table.
    """

    __tablename__ = "shard_leases"

    target_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class CollectorStats(Base):
    """Per-collector health/stats snapshot, published to shared SQLite.

    With sharding the API can't HTTP-poll one collector for fleet stats, so each
    collector writes its own row here (owned-target count, subscription counts,
    health) and the API aggregates across live rows (stale rows = dead shards).
    """

    __tablename__ = "collector_stats"

    collector_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # JSON blob of the collector's health/alert/subscription stats.
    data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    owned_targets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<CollectorStats(collector_id='{self.collector_id}', "
            f"owned_targets={self.owned_targets}, updated_at='{self.updated_at}')>"
        )


class CollectorSlot(Base):
    """Stable ordinal claimed by one collector process (dynamic-sharding only).

    Rendezvous hashing keys on the collector id, so that id must be STABLE across
    restarts or every redeploy reshuffles the whole fleet. Under docker-compose
    ``--scale`` the container hostname is unique but not stable, so each process
    claims the lowest free ordinal here (heartbeated on a TTL, reclaimed when
    stale) and derives ``COLLECTOR_ID = collector-<ordinal>``. The ordinal SET is
    stable across redeploys even as the process-to-ordinal mapping changes, so the
    HRW partition — and thus target ownership — stays put. An explicitly-set
    COLLECTOR_ID (e.g. a k8s StatefulSet pod name) bypasses this table entirely.
    """

    __tablename__ = "collector_slots"

    # Ordinal 0..N-1 -> COLLECTOR_ID "collector-<ordinal>".
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Per-process instance token (the container hostname) identifying the holder.
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<CollectorSlot(ordinal={self.ordinal}, token='{self.token}')>"


class Alert(AlertBase):
    """Redfish alert event from a target system (Critical/Warning only)."""

    __tablename__ = "alerts"

    __table_args__ = (
        # Primary query pattern: window + order by original occurrence time.
        Index("ix_alerts_occurred_at", "occurred_at"),
        Index("ix_alerts_severity_occurred", "severity", "occurred_at"),
        Index("ix_alerts_target_severity_time", "target_id", "severity", "received_at"),
        # Serves per-target alert lists ordered by occurrence time.
        Index("ix_alerts_target_occurred", "target_id", "occurred_at"),
        # Retention deletes range-scan received_at; without this it is a full scan.
        Index("ix_alerts_received_at", "received_at"),
        # CPER enrichment hot paths. Partial indexes keep these tiny (only the
        # transient pending/failed slice) and pre-sorted, so the every-cycle
        # worker queries are index-served regardless of overall table size.
        # (postgresql_where applies on PostgreSQL; SQLite builds a full index.)
        Index(
            "ix_alerts_cper_pending",
            "occurred_at",
            postgresql_where=text("cper_status = 'pending'"),
        ),
        Index(
            "ix_alerts_cper_failed",
            "cper_attempted_at",
            postgresql_where=text("cper_status = 'fetch_failed'"),
        ),
        # Unique index backs dedup on ingest (baseline pull + re-pull + SSE
        # reconnects can resurface the same event). NULL is treated as distinct,
        # so pre-existing rows (which stay NULL) never conflict.
        Index("ix_alerts_dedup_key", "dedup_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Target reference (denormalized for history after target deletion)
    target_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_bmc: Mapped[str] = mapped_column(String(255), nullable=False)

    # Alert details from Redfish Event
    severity: Mapped[str] = mapped_column(
        String(50), index=True, nullable=False
    )  # Critical, Warning, OK
    message: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # e.g., "Thermal.1.0.OverTemperature"
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # Alert, ResourceAdded, StatusChange, etc.
    origin_of_condition: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Resource URI that triggered the alert

    # Timestamps
    event_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # From EventTimestamp / LogEntry.Created
    received_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # Original occurrence time = event_timestamp when known, else received_at.
    # Always set on write; materialized (not a coalesce expression) so it can be
    # indexed and drives ordering + window filtering efficiently at scale.
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Idempotency key (sha256 hex) computed on ingest from target + message +
    # timestamp. Nullable so the auto-migration can add it to existing rows.
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Full original Redfish event / log entry (JSONB on PostgreSQL, JSON/TEXT on
    # SQLite), so no source field is lost (MessageArgs, SensorType, Oem, etc.).
    raw_data: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)

    # CPER (Common Platform Error Record) enrichment. When the source LogEntry
    # references a CPER attachment (DiagnosticDataType/Resolution mentions CPER +
    # AdditionalDataURI), a background worker fetches and decodes it with libcper.
    # cper_status: NULL = not applicable; else pending|decoded|no_data|
    # fetch_failed|decode_failed|unavailable.
    cper_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Human-readable summary distilled from the decoded CPER (shown in the UI).
    refined_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full decoded CPER (libcper JSON); deferred/lazy-loaded like raw_data.
    cper_decoded: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    # Bounded retry counter for the enrichment worker. server_default keeps the
    # ADD COLUMN migration valid on the already-populated alerts table.
    cper_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Wall-clock of the last decode attempt (naive UTC). Drives restart-resilient
    # retry: a fetch_failed row is requeued when this is older than the retry
    # window, independent of any in-memory timer that would reset on restart.
    cper_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Alert(id={self.id}, target='{self.target_name}', "
            f"severity='{self.severity}', message='{self.message[:50]}...')>"
        )


class LogCursor(AlertBase):
    """High-water mark per (target, log-entries collection) for incremental pulls.

    The baseline pull records the newest ``Created`` timestamp it has seen for
    each log collection so subsequent re-pulls skip already-processed entries
    instead of re-fetching and de-duplicating the whole tail every cycle.
    """

    __tablename__ = "log_cursors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entries_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    last_created: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_log_cursors_target_uri", "target_id", "entries_uri", unique=True),)
