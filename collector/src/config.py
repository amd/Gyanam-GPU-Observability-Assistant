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
"""Configuration loader for the GPU Metrics Collector."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollingConfig(BaseModel):
    """Polling configuration."""

    interval_seconds: int = 300
    timeout_seconds: int = 30
    max_concurrent: int = 10
    task_poll_interval: int = 5
    task_timeout: int = 300
    error_retry_interval: int = 10  # Seconds to wait before retry on error
    download_timeout: int = 300  # Timeout for large file downloads


def _default_collect_body() -> dict:
    """Return default collect body for Redfish API."""
    return {"DiagnosticDataType": "OEM", "OEMDiagnosticDataType": "AllLogs"}


def _default_metric_reports() -> list[dict]:
    """Return default metric report URIs ordered by dedup priority.

    Specific reports are listed first so they claim their metrics.
    'All' is last and only fills its exclusive metrics (VR/HSC/IBC current & voltage).
    """
    return [
        {
            "uri": "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0",
            "report_type": "processor",
        },
        {
            "uri": "/redfish/v1/TelemetryService/MetricReports/OAM_MemoryMetrics_0",
            "report_type": "memory",
        },
        {
            "uri": "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorPortMetrics_0",
            "report_type": "interconnect",
        },
        {
            "uri": "/redfish/v1/TelemetryService/MetricReports/PlatformSensorsMetrics_0",
            "report_type": "platform",
        },
        {"uri": "/redfish/v1/TelemetryService/MetricReports/HealthRollup", "report_type": "health"},
        {"uri": "/redfish/v1/TelemetryService/MetricReports/All", "report_type": "comprehensive"},
    ]


class MetricReportConfig(BaseModel):
    """Configuration for a single metric report URI."""

    uri: str
    report_type: str


class RedfishConfig(BaseModel):
    """Redfish API configuration."""

    collect_endpoint: str = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )
    collect_body: dict = Field(default_factory=_default_collect_body)
    cleanup_task_on_success: bool = True  # Delete task after successful download
    metric_reports: list[MetricReportConfig] = Field(
        default_factory=_default_metric_reports
    )  # Empty list disables GET-first


class InfluxDBConfig(BaseModel):
    """InfluxDB connection configuration."""

    url: str = "http://influxdb:8086"
    org: str = "prometheus"
    bucket: str = "gpu_metrics"
    batch_size: int = 1000  # Reduced from 5000 to avoid timeouts
    flush_interval_seconds: int = 10
    write_timeout_ms: int = 90000  # Write timeout in milliseconds (90s for high-scale)
    max_concurrent_writes: int = 5  # Maximum parallel write operations
    verify_ssl: bool = False  # Whether to verify SSL certificates


class AuthConfig(BaseModel):
    """Authentication configuration."""

    username: str = "admin"
    # bcrypt hash of 'changeme' — override in config.yaml for production
    password_hash: str = "$2b$12$DDVvJVK1RdIj//rkWa7g8Op8Sc00hu64FJ9lwMZ/.8hvlXkF7jLaW"


class UIConfig(BaseModel):
    """UI configuration."""

    port: int = 8080
    host: str = "0.0.0.0"
    auth: AuthConfig = Field(default_factory=AuthConfig)


class BlobConfig(BaseModel):
    """Blob processing configuration."""

    temp_dir: str = "/tmp/telemetry"
    cleanup_after_parse: bool = True
    max_blob_size: int = 104857600  # 100MB
    cleanup_max_age_seconds: int = 3600  # Max age for old file cleanup


class CollectedLogsConfig(BaseModel):
    """Configuration for on-demand diagnostic log collection."""

    storage_dir: str = "/app/data/collected_logs"
    retention_days: int = 30
    cleanup_interval_hours: int = 6
    max_concurrent_collections: int = 5
    task_timeout: int = 600  # 10 minutes — log collection can be slow
    download_timeout: int = 600


class PrometheusConfig(BaseModel):
    """Prometheus exporter configuration."""

    metrics_path: str = "/metrics"


class SSEConfig(BaseModel):
    """SSE (Server-Sent Events) subscription configuration."""

    default_endpoint: str = "/redfish/v1/EventService/SSE"
    reconnect_delay: int = 5  # Seconds before reconnecting after disconnect
    max_reconnect_delay: int = 300  # Max backoff delay
    connection_timeout: int = 30  # Timeout for initial connection


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class ParserConfig(BaseModel):
    """Parser/discovery configuration."""

    max_recursion_depth: int = 50  # Max depth for JSON traversal
    max_concurrent_processors: int = 4  # Parallel result processing workers


class AlertsConfig(BaseModel):
    """Alerts/SSE subscription configuration."""

    enabled: bool = True
    sse_endpoint: str = "/redfish/v1/EventService/SSE"
    webhook_base_url: str = "http://localhost:8081/redfish-webhook"  # Collector webhook receiver
    enable_webhook_fallback: bool = True  # Fallback to webhooks when SSE fails
    force_webhook_mode: bool = False  # Force webhook mode even if SSE is available (for testing)
    event_types: list[str] = Field(default_factory=lambda: ["Alert", "StatusChange"])
    severities: list[str] = Field(default_factory=lambda: ["Critical", "Warning"])
    reconnect_delay: int = 30
    # Circuit breaker and retry configuration
    max_retry_duration_hours: float = 24  # Stop retrying after this many hours
    cooldown_duration_hours: float = 6  # Cooldown period before auto-resume
    degraded_threshold_hours: float = 1  # Hours of failures to mark as degraded
    batch_size: int = 100
    batch_interval: float = 5.0
    max_queue_size: int = 10000
    retention_days: int = 30
    cleanup_interval_hours: int = 6
    # Rate limiting per target (prevents alert flooding from misbehaving BMCs)
    max_alerts_per_minute: int = 100  # Max alerts per target per minute


class AppConfig(BaseModel):
    """Main application configuration."""

    polling: PollingConfig = Field(default_factory=PollingConfig)
    redfish: RedfishConfig = Field(default_factory=RedfishConfig)
    sse: SSEConfig = Field(default_factory=SSEConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    influxdb: InfluxDBConfig = Field(default_factory=InfluxDBConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    blob: BlobConfig = Field(default_factory=BlobConfig)
    collected_logs: CollectedLogsConfig = Field(default_factory=CollectedLogsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)


class Settings(BaseSettings):
    """Environment-based settings that override config file."""

    # Metrics backend selection: "influxdb" (default) or "prometheus"
    metrics_backend: str = "influxdb"

    # InfluxDB settings from environment
    # Defaults are empty so YAML config values aren't silently overridden.
    # In Docker, these are set explicitly via docker-compose.yml.
    influxdb_url: str = ""
    influxdb_token: str = ""
    influxdb_org: str = ""
    influxdb_bucket: str = ""
    influxdb_batch_size: int = 0  # 0 = use config.yaml default
    influxdb_write_timeout_ms: int = 0  # 0 = use config.yaml default
    influxdb_max_concurrent_writes: int = 0  # 0 = use config.yaml default
    influxdb_flush_interval_seconds: int = 0  # 0 = use config.yaml default

    # Alert settings from environment
    alert_webhook_base_url: str = ""  # Empty = use config.yaml default
    alert_enable_webhook_fallback: bool | None = None  # None = use config.yaml default
    alert_force_webhook_mode: bool = False  # Force webhook mode for testing

    # Database
    database_url: str = "sqlite:///data/targets.db"

    # Encryption key for credentials
    encryption_key: str = ""

    # Config file paths
    config_path: str = "/app/config/config.yaml"
    schema_path: str = "/app/config/metrics_schema.yaml"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def load_yaml_config(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        return {}

    with open(path) as f:
        content = f.read()

    # Expand environment variables in the config
    content = os.path.expandvars(content)
    return yaml.safe_load(content) or {}


def load_config() -> tuple[AppConfig, Settings]:
    """Load and merge configuration from file and environment."""
    settings = Settings()

    # Load YAML config
    yaml_config = load_yaml_config(settings.config_path)

    # Create AppConfig from YAML
    app_config = AppConfig(**yaml_config) if yaml_config else AppConfig()

    # Override InfluxDB settings from environment if provided
    if settings.influxdb_url:
        app_config.influxdb.url = settings.influxdb_url
    if settings.influxdb_org:
        app_config.influxdb.org = settings.influxdb_org
    if settings.influxdb_bucket:
        app_config.influxdb.bucket = settings.influxdb_bucket
    if settings.influxdb_batch_size > 0:
        app_config.influxdb.batch_size = settings.influxdb_batch_size
    if settings.influxdb_write_timeout_ms > 0:
        app_config.influxdb.write_timeout_ms = settings.influxdb_write_timeout_ms
    if settings.influxdb_max_concurrent_writes > 0:
        app_config.influxdb.max_concurrent_writes = settings.influxdb_max_concurrent_writes
    if settings.influxdb_flush_interval_seconds > 0:
        app_config.influxdb.flush_interval_seconds = settings.influxdb_flush_interval_seconds

    # Override Alert settings from environment if provided
    if settings.alert_webhook_base_url:
        app_config.alerts.webhook_base_url = settings.alert_webhook_base_url
    if settings.alert_enable_webhook_fallback is not None:
        app_config.alerts.enable_webhook_fallback = settings.alert_enable_webhook_fallback
    if settings.alert_force_webhook_mode:
        app_config.alerts.force_webhook_mode = settings.alert_force_webhook_mode

    return app_config, settings


# Global config instance
_config: AppConfig | None = None
_settings: Settings | None = None


def get_config() -> AppConfig:
    """Get the application configuration."""
    global _config, _settings
    if _config is None:
        _config, _settings = load_config()
    return _config


def get_settings() -> Settings:
    """Get the environment settings."""
    global _config, _settings
    if _settings is None:
        _config, _settings = load_config()
    return _settings
