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

import json
import logging
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import case, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Alert, Base, CollectedLog, Target

logger = logging.getLogger(__name__)


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

    def __init__(self, database_url: str, encryption_key: str):
        """Initialize the repository.

        Args:
            database_url: SQLAlchemy async database URL
            encryption_key: Fernet key for credential encryption
        """
        # Convert sqlite:// to sqlite+aiosqlite://
        is_sqlite = database_url.startswith("sqlite://")
        if is_sqlite:
            database_url = database_url.replace("sqlite://", "sqlite+aiosqlite://")

        # Configure connection pool based on database type
        # SQLite: Smaller pool (WAL mode handles concurrency via file locking)
        # PostgreSQL/MySQL: Large pool for true concurrent connections
        if is_sqlite:
            # SQLite with WAL mode: moderate pool is sufficient
            # Writes are still serialized, but reads can be concurrent
            self.engine = create_async_engine(
                database_url,
                echo=False,
                pool_size=20,  # Moderate pool for SQLite
                max_overflow=10,  # Total 30 max connections
                pool_pre_ping=True,
            )

            # Enable WAL mode for better concurrent read/write performance
            # This is set via an engine event so it runs on every new connection
            @event.listens_for(self.engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        else:
            # PostgreSQL/MySQL: large pool for high-scale deployments (300+ endpoints)
            self.engine = create_async_engine(
                database_url,
                echo=False,
                pool_size=50,  # Large pool for concurrent database connections
                max_overflow=50,  # Total 100 max connections
                pool_recycle=3600,  # Recycle connections after 1 hour
                pool_pre_ping=True,
            )

        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.encryption = CredentialEncryption(encryption_key)

    async def init_db(self) -> None:
        """Initialize the database schema.

        Also migrates existing tables by adding any columns that are
        present in the models but missing from the database (SQLAlchemy's
        create_all only creates new tables, it won't ALTER existing ones).
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            # Lightweight migration: add missing columns to existing tables
            await conn.run_sync(self._migrate_missing_columns)

    @staticmethod
    def _migrate_missing_columns(conn) -> None:
        """Add columns and indexes that exist in models but not in the database."""
        import sqlalchemy as sa

        inspector = sa.inspect(conn)

        # Migrate missing columns
        for table in Base.metadata.sorted_tables:
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
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue

            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}

            for index in table.indexes:
                if index.name not in existing_indexes:
                    # Create the index using SQLAlchemy DDL
                    index.create(conn)

    async def close(self) -> None:
        """Close the database connection."""
        await self.engine.dispose()

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
        """Decrypt the password for a target."""
        return self.encryption.decrypt(target.encrypted_password)

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

    async def create_alert(
        self,
        target_id: int,
        target_name: str,
        target_bmc: str,
        severity: str,
        message: str,
        event_type: str,
        message_id: str | None = None,
        origin_of_condition: str | None = None,
        event_timestamp: datetime | None = None,
    ) -> Alert:
        """Create a new alert record."""
        async with self.session_factory() as session:
            alert = Alert(
                target_id=target_id,
                target_name=target_name,
                target_bmc=target_bmc,
                severity=severity,
                message=message,
                message_id=message_id,
                event_type=event_type,
                origin_of_condition=origin_of_condition,
                event_timestamp=event_timestamp,
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)
            return alert

    async def create_alerts_batch(self, alerts: list) -> int:
        """Create multiple alerts in a single transaction.

        Args:
            alerts: List of AlertEvent objects from alert_subscriber

        Returns:
            Number of alerts successfully created

        Note: All alerts are committed in a single transaction. If any alert
        fails validation, the entire batch is rolled back.
        """
        if not alerts:
            return 0

        async with self.session_factory() as session:
            created_count = 0
            for alert_event in alerts:
                try:
                    alert = Alert(
                        target_id=alert_event.target_id,
                        target_name=alert_event.target_name,
                        target_bmc=alert_event.target_bmc,
                        severity=alert_event.severity,
                        message=alert_event.message,
                        message_id=alert_event.message_id,
                        event_type=alert_event.event_type,
                        origin_of_condition=alert_event.origin_of_condition,
                        event_timestamp=alert_event.event_timestamp,
                        received_at=alert_event.received_at,
                    )
                    session.add(alert)
                    created_count += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to add alert to batch from {alert_event.target_name}: {e}"
                    )
                    # Continue adding other alerts

            if created_count > 0:
                await session.commit()
                return created_count
            return 0

    async def get_alert(self, alert_id: int) -> Alert | None:
        """Get a single alert by ID."""
        async with self.session_factory() as session:
            result = await session.execute(select(Alert).where(Alert.id == alert_id))
            return result.scalar_one_or_none()  # type: ignore[no-any-return]

    async def get_alerts(
        self,
        target_id: int | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Alert]:
        """Get alerts with optional filtering.

        Args:
            target_id: Filter by target ID
            severity: Filter by severity (Critical, Warning, OK)
            since: Only return alerts after this timestamp
            limit: Maximum number of alerts to return
            offset: Number of alerts to skip (for pagination)

        Returns:
            List of alerts ordered by received_at desc
        """
        async with self.session_factory() as session:
            query = select(Alert)

            # Apply filters
            if target_id is not None:
                query = query.where(Alert.target_id == target_id)
            if severity:
                query = query.where(Alert.severity == severity)
            if since:
                query = query.where(Alert.received_at >= since)

            # Order and paginate
            query = query.order_by(Alert.received_at.desc()).limit(limit).offset(offset)

            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_alert_stats(self) -> dict:
        """Get alert statistics using efficient SQL aggregation.

        Returns:
            Dictionary with total, critical, warning, ok, and last_24h counts
        """
        async with self.session_factory() as session:
            # Count total and by severity in a single query using SQL aggregation
            counts_query = select(
                func.count(Alert.id).label("total"),
                func.sum(case((Alert.severity == "Critical", 1), else_=0)).label("critical"),
                func.sum(case((Alert.severity == "Warning", 1), else_=0)).label("warning"),
                func.sum(case((Alert.severity == "OK", 1), else_=0)).label("ok"),
            )
            result = await session.execute(counts_query)
            row = result.one()

            # Count recent alerts (last 24 hours)
            since_24h = datetime.now(UTC) - timedelta(hours=24)
            recent_count = await session.scalar(
                select(func.count(Alert.id)).where(Alert.received_at >= since_24h)
            )

            return {
                "total": row.total or 0,
                "critical": row.critical or 0,
                "warning": row.warning or 0,
                "ok": row.ok or 0,
                "last_24h": recent_count or 0,
            }

    async def delete_alert(self, alert_id: int) -> Alert | None:
        """Delete an alert by ID."""
        async with self.session_factory() as session:
            result = await session.execute(select(Alert).where(Alert.id == alert_id))
            alert = result.scalar_one_or_none()
            if alert:
                await session.delete(alert)
                await session.commit()
            return alert  # type: ignore[no-any-return]

    async def delete_alerts_before(self, cutoff: datetime) -> int:
        """Delete alerts older than cutoff timestamp.

        Returns:
            Number of alerts deleted
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Alert).where(Alert.received_at < cutoff))
            alerts = list(result.scalars().all())
            count = len(alerts)
            for alert in alerts:
                await session.delete(alert)
            await session.commit()
            return count

    async def delete_alerts_by_target(self, target_id: int) -> int:
        """Delete all alerts for a specific target.

        Returns:
            Number of alerts deleted
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Alert).where(Alert.target_id == target_id))
            alerts = list(result.scalars().all())
            count = len(alerts)
            for alert in alerts:
                await session.delete(alert)
            await session.commit()
            return count
