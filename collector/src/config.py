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

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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
    reconnect_delay: int = Field(default=30, ge=1)
    # Circuit breaker and retry configuration
    max_retry_duration_hours: float = 24  # Stop retrying after this many hours
    cooldown_duration_hours: float = 6  # Cooldown period before auto-resume
    degraded_threshold_hours: float = 1  # Hours of failures to mark as degraded
    permanent_failure_retry_hours: float = Field(
        default=6, ge=0
    )  # 0 = never auto-retry permanent webhook failures
    batch_size: int = Field(default=100, gt=0)
    batch_interval: float = Field(default=5.0, gt=0)
    # Bounded queue is a safety valve; 0 would mean an *unbounded* asyncio.Queue.
    max_queue_size: int = Field(default=10000, gt=0)
    retention_days: int = Field(default=30, gt=0)
    cleanup_interval_hours: int = Field(default=6, gt=0)
    # Rate limiting per target (prevents alert flooding from misbehaving BMCs)
    max_alerts_per_minute: int = Field(default=100, gt=0)
    # Baseline pull: fetch existing LogService entries on subscription start so
    # standing/active conditions appear even before a new event fires.
    baseline_pull_enabled: bool = True
    baseline_max_entries_per_log: int = Field(default=200, gt=0)
    baseline_repull_interval_minutes: int = Field(default=60, ge=0)  # 0 disables periodic re-pull
    # CPER (Common Platform Error Record) enrichment: background-decode attachments
    # referenced by CPER alerts into a refined human-readable message.
    cper_enrichment_enabled: bool = True
    cper_poll_interval_seconds: int = Field(default=30, gt=0)
    cper_max_attempts: int = Field(default=3, gt=0)
    cper_batch_size: int = Field(default=20, gt=0)
    cper_concurrency: int = Field(default=8, gt=0)  # global concurrent BMC fetches/decodes
    # Max concurrent fetches to a SINGLE BMC — keeps one busy/crashed target from
    # being hammered (and starving others) during a burst.
    cper_per_target_concurrency: int = Field(default=1, gt=0)
    # Maintenance (requeue/finalize/backlog-count) cadence, expressed as a
    # multiple of the poll interval; applied on a wall-clock basis so the
    # adaptive drain (which loops sub-second) can't collapse it. 10 * 30s = ~5min.
    cper_maintenance_every_cycles: int = Field(default=10, gt=0)
    # Min seconds between decode attempts on the same alert, so a just-failed
    # (still-pending) row isn't immediately re-fetched by the adaptive loop.
    cper_attempt_cooldown_seconds: int = Field(default=60, ge=0)
    # BMC attachment serving can be slow (tens of seconds); concurrency keeps a
    # generous per-fetch timeout affordable.
    cper_fetch_timeout: float = Field(default=60.0, gt=0)
    # Periodically requeue transiently-failed (fetch_failed) rows so a slow/busy
    # BMC recovers automatically. 0 disables. (404/410 -> 'unavailable' stays
    # terminal and is not requeued.)
    cper_retry_failed_interval_minutes: int = Field(default=360, ge=0)
    cper_decode_timeout: float = Field(default=15.0, gt=0)
    cper_max_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    cper_convert_path: str = "/usr/local/bin/cper-convert"

    @field_validator("severities")
    @classmethod
    def _validate_severities(cls, v: list[str]) -> list[str]:
        allowed = {"Critical", "Warning", "OK"}
        if not v:
            raise ValueError("alerts.severities must not be empty")
        bad = [s for s in v if s not in allowed]
        if bad:
            raise ValueError(
                f"alerts.severities has invalid entries {bad}; allowed: {sorted(allowed)}"
            )
        return v


def _default_location_token_rules() -> list[dict]:
    """Generic prefix rules for decoding placement from a hostname token.

    Each rule matches a single ``- _ .``-delimited token and claims a field.
    Deliberately conservative and reorderable — operators tailor these (and add
    ``name_patterns``) to their own site convention. Note ``r<n>`` maps to a
    rack by common convention; sites that spell racks differently should adjust.
    """
    return [
        {"field": "hall", "pattern": r"dh(\d+)"},
        {"field": "hall", "pattern": r"hall(\d+)"},
        {"field": "row", "pattern": r"row(\d+)"},
        {"field": "rack", "pattern": r"rack(\d+)"},
        {"field": "rack", "pattern": r"r(\d+)"},
        {"field": "rack", "pattern": r"k(\d+)"},
        {"field": "rack_u", "pattern": r"ru(\d+)"},
        {"field": "rack_u", "pattern": r"u(\d+)"},
    ]


