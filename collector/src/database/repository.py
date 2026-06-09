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

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import case, delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import defer

from .models import Alert, AlertBase, Base, CollectedLog, LogCursor, Target


@dataclass
class PendingCper:
    """Lightweight descriptor of an alert awaiting CPER decode (no blob load)."""

    id: int
    target_id: int
    uri: str | None
    attempts: int
    has_decoded: bool


logger = logging.getLogger(__name__)


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to naive UTC for consistent SQLite storage/compare.

    SQLite stores DateTime as an ISO string; mixing tz-aware values (which
    serialize with a ``+00:00`` suffix) and naive ones breaks lexicographic
    range comparisons. We store everything as naive UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


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
    def _build_engine(url: str, *, pool_size: int = 50, max_overflow: int = 50):
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
        connection_mode: str = "direct",
        sse_endpoint: str | None = None,
        alert_sse_endpoint: str | None = None,
        ssh_proxy_host: str | None = None,
        ssh_proxy_port: int = 22,
        ssh_proxy_username: str | None = None,
        ssh_key: str | None = None,
        ssh_password: str | None = None,
        ssh_command_template: str | None = None,
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
                connection_mode=connection_mode,
                sse_endpoint=sse_endpoint,
                alert_sse_endpoint=alert_sse_endpoint,
                ssh_proxy_host=ssh_proxy_host,
                ssh_proxy_port=ssh_proxy_port,
                ssh_proxy_username=ssh_proxy_username,
                encrypted_ssh_key=self.encryption.encrypt(ssh_key) if ssh_key else None,
                encrypted_ssh_password=self.encryption.encrypt(ssh_password)
                if ssh_password
                else None,
                ssh_command_template=ssh_command_template,
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

    async def get_target_by_ssh_proxy_host(self, ssh_proxy_host: str) -> Target | None:
        """Get an SSH proxy target by its proxy host address."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(Target).where(
                    Target.connection_mode == "ssh_proxy",
                    Target.ssh_proxy_host == ssh_proxy_host,
                )
            )
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    async def get_all_targets(self, enabled_only: bool = False) -> list[Target]:
        """Get all targets, optionally filtered by enabled status."""
        async with self.session_factory() as session:
            query = select(Target)
            if enabled_only:
                query = query.where(Target.enabled.is_(True))
            result = await session.execute(query.order_by(Target.name))
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
            "connection_mode",
            "sse_endpoint",
            "alert_sse_endpoint",
            "ssh_proxy_host",
            "ssh_proxy_port",
            "ssh_proxy_username",
            "ssh_command_template",
        }
    )

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

            # Handle SSH key encryption if provided
            if "ssh_key" in kwargs:
                ssh_key = kwargs.pop("ssh_key")
                kwargs["encrypted_ssh_key"] = self.encryption.encrypt(ssh_key) if ssh_key else None

            # Handle SSH password encryption if provided
            if "ssh_password" in kwargs:
                ssh_pwd = kwargs.pop("ssh_password")
                kwargs["encrypted_ssh_password"] = (
                    self.encryption.encrypt(ssh_pwd) if ssh_pwd else None
                )

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
                "encrypted_ssh_key",
                "encrypted_ssh_password",
            }
            for key, value in kwargs.items():
                if key in allowed and hasattr(target, key):
                    setattr(target, key, value)

            await session.commit()
            await session.refresh(target)
            return target  # type: ignore[no-any-return]

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

    def decrypt_ssh_key(self, target: Target) -> str | None:
        """Decrypt the SSH private key for a target, if present."""
        if target.encrypted_ssh_key:
            return self.encryption.decrypt(target.encrypted_ssh_key)
        return None

    def decrypt_ssh_password(self, target: Target) -> str | None:
        """Decrypt the SSH password for a target, if present."""
        if target.encrypted_ssh_password:
            return self.encryption.decrypt(target.encrypted_ssh_password)
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
            )
            session.add(log)
            await session.commit()
            await session.refresh(log)
            return log

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

    async def get_all_collected_logs(self) -> list[CollectedLog]:
        """Get all collected logs, newest first."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(CollectedLog).order_by(CollectedLog.collected_at.desc())
            )
            return list(result.scalars().all())

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

    async def delete_expired_logs(self, max_age_days: int) -> list[CollectedLog]:
        """Find and delete logs older than max_age_days. Returns deleted records for file cleanup."""
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        async with self.session_factory() as session:
            result = await session.execute(
                select(CollectedLog).where(CollectedLog.collected_at < cutoff)
            )
            expired = list(result.scalars().all())
            if not expired:
                return []
            # Capture file paths before deleting
            detached = []
            for log in expired:
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
                await session.delete(log)
            await session.commit()
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

            session.add_all([Alert(**r) for r in new_rows])
            await session.commit()
            return len(new_rows)

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
        """
        marked = 0
        async with self.alert_session_factory() as session:
            result = await session.execute(
                select(Alert).where(Alert.cper_status.is_(None), Alert.raw_data.is_not(None))
            )
            for alert in result.scalars():
                if _cper_eligible(alert.raw_data):
                    alert.cper_status = "pending"
                    marked += 1
            if marked:
                await session.commit()
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

    async def delete_alert(self, alert_id: int) -> Alert | None:
        """Delete an alert by ID. Returns the (detached) alert or None."""
        async with self.alert_session_factory() as session:
            result = await session.execute(
                select(Alert).where(Alert.id == alert_id).options(defer(Alert.raw_data))
            )
            alert = result.scalar_one_or_none()
            if alert:
                await session.delete(alert)
                await session.commit()
            return alert  # type: ignore[no-any-return]

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
            await session.commit()
