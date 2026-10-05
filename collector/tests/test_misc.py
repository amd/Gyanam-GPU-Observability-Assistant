# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Miscellaneous coverage: dependency stubs, base context manager, config getters."""

import pytest
from src.api import dependencies
from src.config import get_config, get_settings, load_config


def test_dependency_service_stubs():
    # Components that live in the collector service raise in the API service.
    with pytest.raises(RuntimeError):
        dependencies.get_poller()
    with pytest.raises(RuntimeError):
        dependencies.get_exporter()
    with pytest.raises(RuntimeError):
        dependencies.get_extractor()
    assert dependencies.get_alert_manager() is None


def test_get_config_and_settings_cached():
    assert get_config() is get_config()
    assert get_settings() is get_settings()


def test_config_alert_env_overrides(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_BASE_URL", "http://collector:8081/redfish-webhook")
    monkeypatch.setenv("ALERT_FORCE_WEBHOOK_MODE", "true")
    app_config, _ = load_config()
    assert app_config.alerts.webhook_base_url == "http://collector:8081/redfish-webhook"
    assert app_config.alerts.force_webhook_mode is True