class LocationTokenRule(BaseModel):
    """A single hostname-token placement rule: (field, single-token regex)."""

    field: str
    pattern: str

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        allowed = {"site", "hall", "row", "rack", "rack_u", "height"}
        if v not in allowed:
            raise ValueError(f"location token rule field must be one of {sorted(allowed)}")
        return v


class LocationConfig(BaseModel):
    """Data-hall placement configuration (rack geometry + hostname decoding)."""

    # Rack height in rack units. Default is an OCP Open Rack v3 frame (48 OpenU).
    # OCP Open Rack v3 reference: 600 mm external width, ~1200 mm depth, and an
    # "OpenU" (OU) pitch of 48 mm (vs 44.45 mm for a 19" EIA-310 RU).
    rack_height_u: int = Field(default=48, gt=0)
    gpus_per_system: int = Field(default=8, ge=0)
    # Default chassis height in rack units when a system's height isn't known.
    # Defaulted to 4U as a conservative single-system footprint (overridden by a
    # BMC-reported height or a hostname-encoded height when available).
    default_system_height_u: int = Field(default=4, gt=0)
    # Rack-unit standard assumed when a source doesn't specify one:
    # "OpenU" (OCP Open Rack, 48 mm) or "EIA_310" (19" rack, 1.75 in / 44.45 mm).
    default_unit_type: str = "OpenU"
    # Full-hostname regexes with named groups (site/hall/row/rack/rack_u/height),
    # tried before token rules. Empty by default — add site-specific patterns here.
    name_patterns: list[str] = Field(default_factory=list)
    token_rules: list[LocationTokenRule] = Field(
        default_factory=lambda: [LocationTokenRule(**r) for r in _default_location_token_rules()]
    )


class InventoryConfig(BaseModel):
    """Background Redfish inventory-enrichment configuration."""

    enabled: bool = True
    # How often the enricher loop wakes to look for systems needing inventory.
    collect_interval_seconds: int = Field(default=300, gt=0)
    # Re-pull inventory for a system once it's older than this (staleness).
    # 0 means pull exactly once per system and never refresh.
    refresh_interval_hours: float = Field(default=24, ge=0)
    # Max concurrent BMC inventory fetches.
    max_concurrent: int = Field(default=8, gt=0)
    # Per-request timeout for inventory GETs (seconds).
    request_timeout: int = Field(default=30, gt=0)
    # Drop pre-aggregated statistical MetricReports (e.g. AvgPowerConsumptionHour)
    # during discovery — GYANAM downsamples itself, so polling them just multiplies
    # per-cycle GETs. Operators can still pin one via a per-target report override.
    discovery_exclude_aggregate_reports: bool = True


class PolicyConfig(BaseModel):
    """Automated diagnostic-log-collection policy.

    Models the gyanam-owned Redfish PolicyService (exposed read-only at
    /redfish/v1/PolicyService): on a fatal/critical event gyanam collects a
    diagnostic dump, with a per-target rearm window (hysteresis) so a storm of
    events can't trigger a tight collection loop. The standard Redfish Policy
    schema has no rearm property, so rearm_seconds is enforced by the engine and
    surfaced as Oem.Gyanam.RearmSeconds on the exposed Policy.
    """

    enabled: bool = True
    # Disabled | AlertOnly | Enabled (mirrors PolicyService.OperatingMode).
    # "Enabled" = collect; "AlertOnly" = evaluate/log but don't collect.
    operating_mode: str = "Enabled"
    # Per-target hysteresis: minimum seconds between policy-triggered collections
    # for the same target. Default 2h.
    rearm_seconds: int = Field(default=7200, ge=0)
    # Event severities that fire the policy (case-insensitive).
    trigger_severities: list[str] = Field(default_factory=lambda: ["Critical", "Fatal"])
    # Optional allow-list of Redfish MessageIds; empty = any MessageId at a
    # triggering severity.
    trigger_message_ids: list[str] = Field(default_factory=list)


