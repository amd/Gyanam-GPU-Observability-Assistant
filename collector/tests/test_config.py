# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for configuration loading and env overrides."""

from src.config import AppConfig, load_config, load_yaml_config


def test_alerts_defaults():
    cfg = AppConfig()
    assert cfg.alerts.enabled is True
    assert cfg.alerts.severities == ["Critical", "Warning"]
    assert cfg.alerts.baseline_pull_enabled is True
    assert cfg.alerts.baseline_repull_interval_minutes == 60


def test_load_yaml_config_returns_dict():
    # /app/config/config.yaml is mounted in the test runner.
    data = load_yaml_config("/app/config/config.yaml")
    assert isinstance(data, dict)
    assert "polling" in data


def test_load_config_env_override_influxdb(monkeypatch):
    monkeypatch.setenv("INFLUXDB_URL", "http://example:8086")
    monkeypatch.setenv("INFLUXDB_BUCKET", "custom_bucket")
    app_config, settings = load_config()
    assert app_config.influxdb.url == "http://example:8086"
    assert app_config.influxdb.bucket == "custom_bucket"


def test_settings_reads_alerts_database_url(monkeypatch):
    monkeypatch.setenv("ALERTS_DATABASE_URL", "postgresql+asyncpg://u:p@pg/db")
    _app, settings = load_config()
    assert settings.alerts_database_url == "postgresql+asyncpg://u:p@pg/db"


def test_batch_size_zero_env_keeps_yaml_default(monkeypatch):
    # 0 means "use config.yaml default" — override must NOT apply.
    monkeypatch.setenv("INFLUXDB_BATCH_SIZE", "0")
    app_config, _ = load_config()
    assert app_config.influxdb.batch_size > 0
