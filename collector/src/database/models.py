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

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all models."""

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

    # Connection mode: "direct" (default), "ssh_proxy", or "sse"
    # server_default ensures ALTER TABLE migration works on existing SQLite rows
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

    # SSH proxy settings (used when connection_mode == "ssh_proxy")
    ssh_proxy_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_proxy_port: Mapped[int] = mapped_column(Integer, default=22, server_default="22")
    ssh_proxy_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_ssh_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_ssh_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_command_template: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Polling configuration
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Tags to add to all metrics from this target
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string

    # Per-target metric report URI overrides (JSON list of {"uri": ..., "report_type": ...})
    metric_reports_override: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    @property
    def telemetry_url(self) -> str:
        """Get the full telemetry endpoint URL."""
        return f"{self.base_url}{self.telemetry_endpoint}"


class CollectedLog(Base):
    """Record of a collected diagnostic log bundle."""

    __tablename__ = "collected_logs"

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

    # Timestamps
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<CollectedLog(id={self.id}, target='{self.target_name}', status='{self.status}')>"


class Alert(Base):
    """Redfish SSE alert event from a target system."""

    __tablename__ = "alerts"

    # Composite index for common query pattern: filter by target + severity + time range
    __table_args__ = (
        Index("ix_alerts_target_severity_time", "target_id", "severity", "received_at"),
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
        DateTime, index=True, nullable=True
    )  # From EventTimestamp field
    received_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Alert(id={self.id}, target='{self.target_name}', "
            f"severity='{self.severity}', message='{self.message[:50]}...')>"
        )