class ShardConfig(BaseModel):
    """Horizontal sharding: distribute targets across multiple collector processes.

    Off by default — a single collector owns every target (current behavior, no
    lease bookkeeping). When enabled, each collector claims up to
    ``max_targets_per_shard`` targets via DB leases, renews them as a heartbeat,
    and reclaims targets whose owner's heartbeat went stale. Scale the collector
    replica count up/down (docker compose --scale / k8s) and targets rebalance
    automatically.
    """

    enabled: bool = False
    # Assignment strategy:
    #   "dynamic" — Rendezvous (HRW) hashing: targets are balanced across live
    #     collectors to within ±1 and rebalance with minimal movement as replicas
    #     come and go (recommended default).
    #   "static"  — greedy id-order claiming up to max_targets_per_shard per
    #     collector (fills replicas in turn; one may sit idle). The proven
    #     fallback — flip here or via COLLECTOR_BALANCE if dynamic misbehaves.
    balance: Literal["static", "dynamic"] = "dynamic"
    # Max targets one collector will own. In dynamic mode this is a SAFETY CEILING
    # (the fair share ceil(N/live) is the real per-pass bound); in static mode it
    # is the claim cap. 200 covers a ~500-target fleet across 3 replicas.
    max_targets_per_shard: int = Field(default=200, gt=0)
    # Seconds between claim/renew passes.
    claim_interval_seconds: float = Field(default=20.0, gt=0)
    # A lease older than this (owner stopped heartbeating) is reclaimable.
    lease_ttl_seconds: float = Field(default=75.0, gt=0)
    # Dynamic mode: a collector is "live" if its stats row is fresher than this.
    # Must be >= 2x the claim interval (so one missed pass doesn't flap a live
    # collector out of the set) and <= lease_ttl (so a dead collector leaves the
    # live set no later than its leases go stale, keeping the view conservative).
    membership_ttl_seconds: float = Field(default=75.0, gt=0)

    @model_validator(mode="after")
    def _check_heartbeat_margin(self) -> "ShardConfig":
        # The heartbeat cadence equals claim_interval; the TTL must comfortably
        # exceed it so a live collector never lets its own leases expire between
        # passes (which would flap ownership). Require at least a 2x margin to
        # tolerate one missed/slow pass.
        if self.lease_ttl_seconds < 2 * self.claim_interval_seconds:
            raise ValueError(
                "sharding.lease_ttl_seconds must be >= 2 * claim_interval_seconds "
                f"(got ttl={self.lease_ttl_seconds}, interval={self.claim_interval_seconds}); "
                "otherwise a live collector's leases can expire between heartbeats"
            )
        if self.membership_ttl_seconds < 2 * self.claim_interval_seconds:
            raise ValueError(
                "sharding.membership_ttl_seconds must be >= 2 * claim_interval_seconds "
                f"(got {self.membership_ttl_seconds}, interval={self.claim_interval_seconds}); "
                "otherwise one missed heartbeat flaps a live collector out of the set"
            )
        if self.membership_ttl_seconds > self.lease_ttl_seconds:
            raise ValueError(
                "sharding.membership_ttl_seconds must be <= lease_ttl_seconds "
                f"(got membership={self.membership_ttl_seconds}, lease={self.lease_ttl_seconds}); "
                "otherwise a dead collector stays in the live set after its leases go "
                "stale, so survivors exclude its (now unowned) targets and orphan them"
            )
        return self


