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
"""Repository for CRUD operations on target configurations."""

import asyncio
import functools
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import case, delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import defer

from ..location import PINNED_SOURCE, Placement, should_overwrite
from ..util.timeutil import to_naive_utc as _to_naive_utc
from .models import (
    Alert,
    AlertBase,
    Base,
    CollectedLog,
    CollectorSlot,
    CollectorStats,
    HeatmapSnapshot,
    LogCursor,
    ShardLease,
    Target,
)


@dataclass
class PendingCper:
    """Lightweight descriptor of an alert awaiting CPER decode (no blob load)."""

    id: int
    target_id: int
    uri: str | None
    attempts: int
    has_decoded: bool


logger = logging.getLogger(__name__)


def _retry_on_locked(max_attempts: int = 5, base_delay: float = 0.1):
    """Decorator: retry an async repository write on a transient SQLite
    'database is locked' error, with a short exponential backoff.

    WAL + ``busy_timeout`` absorb most contention, but the API and collector
    processes share the targets DB, so a burst of operator writes (e.g. placing
    many rack slots) racing the poller's status writer can still momentarily
    lock. A few quick retries keep those operator-initiated writes from
    surfacing as a 500 instead of just completing a beat later.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except OperationalError as e:
                    if "database is locked" not in str(e).lower() or attempt >= max_attempts - 1:
                        raise
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "%s hit 'database is locked'; retrying in %.2fs (attempt %d/%d)",
                        func.__name__,
                        delay,
                        attempt + 1,
                        max_attempts,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1

        return wrapper

    return decorator


def _cper_eligible(raw: dict | None) -> bool:
    """Whether an alert's raw LogEntry references a decodable CPER attachment.

    Trigger: ``DiagnosticDataType == "CPER"`` OR ``"CPER"`` appears in
    ``Resolution`` (case-insensitive) — and an ``AdditionalDataURI`` is present
    to fetch the binary from.
    """
    if not isinstance(raw, dict):
        return False
    if not raw.get("AdditionalDataURI"):
        return False
    diag = str(raw.get("DiagnosticDataType") or "")
    resolution = str(raw.get("Resolution") or "")
    return "cper" in diag.lower() or "cper" in resolution.lower()


def _compute_alert_dedup_key(
    target_id: int,
    message_id: str | None,
    message: str | None,
    event_timestamp: datetime | None,
    received_at: datetime | None,
    source_id: str | None = None,
) -> str:
    """Stable idempotency key (sha256 hex) for an alert.

    The key MUST converge across ingestion paths: the same physical event seen
    live via SSE/webhook and later re-read from a LogService by the baseline
    puller has to hash identically, otherwise it is stored twice. So the key is
    derived from event *content* whenever a timestamp is available:

    * ``event_timestamp`` present -> ``target | message_id | ts | message``.
      Both the SSE Event and the baseline LogEntry carry the same registry
      MessageId/Message and occurrence time, so their keys match.
    * else ``source_id`` present -> ``target | "src" | source_id``. Baseline
      LogEntries without a ``Created`` timestamp fall back to their stable
      ``@odata.id`` so periodic re-pulls still dedup.
    * else -> ``target | message_id | received_at | message`` (last resort for
      timestamp-less live events; ``received_at`` keeps genuinely repeated live
      alerts distinct).
    """
    if event_timestamp is not None:
        ts_str = _to_naive_utc(event_timestamp).isoformat()
        basis = "\x1f".join([str(target_id), message_id or "", ts_str, message or ""])
    elif source_id:
        basis = "\x1f".join([str(target_id), "src", source_id])
    else:
        ts_str = _to_naive_utc(received_at).isoformat() if received_at else ""
        basis = "\x1f".join([str(target_id), message_id or "", ts_str, message or ""])
    return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


class CredentialEncryption:
    """Handles encryption/decryption of sensitive credentials."""

    def __init__(self, encryption_key: str):
        """Initialize with Fernet encryption key."""
        if not encryption_key:
            raise ValueError("Encryption key is required")
        self.fernet = Fernet(
            encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
        )

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string."""
        return self.fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt an encrypted string."""
        return self.fernet.decrypt(ciphertext.encode()).decode()


class TargetRepository:
    """Repository for managing target configurations in the database."""

    @staticmethod
    def _build_engine(url: str, *, pool_size: int = 20, max_overflow: int = 10):
        """Create a tuned async engine, applying WAL for SQLite.

        pool_size/max_overflow apply to non-SQLite (PostgreSQL) engines. Keep
        them modest for the alert store: both the api and collector open their
        own engine, so the combined ceiling must stay under PostgreSQL's
        max_connections (default 100).
        """
        is_sqlite = url.startswith("sqlite://")
        if is_sqlite:
            url = url.replace("sqlite://", "sqlite+aiosqlite://")
            engine = create_async_engine(
                url,
                echo=False,
                pool_size=20,  # Moderate pool for SQLite
                max_overflow=10,
                pool_pre_ping=True,
            )

            @event.listens_for(engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                # Wait up to 15s for a write lock instead of failing immediately
                # with "database is locked". Under concurrent writers (poller
                # status writer + on-demand log collection) the bare WAL config
                # still raised OperationalError on contention; busy_timeout makes
                # writers queue instead of erroring.
                cursor.execute("PRAGMA busy_timeout=15000")
                # NORMAL is the recommended durability for WAL — safe against
                # application crashes, and avoids an fsync per transaction.
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
        else:
            # PostgreSQL/MySQL
            engine = create_async_engine(
                url,
                echo=False,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_recycle=3600,
                pool_pre_ping=True,
            )
        return engine

    def __init__(
        self,
        database_url: str,
        encryption_key: str,
        alerts_database_url: str,
    ):
        """Initialize the repository.

        Args:
            database_url: SQLAlchemy async URL for targets/collected_logs (SQLite)
            encryption_key: Fernet key for credential encryption
            alerts_database_url: SQLAlchemy async URL for the alert store
                (PostgreSQL). Required — alerts are stored separately from the
                target store.
        """
        if not alerts_database_url:
            raise ValueError(
                "alerts_database_url is required (set ALERTS_DATABASE_URL to the "
                "PostgreSQL alert store)"
            )

        # Main store (targets, collected_logs) and dedicated alert store.
        self.engine = self._build_engine(database_url)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        # Alert store carries the batch writer, the CPER enrichment worker, and
        # occasional API reads. Sized for a ~400-target fleet (incl. correlated
        # bursts) while api + collector combined stay well under PG's default
        # max_connections (100).
        self.alert_engine = self._build_engine(alerts_database_url, pool_size=20, max_overflow=10)
        self.alert_session_factory = async_sessionmaker(
            self.alert_engine, class_=AsyncSession, expire_on_commit=False
        )
        self.encryption = CredentialEncryption(encryption_key)

    async def init_db(self) -> None:
        """Initialize schema on both stores and migrate missing columns/indexes."""
        # Main store (targets, collected_logs) — fatal if it fails: the app can't
        # run without target configuration.
        async with self.engine.begin() as conn:
            # heatmap_snapshots gained a composite (collector_id, metric) key for
            # sharding; the column-only auto-migrator can't change a PK, so drop the
            # stale table (it's an ephemeral cache, rebuilt within seconds) and let
            # create_all rebuild it with the new schema.
            await conn.run_sync(self._drop_stale_heatmap_snapshots)
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(lambda c: self._migrate_metadata(c, Base.metadata))

        # Alert store (alerts, log_cursors) — non-fatal. If PostgreSQL is
        # unavailable at startup, keep the rest of the app (targets, metrics,
        # health) working; the alerts feature degrades until PG recovers and the
        # service is restarted.
        try:
            async with self.alert_engine.begin() as conn:
                # api and collector both run init concurrently against the same
                # PostgreSQL. Serialize schema creation with a transaction-scoped
                # advisory lock (auto-released at commit) to avoid DDL races.
                if conn.dialect.name == "postgresql":
                    await conn.execute(text("SELECT pg_advisory_xact_lock(4771001)"))
                await conn.run_sync(AlertBase.metadata.create_all)
                await conn.run_sync(lambda c: self._migrate_metadata(c, AlertBase.metadata))
                # Drop indexes superseded by newer definitions so the planner
                # prefers the partial CPER indexes (idempotent; no-op if absent).
                for obsolete in ("ix_alerts_cper_status",):
                    await conn.execute(text(f"DROP INDEX IF EXISTS {obsolete}"))
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Alert store schema init failed (%s: %s). The alerts feature will "
                "be unavailable until the alert database is reachable and the "
                "service is restarted; targets/metrics remain functional.",
                type(e).__name__,
                e,
            )

    @staticmethod
    def _drop_stale_heatmap_snapshots(conn) -> None:
        """Drop heatmap_snapshots if it predates the (collector_id, metric) key."""
        import sqlalchemy as sa

        inspector = sa.inspect(conn)
        if not inspector.has_table("heatmap_snapshots"):
            return
        cols = {c["name"] for c in inspector.get_columns("heatmap_snapshots")}
        if "collector_id" not in cols:
            conn.execute(sa.text("DROP TABLE heatmap_snapshots"))

    @staticmethod
    def _migrate_metadata(conn, metadata) -> None:
        """Add columns and indexes present in the given metadata but not the DB."""
        import sqlalchemy as sa

        inspector = sa.inspect(conn)

        # Migrate missing columns
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    # Build the column type as SQL
                    col_type = column.type.compile(conn.dialect)
                    nullable = "NULL" if column.nullable else "NOT NULL"
                    default = ""
                    if column.server_default is not None:
                        default_val = column.server_default.arg
                        # Quote string defaults for SQL (integers don't need quotes)
                        if isinstance(column.type, sa.String | sa.Text):
                            default = f" DEFAULT '{default_val}'"
                        else:
                            default = f" DEFAULT {default_val}"
                    # Safe from SQL injection: table.name and column.name come from
                    # SQLAlchemy models (code-defined), not user input. Using f-string
                    # for DDL is acceptable here.
                    sql = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type} {nullable}{default}"
                    conn.execute(sa.text(sql))

        # Migrate missing indexes
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue

            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}

            for index in table.indexes:
                if index.name not in existing_indexes:
                    # Create the index using SQLAlchemy DDL
                    index.create(conn)

    async def close(self) -> None:
        """Close the database connections."""
        await self.engine.dispose()
        await self.alert_engine.dispose()

    @_retry_on_locked()
    async def create_target(
        self,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        use_ssl: bool = True,
        verify_ssl: bool = False,
        telemetry_endpoint: str = "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData",
        token: str | None = None,
        enabled: bool = True,
        enable_alert_subscription: bool = True,
        poll_interval_override: int | None = None,
        tags: dict | None = None,
        metric_reports_override: list | None = None,
        metric_discovery_mode: str = "auto",
        connection_mode: str = "direct",
        sse_endpoint: str | None = None,
        alert_sse_endpoint: str | None = None,
    ) -> Target:
        """Create a new target configuration.

        Args:
            name: Display name for the target
            host: FQDN or IP address
            username: Authentication username
            password: Authentication password (will be encrypted)
            port: Port number (default 443)
            use_ssl: Whether to use HTTPS (default True)
            telemetry_endpoint: Redfish endpoint path
            token: Optional authentication token (will be encrypted)
            enabled: Whether polling is enabled
            poll_interval_override: Override default polling interval
            tags: Additional tags to add to metrics

        Returns:
            Created Target object
        """
        async with self.session_factory() as session:
            target = Target(
                name=name,
                host=host,
                port=port,
                use_ssl=use_ssl,
                verify_ssl=verify_ssl,
                telemetry_endpoint=telemetry_endpoint,
                username=username,
                encrypted_password=self.encryption.encrypt(password),
                encrypted_token=self.encryption.encrypt(token) if token else None,
                enabled=enabled,
                enable_alert_subscription=enable_alert_subscription,
                poll_interval_override=poll_interval_override,
                tags=json.dumps(tags) if tags else None,
                metric_reports_override=json.dumps(metric_reports_override)
                if metric_reports_override
                else None,
                metric_discovery_mode=metric_discovery_mode,
                connection_mode=connection_mode,
                sse_endpoint=sse_endpoint,
                alert_sse_endpoint=alert_sse_endpoint,
            )
            session.add(target)
            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    async def get_target(self, target_id: int) -> Target | None:
        """Get a target by ID."""
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    async def get_target_by_host(self, host: str) -> Target | None:
        """Get a target by host."""
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.host == host))
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    async def get_all_targets(self, enabled_only: bool = False) -> list[Target]:
        """Get all targets, optionally filtered by enabled status."""
        async with self.session_factory() as session:
            query = select(Target)
            if enabled_only:
                query = query.where(Target.enabled.is_(True))
            result = await session.execute(query.order_by(Target.name))
            return list(result.scalars().all())

    async def get_enabled_target_ids(self) -> list[int]:
        """Just the ids of enabled targets — the input to HRW allocation.

        Dynamic sharding recomputes the assignment every claim pass (~20s); it
        only needs ids, so this avoids loading (and decrypting) full Target rows.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target.id).where(Target.enabled.is_(True)))
            return list(result.scalars().all())

    # Fields that can be updated via update_target()
    _UPDATABLE_FIELDS = frozenset(
        {
            "name",
            "host",
            "port",
            "use_ssl",
            "verify_ssl",
            "telemetry_endpoint",
            "username",
            "enabled",
            "enable_alert_subscription",
            "poll_interval_override",
            "tags",
            "metric_reports_override",
            "metric_discovery_mode",
            "connection_mode",
            "sse_endpoint",
            "alert_sse_endpoint",
            "loc_site",
            "loc_hall",
            "loc_row",
            "loc_rack",
            "loc_rack_u",
            "loc_rack_u_height",
            "loc_unit_type",
            "loc_source",
        }
    )

    @_retry_on_locked()
    async def update_target(self, target_id: int, **kwargs) -> Target | None:
        """Update a target configuration.

        Args:
            target_id: Target ID to update
            **kwargs: Fields to update (must be in the allowed set)

        Returns:
            Updated Target object or None if not found
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()

            if not target:
                return None

            # Handle password encryption if provided
            if "password" in kwargs:
                kwargs["encrypted_password"] = self.encryption.encrypt(kwargs.pop("password"))

            # Handle token encryption if provided
            if "token" in kwargs:
                token = kwargs.pop("token")
                kwargs["encrypted_token"] = self.encryption.encrypt(token) if token else None

            # Handle tags serialization
            if "tags" in kwargs and isinstance(kwargs["tags"], dict):
                kwargs["tags"] = json.dumps(kwargs["tags"])

            # Handle metric_reports_override serialization
            if "metric_reports_override" in kwargs and isinstance(
                kwargs["metric_reports_override"], list
            ):
                kwargs["metric_reports_override"] = json.dumps(kwargs["metric_reports_override"])

            # Only allow updating known safe fields
            allowed = self._UPDATABLE_FIELDS | {
                "encrypted_password",
                "encrypted_token",
            }
            for key, value in kwargs.items():
                if key in allowed and hasattr(target, key):
                    setattr(target, key, value)

            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    @_retry_on_locked()
    async def set_target_location(
        self, target_id: int, placement: Placement, force: bool = False
    ) -> Target | None:
        """Persist a system's physical placement, honouring source precedence.

        The write is skipped (existing placement kept) when the incoming
        ``placement.source`` is lower-trust than what is already stored —
        unless ``force`` is set — so an operator's manual correction is never
        clobbered by an automated hostname/Redfish re-resolution.

        Args:
            target_id: Target to place.
            placement: The candidate placement (must carry a ``source``).
            force: Bypass the precedence guard (e.g. explicit manual override).

        Returns:
            The (possibly unchanged) Target, or None if not found.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()
            if not target:
                return None

            if not force and not should_overwrite(target.loc_source, placement.source):
                return target  # type: ignore[no-any-return]  # keep higher-trust placement

            for column, value in placement.to_columns().items():
                setattr(target, column, value)

            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    @_retry_on_locked()
    async def set_target_inventory(
        self,
        target_id: int,
        inventory_json: str,
        source: str,
        location_check: str | None,
        updated_at: datetime,
    ) -> Target | None:
        """Persist the basic inventory read from the BMC for a target."""
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()
            if not target:
                return None

            target.inventory_json = inventory_json
            target.inventory_source = source
            target.location_check = location_check
            target.inventory_updated_at = updated_at

            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    @_retry_on_locked()
    async def set_target_height(
        self, target_id: int, height_u: int, source: str = "redfish"
    ) -> Target | None:
        """Update only the rack-unit height, honouring placement precedence.

        Used when a BMC reports a chassis height but no rack placement — we want
        to record the height without clobbering a manual placement, so this goes
        through the same ``should_overwrite`` guard as ``set_target_location``.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()
            if not target:
                return None
            if not should_overwrite(target.loc_source, source):
                return target  # type: ignore[no-any-return]  # keep higher-trust height

            target.loc_rack_u_height = height_u
            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    @_retry_on_locked()
    async def clear_target_location(self, target_id: int) -> Target | None:
        """Remove a system's placement so it returns to the Unplaced tray.

        The coordinates are cleared but ``loc_source`` is set to the highest-rank
        ``pinned`` sentinel rather than null, so a subsequent automated
        hostname/Redfish resolve cannot silently re-place the system against the
        operator's intent. An explicit manual placement (force=True) still wins.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()
            if not target:
                return None

            for column in Placement().to_columns():
                setattr(target, column, None)
            target.loc_source = PINNED_SOURCE

            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

    @_retry_on_locked()
    async def delete_target(self, target_id: int) -> bool:
        """Delete a target configuration.

        Args:
            target_id: Target ID to delete

        Returns:
            True if deleted, False if not found
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Target).where(Target.id == target_id))
            target = result.scalar_one_or_none()

            if not target:
                return False

            await session.delete(target)
            await session.commit()
            return True

    async def update_poll_status(
        self, target_id: int, status: str, error_message: str | None = None
    ) -> None:
        """Update the polling status for a target.

        Uses atomic update for consecutive_failures to avoid race conditions.

        Args:
            target_id: Target ID to update
            status: Status string (e.g., 'success', 'error')
            error_message: Error message if status is 'error'
        """
        async with self.session_factory() as session:
            now = datetime.now(UTC)

            if status == "success":
                # Reset failures on success
                stmt = (
                    update(Target)
                    .where(Target.id == target_id)
                    .values(
                        last_poll_time=now,
                        last_poll_status=status,
                        consecutive_failures=0,
                        last_error_message=None,
                    )
                )
            else:
                # Atomically increment failures on error
                stmt = (
                    update(Target)
                    .where(Target.id == target_id)
                    .values(
                        last_poll_time=now,
                        last_poll_status=status,
                        consecutive_failures=Target.consecutive_failures + 1,
                        last_error_message=error_message,
                    )
                )

            await session.execute(stmt)
            await session.commit()

    async def update_poll_status_batch(self, updates: dict[int, tuple[str, str | None]]) -> None:
        """Apply many poll-status updates in a single transaction.

        Coalescing concurrent status writes is required at high target counts:
        per-poll commits hammer SQLite's WAL writer and produce 'database is
        locked' errors at ~100 concurrent commits.

        Args:
            updates: target_id -> (status, error_message)
        """
        if not updates:
            return

        now = datetime.now(UTC)
        # Partition by outcome so we can issue two bulk UPDATEs instead of
        # one per row.
        success_ids: list[int] = []
        error_rows: list[tuple[int, str | None]] = []
        for tid, (status, err) in updates.items():
            if status == "success":
                success_ids.append(tid)
            else:
                error_rows.append((tid, err))

        async with self.session_factory() as session:
            if success_ids:
                await session.execute(
                    update(Target)
                    .where(Target.id.in_(success_ids))
                    .values(
                        last_poll_time=now,
                        last_poll_status="success",
                        consecutive_failures=0,
                        last_error_message=None,
                    )
                )

            # Errors need per-row error_message values; SQLAlchemy's update
            # can't take per-row values in a single statement portably, so
            # issue them inside the same transaction (one commit).
            for tid, err in error_rows:
                await session.execute(
                    update(Target)
                    .where(Target.id == tid)
                    .values(
                        last_poll_time=now,
                        last_poll_status="error",
                        consecutive_failures=Target.consecutive_failures + 1,
                        last_error_message=err,
                    )
                )

            await session.commit()

    def decrypt_password(self, target: Target) -> str:
        """Decrypt the password for a target.

        A decryption failure almost always means ENCRYPTION_KEY no longer matches
        the key that encrypted the stored credentials (e.g. .env was regenerated
        or targets.db was copied from another deployment). Surface that clearly.
        """
        try:
            return self.encryption.decrypt(target.encrypted_password)
        except Exception as e:
            logger.error(
                "Failed to decrypt credentials for target '%s' (%s). This usually "
                "means ENCRYPTION_KEY does not match the key used to encrypt "
                "targets.db. Run './gyanam.sh doctor' to confirm.",
                getattr(target, "name", "?"),
                type(e).__name__,
            )
            raise

    def decrypt_token(self, target: Target) -> str | None:
        """Decrypt the token for a target, if present."""
        if target.encrypted_token:
            return self.encryption.decrypt(target.encrypted_token)
        return None

    def get_target_tags(self, target: Target) -> dict:
        """Get the tags dictionary for a target."""
        if target.tags:
            return json.loads(target.tags)  # type: ignore[no-any-return]
        return {}

    def get_target_metric_reports(self, target: Target) -> list[dict] | None:
        """Get per-target metric report URI overrides, or None for global defaults."""
        if target.metric_reports_override:
            return json.loads(target.metric_reports_override)  # type: ignore[no-any-return]
        return None

    def get_discovered_metric_reports(self, target: Target) -> list[dict] | None:
        """Auto-discovered metric reports for a target, or None.

        Only honoured when the target is in "auto" discovery mode; a "manual"
        target uses its override / the global default instead.
        """
        if getattr(target, "metric_discovery_mode", "auto") != "auto":
            return None
        if target.discovered_reports:
            return json.loads(target.discovered_reports)  # type: ignore[no-any-return]
        return None

    def resolve_metric_reports(self, target: Target, global_default: list | None) -> list | None:
        """Reports the poller should fetch.

        - An explicit per-target override always wins outright.
        - Otherwise, auto-discovered reports are *unioned* with the global
          defaults (deduped by URI, aggregate reports last). The union matters:
          the defaults are GET by direct URI and succeed even on BMCs that don't
          enumerate them in their MetricReports collection, so merging guarantees
          those known-good reports (e.g. GPU temp) are never lost when a BMC
          under-enumerates — while still consuming whatever extra reports it does
          expose.
        - With no override and nothing discovered, fall back to the defaults.
        """
        override = self.get_target_metric_reports(target)
        if override:
            return override
        discovered = self.get_discovered_metric_reports(target)
        if not discovered:
            return global_default
        return self._merge_reports(discovered, global_default or [])

    @staticmethod
    def _merge_reports(discovered: list, defaults: list) -> list:
        """Union two report lists by URI, keeping aggregate reports ('All') last."""

        def uri_of(r):
            return r.get("uri") if isinstance(r, dict) else getattr(r, "uri", None)

        def is_aggregate(r):
            rt = (
                (r.get("report_type") if isinstance(r, dict) else getattr(r, "report_type", ""))
                or ""
            ).casefold()
            return rt in ("all", "comprehensive") or (uri_of(r) or "").casefold().endswith("/all")

        seen: set[str] = set()
        merged = []
        for r in [*discovered, *defaults]:
            u = uri_of(r)
            if u and u not in seen:
                seen.add(u)
                merged.append(r)
        # Stable sort: specific reports first so they claim their MetricProperties
        # before the aggregate 'All' report during the poller's dedup pass.
        merged.sort(key=is_aggregate)
        return merged

    async def set_discovered_reports(self, target_id: int, reports: list[dict] | None) -> None:
        """Persist auto-discovered metric reports for a target (enricher-driven)."""
        async with self.session_factory() as session:
            target = await session.get(Target, target_id)
            if target is None:
                return
            target.discovered_reports = json.dumps(reports) if reports else None
            await session.commit()

    # ---- CollectedLog CRUD ----

    async def create_collected_log(
        self,
        target_id: int,
        target_name: str,
        target_host: str,
        filename: str,
        file_path: str,
        status: str = "pending",
        file_size_bytes: int | None = None,
        trigger: str = "manual",
        trigger_message_id: str | None = None,
    ) -> CollectedLog:
        """Create a new collected log record."""
        async with self.session_factory() as session:
            log = CollectedLog(
                target_id=target_id,
                target_name=target_name,
                target_host=target_host,
                filename=filename,
                file_path=file_path,
                status=status,
                file_size_bytes=file_size_bytes,
                trigger=trigger,
                trigger_message_id=trigger_message_id,
            )
            session.add(log)
            await session.commit()
            await session.refresh(log)
            return log

    # ---- Heatmap snapshot (collector publishes; API reads) ----

    async def upsert_heatmap_snapshot(
        self, metric: str, values: dict, collector_id: str = ""
    ) -> None:
        """Publish this collector's latest {host: value} map for a heatmap metric.

        Replaces only the calling collector's row for the metric; shards never
        clobber each other's hosts.
        """
        payload = json.dumps(values)
        async with self.session_factory() as session:
            row = await session.get(HeatmapSnapshot, (collector_id, metric))
            if row is None:
                session.add(
                    HeatmapSnapshot(
                        collector_id=collector_id,
                        metric=metric,
                        data=payload,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                row.data = payload
                row.updated_at = datetime.now(UTC)
            await session.commit()

    async def get_heatmap_snapshot(
        self, metric: str, max_age_seconds: float = 300.0
    ) -> dict | None:
        """Merged {host: value} for a metric across all fresh shards, or None.

        Reads every collector's row for the metric and merges the fresh ones
        (dropping rows from shards that stopped publishing). Returns None when no
        fresh data exists so the UI renders "no data" rather than a frozen map.
        """
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            result = await session.execute(
                select(HeatmapSnapshot).where(HeatmapSnapshot.metric == metric)
            )
            # Collect fresh rows with their timestamps, then merge OLDEST-first so
            # the newest-publishing shard wins any per-host overlap. After a
            # rebalance the old owner can briefly keep republishing a shed host's
            # last value; newest-wins biases the Data Hall toward the new owner's
            # live reading rather than an arbitrary row order.
            fresh_rows: list[tuple[datetime, dict]] = []
            for row in result.scalars():
                updated = row.updated_at
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                if updated is not None and (now - updated).total_seconds() > max_age_seconds:
                    continue  # stale shard — skip
                try:
                    data = json.loads(row.data)
                except (ValueError, TypeError):
                    continue
                fresh_rows.append((updated or now, data))
            if not fresh_rows:
                return None
            merged: dict = {}
            for _updated, data in sorted(fresh_rows, key=lambda r: r[0]):
                merged.update(data)
            return merged

    # ---- Shard leases (horizontal sharding) ----

    def set_shard_context(self, collector_id: str, lease_ttl_seconds: float) -> None:
        """Enable shard-aware target selection for this (collector) repository.

        Once set, ``get_active_targets`` returns only this collector's owned slice.
        The API never calls this, so its views keep seeing the whole fleet.
        """
        self._shard = (collector_id, lease_ttl_seconds)

    async def get_active_targets(self) -> list[Target]:
        """Targets this process should collect from.

        The owned shard slice when sharding is enabled; otherwise every enabled
        target (unchanged single-collector behavior).
        """
        shard = getattr(self, "_shard", None)
        if shard is not None:
            return await self.get_owned_targets(shard[0], shard[1])
        return await self.get_all_targets(enabled_only=True)

    @_retry_on_locked()
    async def claim_shard_targets(
        self, collector_id: str, max_targets: int, lease_ttl_seconds: float
    ) -> int:
        """Renew/reclaim/claim leases for this collector; return count now owned.

        One serialized pass: sweep leases for deboarded targets, heartbeat my own,
        then claim unowned-or-stale targets up to the cap. SQLite serializes writes
        and the ``ON CONFLICT ... WHERE stale`` guard makes claims race-safe across
        collectors (a target a peer just renewed can't be stolen).
        """
        now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC to match SQLite storage
        stale = (datetime.now(UTC) - timedelta(seconds=lease_ttl_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            # Sweep leases whose target is gone/disabled.
            await session.execute(
                text(
                    "DELETE FROM shard_leases WHERE target_id NOT IN "
                    "(SELECT id FROM targets WHERE enabled = 1)"
                )
            )
            # Heartbeat my leases.
            await session.execute(
                update(ShardLease)
                .where(ShardLease.collector_id == collector_id)
                .values(updated_at=now)
            )
            owned = (
                await session.execute(
                    select(func.count())
                    .select_from(ShardLease)
                    .where(ShardLease.collector_id == collector_id)
                )
            ).scalar_one()
            capacity = max_targets - owned
            if capacity > 0:
                claimable = (
                    (
                        await session.execute(
                            text(
                                "SELECT t.id FROM targets t "
                                "LEFT JOIN shard_leases l ON l.target_id = t.id "
                                "WHERE t.enabled = 1 AND (l.target_id IS NULL OR l.updated_at < :stale) "
                                "ORDER BY t.id LIMIT :cap"
                            ),
                            {"stale": stale, "cap": capacity},
                        )
                    )
                    .scalars()
                    .all()
                )
                for tid in claimable:
                    await session.execute(
                        text(
                            "INSERT INTO shard_leases(target_id, collector_id, updated_at) "
                            "VALUES(:tid, :cid, :now) "
                            "ON CONFLICT(target_id) DO UPDATE SET collector_id=:cid, updated_at=:now "
                            "WHERE shard_leases.updated_at < :stale"
                        ),
                        {"tid": tid, "cid": collector_id, "now": now, "stale": stale},
                    )
            await session.commit()
            owned = (
                await session.execute(
                    select(func.count())
                    .select_from(ShardLease)
                    .where(ShardLease.collector_id == collector_id)
                )
            ).scalar_one()
            return int(owned)

    @_retry_on_locked()
    async def reconcile_shard_leases(
        self, collector_id: str, mine_ids: list[int], cap: int, lease_ttl_seconds: float
    ) -> int:
        """Dynamic (HRW) path: make the lease table match this collector's slice.

        ``mine_ids`` is the target set assigned to this collector by the pure
        allocator (computed OUTSIDE this txn — the sha256 work must not run under
        SQLite's write lock). One serialized pass:
          1. Sweep leases for disabled/gone targets.
          2. **Lazy shed:** renew (claim/heartbeat) ONLY the leases in my slice.
             Leases I no longer want are simply NOT renewed — they expire after
             ``lease_ttl`` and a peer claims them once stale. I keep polling a
             shed target until my lease on it goes stale, so handoff is gap-free.
          3. **Steal-guard:** the claim upsert only takes a row that is unowned,
             already mine, or stale — never a peer's fresh lease.
          4. **Orphan safety-net:** after claiming my slice, if I'm below ``cap``,
             also claim any enabled target with NO fresh owner (unowned or stale).
             Unlike the static path, HRW can leave a target in nobody's slice —
             during asymmetric membership views, cold start, or when a peer's
             reconcile is failing while it still looks live. This bounded sweep
             guarantees such orphans are covered within one pass; adopted orphans
             outside my HRW slice are lazy-shed again once their rightful owner
             reclaims them. Inactive in steady state (no orphans to find).
        Returns the number of fresh leases now held (includes shed-but-not-yet-
        stale targets I'm still polling).
        """
        async with self.session_factory() as session:
            # (1) Sweep leases whose target is gone/disabled. Takes the write lock
            # up front, serializing reconcile passes across collectors. Read the
            # clock AFTER the lock is held so a long busy-wait can't backdate the
            # heartbeat (and over-widen the stale cutoff).
            await session.execute(
                text(
                    "DELETE FROM shard_leases WHERE target_id NOT IN "
                    "(SELECT id FROM targets WHERE enabled = 1)"
                )
            )
            now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC to match storage
            stale = now - timedelta(seconds=lease_ttl_seconds)
            claim_sql = text(
                "INSERT INTO shard_leases(target_id, collector_id, updated_at) "
                "VALUES(:tid, :cid, :now) "
                "ON CONFLICT(target_id) DO UPDATE SET collector_id=:cid, updated_at=:now "
                "WHERE shard_leases.collector_id = :cid OR shard_leases.updated_at < :stale"
            )
            # Guard against a target disabled between the id-read and now: only
            # claim ids that are still enabled. Chunk the IN() to stay well under
            # SQLite's bound-parameter limit for large slices.
            enabled_mine: list[int] = []
            unique_mine = sorted(set(mine_ids))
            for i in range(0, len(unique_mine), 500):
                chunk = unique_mine[i : i + 500]
                rows = (
                    (
                        await session.execute(
                            select(Target.id).where(Target.enabled.is_(True), Target.id.in_(chunk))
                        )
                    )
                    .scalars()
                    .all()
                )
                enabled_mine.extend(rows)
            # (2+3) Claim/renew my slice with the steal-guard. The ``collector_id=
            # :cid`` branch renews my own leases (the heartbeat); the stale branch
            # reclaims a dead peer's; a peer's FRESH lease is left untouched.
            for tid in enabled_mine:
                await session.execute(
                    claim_sql, {"tid": tid, "cid": collector_id, "now": now, "stale": stale}
                )
            # (4) Orphan safety-net sweep, bounded by remaining capacity.
            owned_now = (
                await session.execute(
                    select(func.count())
                    .select_from(ShardLease)
                    .where(ShardLease.collector_id == collector_id, ShardLease.updated_at >= stale)
                )
            ).scalar_one()
            capacity = cap - int(owned_now)
            if capacity > 0:
                orphans = (
                    (
                        await session.execute(
                            text(
                                "SELECT t.id FROM targets t "
                                "LEFT JOIN shard_leases l ON l.target_id = t.id "
                                "WHERE t.enabled = 1 AND (l.target_id IS NULL OR l.updated_at < :stale) "
                                "ORDER BY t.id LIMIT :cap"
                            ),
                            {"stale": stale, "cap": capacity},
                        )
                    )
                    .scalars()
                    .all()
                )
                for tid in orphans:
                    await session.execute(
                        claim_sql, {"tid": tid, "cid": collector_id, "now": now, "stale": stale}
                    )
            await session.commit()
            owned = (
                await session.execute(
                    select(func.count())
                    .select_from(ShardLease)
                    .where(ShardLease.collector_id == collector_id, ShardLease.updated_at >= stale)
                )
            ).scalar_one()
            return int(owned)

    @_retry_on_locked()
    async def claim_collector_slot(self, token: str, lease_ttl_seconds: float) -> int:
        """Claim or renew a stable ordinal for this process; return the ordinal.

        Idempotent: if ``token`` already holds a slot, renew its heartbeat and
        return it; otherwise claim the lowest free ordinal (one with no fresh
        heartbeat). The opening DELETE of stale slots takes SQLite's write lock,
        serializing concurrent claims so two starting replicas can't grab the same
        ordinal (the loser blocks, re-reads, and takes the next free one).
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stale = (datetime.now(UTC) - timedelta(seconds=lease_ttl_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            await session.execute(delete(CollectorSlot).where(CollectorSlot.updated_at < stale))
            existing = (
                await session.execute(select(CollectorSlot).where(CollectorSlot.token == token))
            ).scalar_one_or_none()
            if existing is not None:
                existing.updated_at = now
                await session.commit()
                return int(existing.ordinal)
            taken = set((await session.execute(select(CollectorSlot.ordinal))).scalars().all())
            ordinal = 0
            while ordinal in taken:
                ordinal += 1
            session.add(CollectorSlot(ordinal=ordinal, token=token, updated_at=now))
            await session.commit()
            return ordinal

    @_retry_on_locked()
    async def renew_collector_slot(
        self, token: str, ordinal: int, lease_ttl_seconds: float
    ) -> bool:
        """Re-assert ownership of a SPECIFIC ordinal for the renew loop.

        Unlike claim_collector_slot (which grabs the lowest free ordinal at
        startup), this keeps the process on its ORIGINAL ordinal so its
        collector_id never drifts: if the process stalls long enough for its slot
        to be reaped but no peer took the ordinal, it simply re-inserts it. Returns
        True while this token (still) holds ``ordinal``; returns False only if
        another token has taken it — a genuine identity loss the caller must treat
        as fatal (continuing would double-own under a shared id).
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stale = (datetime.now(UTC) - timedelta(seconds=lease_ttl_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            await session.execute(delete(CollectorSlot).where(CollectorSlot.updated_at < stale))
            row = await session.get(CollectorSlot, ordinal)
            if row is None:
                session.add(CollectorSlot(ordinal=ordinal, token=token, updated_at=now))
                await session.commit()
                return True
            if row.token == token:
                row.updated_at = now
                await session.commit()
                return True
            await session.commit()
            return False  # another process took our ordinal -> identity lost

    @_retry_on_locked()
    async def release_collector_slot(self, token: str) -> None:
        """Release this process's ordinal so a peer can reuse it immediately."""
        async with self.session_factory() as session:
            await session.execute(delete(CollectorSlot).where(CollectorSlot.token == token))
            await session.commit()

    async def count_all_shard_leases(self, lease_ttl_seconds: float) -> int:
        """Total fresh leases held across *all* collectors (fleet coverage check)."""
        fresh = (datetime.now(UTC) - timedelta(seconds=lease_ttl_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(ShardLease)
                    .where(ShardLease.updated_at >= fresh)
                )
            ).scalar_one()
            return int(total)

    async def get_owned_targets(
        self, collector_id: str, lease_ttl_seconds: float, enabled_only: bool = True
    ) -> list[Target]:
        """Enabled targets this collector holds a fresh lease on."""
        fresh = (datetime.now(UTC) - timedelta(seconds=lease_ttl_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            stmt = (
                select(Target)
                .join(ShardLease, ShardLease.target_id == Target.id)
                .where(ShardLease.collector_id == collector_id, ShardLease.updated_at >= fresh)
            )
            if enabled_only:
                stmt = stmt.where(Target.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @_retry_on_locked()
    async def release_shard_leases(self, collector_id: str) -> None:
        """Drop this collector's leases AND its membership/stats row on graceful
        shutdown, so peers rebalance immediately.

        Deleting the ``collector_stats`` row matters for the dynamic (HRW) path:
        peers derive the live-collector set from fresh ``collector_stats`` rows,
        so a departing collector must disappear from that set now — otherwise its
        freed targets sit unclaimed (nobody computes them as theirs) until the row
        ages out ``membership_ttl`` later.
        """
        async with self.session_factory() as session:
            await session.execute(delete(ShardLease).where(ShardLease.collector_id == collector_id))
            await session.execute(
                delete(CollectorStats).where(CollectorStats.collector_id == collector_id)
            )
            # Also drop this collector's heatmap rows so a departing shard's last
            # values don't linger in the Data Hall merge until they age out.
            await session.execute(
                delete(HeatmapSnapshot).where(HeatmapSnapshot.collector_id == collector_id)
            )
            await session.commit()

    # ---- Collector stats (per-shard, aggregated by the API) ----

    @_retry_on_locked()
    async def upsert_collector_stats(
        self, collector_id: str, data: dict, owned_targets: int
    ) -> None:
        """Publish this collector's health/subscription stats for API aggregation."""
        payload = json.dumps(data)
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self.session_factory() as session:
            row = await session.get(CollectorStats, collector_id)
            if row is None:
                session.add(
                    CollectorStats(
                        collector_id=collector_id,
                        data=payload,
                        owned_targets=owned_targets,
                        updated_at=now,
                    )
                )
            else:
                row.data = payload
                row.owned_targets = owned_targets
                row.updated_at = now
            await session.commit()

    async def get_collector_stats(self, max_age_seconds: float = 75.0) -> list[dict]:
        """All fresh per-collector stat rows (stale shards dropped).

        Default freshness window is kept at/below the shard lease TTL (75s) so a
        departed collector's row drops out before — not after — its leases are
        reclaimed, avoiding a window where fleet aggregates double-count.
        """
        fresh = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).replace(tzinfo=None)
        async with self.session_factory() as session:
            result = await session.execute(
                select(CollectorStats).where(CollectorStats.updated_at >= fresh)
            )
            out = []
            for row in result.scalars():
                try:
                    data = json.loads(row.data)
                except (ValueError, TypeError):
                    data = {}
                out.append(
                    {
                        "collector_id": row.collector_id,
                        "owned_targets": row.owned_targets,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        **data,
                    }
                )
            return out

    async def get_last_policy_collection_time(self, target_id: int) -> datetime | None:
        """Most recent policy-triggered collection time for a target (for rearm).

        Considers any non-failed policy collection (pending/collecting/completed)
        so an in-flight collection also holds off a duplicate.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.max(CollectedLog.collected_at)).where(
                    CollectedLog.target_id == target_id,
                    CollectedLog.trigger == "policy",
                    CollectedLog.status != "failed",
                )
            )
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    _COLLECTED_LOG_UPDATABLE = frozenset(
        {
            "status",
            "file_size_bytes",
            "error_message",
            "duration_ms",
        }
    )

    async def update_collected_log(self, log_id: int, **kwargs) -> CollectedLog | None:
        """Update a collected log record (status, file_size_bytes, error_message, duration_ms)."""
        async with self.session_factory() as session:
            result = await session.execute(select(CollectedLog).where(CollectedLog.id == log_id))
            log = result.scalar_one_or_none()
            if not log:
                return None
            for key, value in kwargs.items():
                if key in self._COLLECTED_LOG_UPDATABLE and hasattr(log, key):
                    setattr(log, key, value)
            await session.commit()
            await session.refresh(log)
            return log  # type: ignore[no-any-return]

    async def get_collected_log(self, log_id: int) -> CollectedLog | None:
        """Get a collected log by ID."""
        async with self.session_factory() as session:
            result = await session.execute(select(CollectedLog).where(CollectedLog.id == log_id))
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    # Upper bound on a single collected-logs page — prevents a request from
    # ever materializing the whole (unbounded, retention-sized) table.
    MAX_LOG_PAGE_SIZE = 1000

    async def get_all_collected_logs(
        self, limit: int | None = None, offset: int = 0
    ) -> list[CollectedLog]:
        """Get collected logs, newest first.

        ``limit`` is capped at :attr:`MAX_LOG_PAGE_SIZE`; callers that serve HTTP
        responses MUST pass a limit so a large history can't exhaust memory. A
        ``None`` limit (internal/batch callers) still returns the full set.
        """
        async with self.session_factory() as session:
            stmt = select(CollectedLog).order_by(CollectedLog.collected_at.desc())
            if limit is not None:
                stmt = stmt.limit(min(max(1, limit), self.MAX_LOG_PAGE_SIZE))
                if offset:
                    stmt = stmt.offset(max(0, offset))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_collected_logs(self) -> int:
        """Total number of collected-log records (for pagination)."""
        async with self.session_factory() as session:
            result = await session.execute(select(func.count()).select_from(CollectedLog))
            return int(result.scalar_one())

    async def delete_collected_log(self, log_id: int) -> CollectedLog | None:
        """Delete a collected log record and return it for file cleanup."""
        async with self.session_factory() as session:
            result = await session.execute(select(CollectedLog).where(CollectedLog.id == log_id))
            log = result.scalar_one_or_none()
            if not log:
                return None
            # Detach before delete so caller can read file_path
            file_path = log.file_path
            filename = log.filename
            log_id_val = log.id
            await session.delete(log)
            await session.commit()
            # Return a lightweight copy with the fields we need
            detached = CollectedLog(
                id=log_id_val,
                target_id=log.target_id,
                target_name=log.target_name,
                target_host=log.target_host,
                filename=filename,
                file_path=file_path,
                status=log.status,
            )
            return detached

    @_retry_on_locked()
    async def delete_prior_target_logs(
        self, target_id: int, keep_log_id: int
    ) -> list[CollectedLog]:
        """Delete a target's older *completed* diagnostic logs, keeping only
        ``keep_log_id`` (the freshest). Returns the removed records so the caller
        can delete their files.

        A new diagnostic bundle supersedes the previous one from the same node,
        so we don't retain duplicate copies — this bounds total stored logs to
        roughly one per node. Failed records are left untouched (audit trail);
        they carry no large file and are pruned by age-based retention.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(CollectedLog).where(
                    CollectedLog.target_id == target_id,
                    CollectedLog.id != keep_log_id,
                    CollectedLog.status == "completed",
                )
            )
            old = list(result.scalars().all())
            if not old:
                return []
            detached = [
                CollectedLog(
                    id=log.id,
                    target_id=log.target_id,
                    target_name=log.target_name,
                    target_host=log.target_host,
                    filename=log.filename,
                    file_path=log.file_path,
                    status=log.status,
                )
                for log in old
            ]
            await session.execute(
                delete(CollectedLog).where(CollectedLog.id.in_([log.id for log in old]))
            )
            await session.commit()
            return detached

    async def delete_expired_logs(self, max_age_days: int) -> list[CollectedLog]:
        """Find and delete logs older than max_age_days. Returns deleted records for file cleanup.

        Processed in bounded chunks so a large expiry backlog (e.g. after a long
        retention change or downtime) can't load the whole table into memory or
        issue one row-at-a-time delete per record.
        """
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        chunk_size = self.MAX_LOG_PAGE_SIZE
        detached: list[CollectedLog] = []
        while True:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(CollectedLog)
                    .where(CollectedLog.collected_at < cutoff)
                    .order_by(CollectedLog.collected_at.asc())
                    .limit(chunk_size)
                )
                expired = list(result.scalars().all())
                if not expired:
                    break
                ids = [log.id for log in expired]
                for log in expired:
                    # Capture file paths before deleting (for on-disk cleanup).
                    detached.append(
                        CollectedLog(
                            id=log.id,
                            target_id=log.target_id,
                            target_name=log.target_name,
                            target_host=log.target_host,
                            filename=log.filename,
                            file_path=log.file_path,
                            status=log.status,
                        )
                    )
                # Single bulk DELETE for the chunk instead of per-row ORM deletes.
                await session.execute(delete(CollectedLog).where(CollectedLog.id.in_(ids)))
                await session.commit()
                if len(expired) < chunk_size:
                    break
        return detached

    # ========================================================================
    # Alert Management
    # ========================================================================

    async def create_alerts_batch(self, alerts: list) -> int:
        """Persist a batch of AlertEvent objects, skipping duplicates.

        Args:
            alerts: List of AlertEvent objects from alert_subscriber

        Returns:
            Number of NEW rows actually inserted.

        Dedup is enforced by ``Alert.dedup_key`` (unique index). Rather than a
        dialect-specific upsert, we filter out keys that already exist and then
        insert the remainder — portable across SQLite/PostgreSQL/MySQL. This is
        safe because every alert write funnels through the single batch-processor
        consumer (one writer); the unique index remains a defensive backstop.
        """
        if not alerts:
            return 0

        # Build candidate rows, collapsing duplicates within this batch.
        rows: list[dict] = []
        seen_keys: set[str] = set()
        for alert_event in alerts:
            try:
                dedup_key = _compute_alert_dedup_key(
                    alert_event.target_id,
                    alert_event.message_id,
                    alert_event.message,
                    alert_event.event_timestamp,
                    alert_event.received_at,
                    getattr(alert_event, "source_id", None),
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                event_ts = _to_naive_utc(alert_event.event_timestamp)
                received = _to_naive_utc(alert_event.received_at) or _to_naive_utc(
                    datetime.now(UTC)
                )
                raw_event = getattr(alert_event, "raw", None) or None
                rows.append(
                    {
                        "target_id": alert_event.target_id,
                        "target_name": alert_event.target_name,
                        "target_bmc": alert_event.target_bmc,
                        "severity": alert_event.severity,
                        "message": alert_event.message,
                        "message_id": alert_event.message_id,
                        "event_type": alert_event.event_type,
                        "origin_of_condition": alert_event.origin_of_condition,
                        "event_timestamp": event_ts,
                        "received_at": received,
                        # Materialized occurrence time for indexed ordering/window.
                        "occurred_at": event_ts or received,
                        "dedup_key": dedup_key,
                        # JSON column stores the dict directly (JSONB on Postgres).
                        "raw_data": raw_event,
                        # Mark for background CPER decoding when eligible; NULL =
                        # not applicable (worker only touches 'pending').
                        "cper_status": "pending" if _cper_eligible(raw_event) else None,
                    }
                )
            except Exception as e:
                logger.warning(
                    f"Failed to prepare alert for batch from "
                    f"{getattr(alert_event, 'target_name', '?')}: {e}"
                )

        if not rows:
            return 0

        async with self.alert_session_factory() as session:
            # Filter out keys already persisted. Chunk the IN() to stay well
            # under any backend's bound-parameter limit.
            candidate_keys = [r["dedup_key"] for r in rows]
            existing: set[str] = set()
            for start in range(0, len(candidate_keys), 500):
                chunk = candidate_keys[start : start + 500]
                result = await session.execute(
                    select(Alert.dedup_key).where(Alert.dedup_key.in_(chunk))
                )
                existing.update(result.scalars().all())

            new_rows = [r for r in rows if r["dedup_key"] not in existing]
            if not new_rows:
                return 0

            try:
                session.add_all([Alert(**r) for r in new_rows])
                await session.commit()
                return len(new_rows)
            except IntegrityError:
                # A concurrent writer inserted an overlapping dedup_key between
                # our existence check and this commit. Rather than lose the whole
                # batch, re-insert row-by-row and skip only the true collisions.
                await session.rollback()
                inserted = 0
                for r in new_rows:
                    try:
                        async with session.begin():
                            session.add(Alert(**r))
                        inserted += 1
                    except IntegrityError:
                        await session.rollback()
                return inserted

    async def alert_source_seen(self, target_id: int, source_id: str) -> bool:
        """Whether a timestamp-less baseline entry is already stored.

        Lets the baseline puller skip re-fetching entries it has already
        persisted. Uses the same ``source_id`` dedup key so the lookup is served
        by the unique ``ix_alerts_dedup_key`` index.
        """
        key = _compute_alert_dedup_key(target_id, None, None, None, None, source_id)
        async with self.alert_session_factory() as session:
            row = await session.scalar(select(Alert.id).where(Alert.dedup_key == key))
            return row is not None

    async def get_pending_cper_alerts(
        self, limit: int = 20, max_attempts: int = 3, attempt_cooldown_seconds: int = 0
    ) -> list[PendingCper]:
        """Fetch lightweight descriptors of alerts awaiting CPER decoding.

        Selects only the columns the worker needs — id, target, the attachment
        URI, attempt count, and whether a decoded blob already exists — instead
        of loading the full row (avoids reading the large cper_decoded/raw_data
        JSONB every cycle). The decoded blob is loaded lazily via get_alert_cper
        only on the rare re-summarize path. Served by ix_alerts_cper_pending.

        ``attempt_cooldown_seconds`` (>0) excludes rows attempted within the last
        N seconds, so a just-failed (still-pending) row isn't immediately
        re-selected by the adaptive loop — spacing out the bounded retries.
        """
        conds = [Alert.cper_status == "pending", Alert.cper_attempts < max_attempts]
        if attempt_cooldown_seconds and attempt_cooldown_seconds > 0:
            cutoff = _to_naive_utc(datetime.now(UTC) - timedelta(seconds=attempt_cooldown_seconds))
            conds.append((Alert.cper_attempted_at.is_(None)) | (Alert.cper_attempted_at < cutoff))
        async with self.alert_session_factory() as session:
            result = await session.execute(
                select(
                    Alert.id,
                    Alert.target_id,
                    Alert.raw_data["AdditionalDataURI"].as_string().label("uri"),
                    Alert.cper_attempts,
                    Alert.cper_decoded.is_not(None).label("has_decoded"),
                )
                .where(*conds)
                .order_by(Alert.occurred_at.asc())
                .limit(limit)
            )
            return [
                PendingCper(
                    id=row.id,
                    target_id=row.target_id,
                    uri=row.uri,
                    attempts=row.cper_attempts or 0,
                    has_decoded=bool(row.has_decoded),
                )
                for row in result.all()
            ]

    async def set_cper_results_batch(self, results: list[dict]) -> int:
        """Persist a batch of CPER decode outcomes, isolating per-row failures.

        Each result: {id, status, refined_message?, decoded?, increment_attempt?}.
        Uses targeted UPDATE-by-id (no ORM row load, no per-blob read). Each row
        is written in its own SAVEPOINT so a single bad row (e.g. an oversized
        blob) can't roll back the whole batch and leave every row 'pending' — that
        would re-fetch the whole batch from BMCs forever (poison-pill loop).
        Returns the number of rows successfully written.
        """
        if not results:
            return 0
        now = _to_naive_utc(datetime.now(UTC))
        written = 0
        async with self.alert_session_factory() as session:
            for r in results:
                values: dict = {"cper_status": r["status"], "cper_attempted_at": now}
                if r.get("refined_message") is not None:
                    values["refined_message"] = r["refined_message"]
                if r.get("decoded") is not None:
                    values["cper_decoded"] = r["decoded"]
                if r.get("increment_attempt"):
                    # Atomic SQL increment — no read-modify-write.
                    values["cper_attempts"] = Alert.cper_attempts + 1
                try:
                    async with session.begin_nested():  # savepoint per row
                        await session.execute(
                            update(Alert).where(Alert.id == r["id"]).values(**values)
                        )
                    written += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "CPER result write failed for alert %s (%s): %s",
                        r.get("id"),
                        r.get("status"),
                        e,
                    )
            await session.commit()
        return written

    async def count_cper_by_status(self) -> dict[str, int]:
        """Count alerts grouped by cper_status (for resume/observability)."""
        async with self.alert_session_factory() as session:
            result = await session.execute(
                select(Alert.cper_status, func.count())
                .where(Alert.cper_status.is_not(None))
                .group_by(Alert.cper_status)
            )
            return dict(result.all())

    async def finalize_exhausted_cper(self, max_attempts: int) -> int:
        """Move 'pending' rows that have exhausted their retries to a terminal state.

        Guards against limbo rows (status 'pending' but cper_attempts >= max),
        which get_pending_cper_alerts skips and would otherwise never resolve —
        e.g. if a requeue left the attempt counter set. Returns rows finalized.
        """
        async with self.alert_session_factory() as session:
            result = await session.execute(
                update(Alert)
                .where(Alert.cper_status == "pending", Alert.cper_attempts >= max_attempts)
                .values(cper_status="fetch_failed")
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def requeue_failed_cper(self, older_than_minutes: int, limit: int = 0) -> int:
        """Requeue transiently-failed CPER rows whose last attempt is stale.

        Time-based (using ``cper_attempted_at``) so retries are restart-resilient:
        a fetch_failed row is retried ``older_than_minutes`` after its last
        attempt regardless of restarts — no in-memory timer to reset. Only
        ``fetch_failed`` (timeout/network/oversize) rows are requeued;
        ``unavailable`` (404/410, definitively gone) stays terminal.

        ``limit`` (>0) bounds how many are requeued per call so a recovered BMC
        with many failed rows isn't re-hammered all at once (spread over cycles).
        Returns rows requeued.
        """
        cutoff = _to_naive_utc(datetime.now(UTC) - timedelta(minutes=older_than_minutes))
        stale = (Alert.cper_attempted_at.is_(None)) | (Alert.cper_attempted_at < cutoff)
        async with self.alert_session_factory() as session:
            if limit and limit > 0:
                # Oldest-attempt first, bounded, so recovery is gradual.
                ids = (
                    (
                        await session.execute(
                            select(Alert.id)
                            .where(Alert.cper_status == "fetch_failed", stale)
                            .order_by(Alert.cper_attempted_at.asc().nulls_first())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    return 0
                where_clause = Alert.id.in_(ids)
            else:
                where_clause = (Alert.cper_status == "fetch_failed") & stale
            result = await session.execute(
                update(Alert).where(where_clause).values(cper_status="pending", cper_attempts=0)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def set_cper_result(
        self,
        alert_id: int,
        *,
        status: str,
        refined_message: str | None = None,
        decoded: dict | None = None,
        increment_attempt: bool = False,
    ) -> None:
        """Record the outcome of a CPER enrichment attempt for one alert."""
        async with self.alert_session_factory() as session:
            alert = await session.get(Alert, alert_id)
            if alert is None:
                return
            alert.cper_status = status
            if refined_message is not None:
                alert.refined_message = refined_message
            if decoded is not None:
                alert.cper_decoded = decoded
            if increment_attempt:
                alert.cper_attempts = (alert.cper_attempts or 0) + 1
            # Stamp the attempt time so time-based retry survives restarts.
            alert.cper_attempted_at = _to_naive_utc(datetime.now(UTC))
            await session.commit()

    async def get_alert_cper(self, alert_id: int) -> dict | None:
        """Return the decoded CPER JSON for one alert (lazy detail load)."""
        async with self.alert_session_factory() as session:
            row = await session.get(Alert, alert_id)
            return row.cper_decoded if row else None  # type: ignore[no-any-return]

    async def mark_eligible_cper_pending(self) -> int:
        """Backfill: mark already-stored eligible alerts as pending for decoding.

        Used once to enrich history. Scans rows with NULL cper_status whose
        raw_data indicates CPER, in Python (dialect-portable), and flips them to
        'pending'. Returns the number marked.

        Processed in id-ordered chunks loading only (id, raw_data) — never the
        whole alerts table (incl. large JSONB blobs) at once — so the backfill
        stays memory-bounded on a big history.
        """
        marked = 0
        chunk = 1000
        last_id = 0
        while True:
            async with self.alert_session_factory() as session:
                result = await session.execute(
                    select(Alert.id, Alert.raw_data)
                    .where(
                        Alert.cper_status.is_(None),
                        Alert.raw_data.is_not(None),
                        Alert.id > last_id,
                    )
                    .order_by(Alert.id.asc())
                    .limit(chunk)
                )
                batch = result.all()
                if not batch:
                    break
                last_id = batch[-1].id
                eligible_ids = [row.id for row in batch if _cper_eligible(row.raw_data)]
                if eligible_ids:
                    await session.execute(
                        update(Alert)
                        .where(Alert.id.in_(eligible_ids))
                        .values(cper_status="pending")
                    )
                    await session.commit()
                    marked += len(eligible_ids)
                if len(batch) < chunk:
                    break
        return marked

    async def get_alert(self, alert_id: int) -> Alert | None:
        """Get a single alert by ID."""
        async with self.alert_session_factory() as session:
            result = await session.execute(select(Alert).where(Alert.id == alert_id))
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    def _alert_filters(
        self,
        query,
        *,
        target_id=None,
        severity=None,
        since=None,
        severity_in=None,
        severity_not_in=None,
        search=None,
    ):
        """Apply the shared alert filter predicates to a select() query."""
        if target_id is not None:
            query = query.where(Alert.target_id == target_id)
        if severity:
            query = query.where(Alert.severity == severity)
        if severity_in:
            query = query.where(Alert.severity.in_(severity_in))
        if severity_not_in:
            query = query.where(Alert.severity.not_in(severity_not_in))
        if since:
            query = query.where(Alert.occurred_at >= _to_naive_utc(since))
        if search:
            like = f"%{search.lower()}%"
            query = query.where(
                func.lower(Alert.target_name).like(like)
                | func.lower(Alert.target_bmc).like(like)
                | func.lower(Alert.message).like(like)
            )
        return query

    async def get_alerts(
        self,
        target_id: int | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        severity_in: list[str] | None = None,
        severity_not_in: list[str] | None = None,
        search: str | None = None,
        include_raw: bool = False,
    ) -> list[Alert]:
        """Get alerts with optional filtering, latest-first by occurrence time.

        Ordering/window use the indexed ``occurred_at`` column (materialized
        event_timestamp-or-received_at) for efficiency at scale.

        ``raw_data`` (the large JSON blob) is deferred by default so list views
        don't pull it for every row — the per-alert raw endpoint loads it on
        demand. Set ``include_raw=True`` when the caller needs it inline.
        """
        async with self.alert_session_factory() as session:
            query = self._alert_filters(
                select(Alert),
                target_id=target_id,
                severity=severity,
                since=since,
                severity_in=severity_in,
                severity_not_in=severity_not_in,
                search=search,
            )
            if not include_raw:
                query = query.options(defer(Alert.raw_data))
            # Decoded CPER can be large; it's only shown in the detail view, so
            # keep it out of the list payload (lazy-loaded via /api/{id}/cper).
            query = query.options(defer(Alert.cper_decoded))
            # id.desc() is a deterministic tiebreaker: occurred_at is not unique
            # (baseline entries can share a timestamp), so without it rows with
            # equal occurred_at could be skipped/duplicated across pages.
            query = (
                query.order_by(Alert.occurred_at.desc(), Alert.id.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(query)
            return list(result.scalars().all())

    async def count_alerts_grouped_by_severity(
        self,
        target_id: int | None = None,
        since: datetime | None = None,
        search: str | None = None,
    ) -> dict[str, int]:
        """Count alerts per severity in one grouped query (same filters as the list).

        Lets the alerts page get its Critical/Warning pane counts with a single
        scan instead of one COUNT per severity.
        """
        async with self.alert_session_factory() as session:
            query = self._alert_filters(
                select(Alert.severity, func.count(Alert.id)),
                target_id=target_id,
                since=since,
                search=search,
            ).group_by(Alert.severity)
            result = await session.execute(query)
            return dict(result.all())

    async def count_alerts(
        self,
        target_id: int | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        severity_in: list[str] | None = None,
        severity_not_in: list[str] | None = None,
        search: str | None = None,
    ) -> int:
        """Count alerts matching the same filters as get_alerts (for pagination)."""
        async with self.alert_session_factory() as session:
            query = self._alert_filters(
                select(func.count(Alert.id)),
                target_id=target_id,
                severity=severity,
                since=since,
                severity_in=severity_in,
                severity_not_in=severity_not_in,
                search=search,
            )
            return int(await session.scalar(query) or 0)

    async def count_alerts_by_target_severity(
        self, since: datetime | None = None
    ) -> dict[tuple[int, str], int]:
        """Count alerts grouped by (target_id, severity) in one query.

        Backs the subscription-status view without loading rows per target.
        """
        async with self.alert_session_factory() as session:
            query = select(Alert.target_id, Alert.severity, func.count(Alert.id))
            if since:
                query = query.where(Alert.occurred_at >= _to_naive_utc(since))
            query = query.group_by(Alert.target_id, Alert.severity)
            result = await session.execute(query)
            return {(row[0], row[1]): row[2] for row in result.all()}

    async def get_alert_stats(self) -> dict:
        """Get alert statistics using efficient SQL aggregation.

        Returns:
            Dictionary with total, critical, warning, ok, and last_24h counts
        """
        async with self.alert_session_factory() as session:
            # Count total and by severity in a single query using SQL aggregation
            counts_query = select(
                func.count(Alert.id).label("total"),
                func.sum(case((Alert.severity == "Critical", 1), else_=0)).label("critical"),
                func.sum(case((Alert.severity == "Warning", 1), else_=0)).label("warning"),
                func.sum(case((Alert.severity == "OK", 1), else_=0)).label("ok"),
            )
            result = await session.execute(counts_query)
            row = result.one()

            # Count recent alerts (last 24 hours by occurrence time)
            since_24h = _to_naive_utc(datetime.now(UTC) - timedelta(hours=24))
            recent_count = await session.scalar(
                select(func.count(Alert.id)).where(Alert.occurred_at >= since_24h)
            )

            return {
                "total": row.total or 0,
                "critical": row.critical or 0,
                "warning": row.warning or 0,
                "ok": row.ok or 0,
                "last_24h": recent_count or 0,
            }

    async def ping_alert_store(self) -> bool:
        """Return True if the alert store is reachable (SELECT 1)."""
        try:
            async with self.alert_session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def delete_alerts_before(self, cutoff: datetime) -> int:
        """Bulk-delete alerts by RECEIVED time (retention = age since observed).

        Retention keys off ``received_at``, not ``occurred_at``, so historical
        events surfaced by a baseline pull (old occurrence date, just observed)
        are retained for the full window instead of being purged on arrival.
        Display ordering/window still use ``occurred_at``.

        Deletes in bounded chunks (keyed on the indexed ``received_at``) so a
        large purge does not hold one long transaction/lock or bloat WAL — it
        releases locks between chunks and is safe to run alongside ingest.
        """
        cutoff_naive = _to_naive_utc(cutoff)
        chunk = 5000
        total = 0
        while True:
            async with self.alert_session_factory() as session:
                # Delete a bounded slice: select the chunk's ids, then delete them.
                ids = (
                    (
                        await session.execute(
                            select(Alert.id).where(Alert.received_at < cutoff_naive).limit(chunk)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                result = await session.execute(delete(Alert).where(Alert.id.in_(ids)))
                await session.commit()
                total += int(result.rowcount or 0)
                if len(ids) < chunk:
                    break
        return total

    async def delete_alerts_by_target(self, target_id: int) -> int:
        """Bulk-delete all alerts for a target (single SQL statement)."""
        async with self.alert_session_factory() as session:
            result = await session.execute(delete(Alert).where(Alert.target_id == target_id))
            await session.commit()
            return int(result.rowcount or 0)

    # ---- Incremental-pull cursors (per target + log-entries collection) ----

    async def get_log_cursor(self, target_id: int, entries_uri: str) -> datetime | None:
        """Return the newest LogEntry.Created seen for this collection, if any."""
        async with self.alert_session_factory() as session:
            row = await session.scalar(
                select(LogCursor.last_created).where(
                    LogCursor.target_id == target_id,
                    LogCursor.entries_uri == entries_uri,
                )
            )
            return row  # type: ignore[no-any-return]

    async def set_log_cursor(
        self, target_id: int, entries_uri: str, last_created: datetime
    ) -> None:
        """Upsert the high-water mark for a log-entries collection."""
        last_created = _to_naive_utc(last_created)
        async with self.alert_session_factory() as session:
            existing = await session.scalar(
                select(LogCursor).where(
                    LogCursor.target_id == target_id,
                    LogCursor.entries_uri == entries_uri,
                )
            )
            if existing:
                if existing.last_created is None or (
                    last_created and last_created > existing.last_created
                ):
                    existing.last_created = last_created
                    existing.updated_at = _to_naive_utc(datetime.now(UTC))
            else:
                session.add(
                    LogCursor(
                        target_id=target_id,
                        entries_uri=entries_uri,
                        last_created=last_created,
                        updated_at=_to_naive_utc(datetime.now(UTC)),
                    )
                )
            try:
                await session.commit()
            except IntegrityError:
                # Another worker inserted this (target_id, entries_uri) cursor
                # concurrently (unique index). Fold our value into theirs instead
                # of failing: re-read and advance the high-water mark if ours is
                # newer.
                await session.rollback()
                async with self.alert_session_factory() as session2:
                    existing = await session2.scalar(
                        select(LogCursor).where(
                            LogCursor.target_id == target_id,
                            LogCursor.entries_uri == entries_uri,
                        )
                    )
                    if (
                        existing
                        and last_created
                        and (existing.last_created is None or last_created > existing.last_created)
                    ):
                        existing.last_created = last_created
                        existing.updated_at = _to_naive_utc(datetime.now(UTC))
                        await session2.commit()