class AppConfig(BaseModel):
    """Main application configuration."""

    polling: PollingConfig = Field(default_factory=PollingConfig)
    redfish: RedfishConfig = Field(default_factory=RedfishConfig)
    sse: SSEConfig = Field(default_factory=SSEConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    influxdb: InfluxDBConfig = Field(default_factory=InfluxDBConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    blob: BlobConfig = Field(default_factory=BlobConfig)
    collected_logs: CollectedLogsConfig = Field(default_factory=CollectedLogsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    sharding: ShardConfig = Field(default_factory=ShardConfig)


class Settings(BaseSettings):
    """Environment-based settings that override config file."""

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

    # Horizontal sharding (multi-collector). COLLECTOR_SHARDING enables it;
    # COLLECTOR_ID distinguishes replicas (defaults to the hostname, which is
    # unique per docker/k8s replica). MAX_TARGETS_PER_SHARD overrides the cap.
    collector_sharding: bool | None = None  # None = use config.yaml default
    collector_id: str = ""  # Empty = slot-claimed ordinal (dynamic) or hostname
    max_targets_per_shard: int = 0  # 0 = use config.yaml default
    collector_balance: str = ""  # "static"|"dynamic"; empty = use config.yaml default

    # UI credentials from environment (preferred over editing config.yaml).
    # UI_PASSWORD is bcrypt-hashed at load; UI_USERNAME overrides the admin name.
    ui_username: str = ""
    ui_password: str = ""

    # Database
    database_url: str = "sqlite:///data/targets.db"
    # Dedicated store for alerts (PostgreSQL). Required — set via ALERTS_DATABASE_URL.
    alerts_database_url: str = ""

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

    # os.path.expandvars silently leaves unresolved references (an unset ${VAR},
    # or the unsupported ${VAR:-default} form) as literal text — which would
    # become a literal config value like "${INFLUXDB_URL}" and fail obscurely
    # downstream. Fail fast instead so misconfiguration is caught at startup.
    residual = re.findall(r"\$\{[^}]+\}", content)
    if residual:
        unique = sorted(set(residual))
        raise ValueError(
            "Unresolved environment variable(s) in config: "
            f"{', '.join(unique)}. Set the variable(s); note that the "
            "${VAR:-default} syntax is not supported."
        )
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

    # Sharding overrides from environment.
    if settings.collector_sharding is not None:
        app_config.sharding.enabled = settings.collector_sharding
    if settings.max_targets_per_shard > 0:
        app_config.sharding.max_targets_per_shard = settings.max_targets_per_shard
    if settings.collector_balance:
        # Direct assignment bypasses the Literal validator, so guard the value.
        if settings.collector_balance in ("static", "dynamic"):
            app_config.sharding.balance = cast(
                'Literal["static", "dynamic"]', settings.collector_balance
            )
        else:
            logger.warning(
                "Ignoring invalid COLLECTOR_BALANCE=%r (expected 'static' or 'dynamic'); "
                "using config.yaml default %r",
                settings.collector_balance,
                app_config.sharding.balance,
            )

    # UI credentials from environment take precedence over config.yaml. Hashing
    # UI_PASSWORD here lets operators set a real password without committing a
    # bcrypt hash to config.yaml (and avoids the default-password lockout).
    if settings.ui_username:
        app_config.ui.auth.username = settings.ui_username
    if settings.ui_password:
        import bcrypt

        app_config.ui.auth.password_hash = bcrypt.hashpw(
            settings.ui_password.encode(), bcrypt.gensalt()
        ).decode()

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


def get_collector_id() -> str:
    """Explicit identity for this collector process, or the hostname fallback.

    ``COLLECTOR_ID`` if set, else the hostname. NOTE: in dynamic (HRW) sharding
    the id must be STABLE across restarts, not merely unique — the hostname is
    unique per container but changes on recreate, so dynamic mode slot-claims a
    stable ``collector-<ordinal>`` instead (see collector_main._init_sharding).
    This fallback is for static mode or an explicitly-pinned id (e.g. a k8s
    StatefulSet pod name).
    """
    import socket

    return get_settings().collector_id or socket.gethostname()


# Header carrying the internal service-to-service token on the collector's
# control endpoints (e.g. POST /poll). The API and collector containers share
# ENCRYPTION_KEY, so we derive a token from it rather than add new config.
INTERNAL_AUTH_HEADER = "X-Gyanam-Internal"


def internal_service_token() -> str | None:
    """Shared secret for internal API→collector control calls, derived from the
    ENCRYPTION_KEY both processes already share. Returns None if no key is set
    (in which case callers should skip enforcement rather than fail closed)."""
    import hashlib

    key = get_settings().encryption_key
    if not key:
        return None
    return hashlib.sha256(b"gyanam-internal-v1:" + key.encode()).hexdigest()
