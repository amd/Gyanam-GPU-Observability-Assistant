# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Shared pytest fixtures for the collector/api test suite."""

import os
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport
from src.database.repository import TargetRepository
from src.parser.schema import SchemaLoader

# Schema file location (config is mounted at /app/config in the test runner;
# fall back to the repo-relative path for local runs).
_SCHEMA_CANDIDATES = [
    "/app/config/metrics_schema.yaml",
    os.path.join(os.path.dirname(__file__), "..", "config", "metrics_schema.yaml"),
]


def _schema_path() -> str:
    for p in _SCHEMA_CANDIDATES:
        if os.path.exists(p):
            return p
    return _SCHEMA_CANDIDATES[0]


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
async def repo(tmp_path, encryption_key):
    """A TargetRepository backed by throwaway SQLite (targets + alerts)."""
    repository = TargetRepository(
        database_url=f"sqlite:///{tmp_path}/targets.db",
        encryption_key=encryption_key,
        alerts_database_url=f"sqlite:///{tmp_path}/alerts.db",
    )
    await repository.init_db()
    try:
        yield repository
    finally:
        await repository.close()


@pytest.fixture(scope="session")
def schema_loader() -> SchemaLoader:
    """SchemaLoader bound to the real metrics_schema.yaml."""
    loader = SchemaLoader(schema_path=_schema_path())
    loader.load()
    return loader


@pytest.fixture
async def app(repo, schema_loader):
    """A FastAPI app wired to the test repository (lifespan not run)."""
    from src import api_main
    from src.api import dependencies

    application = api_main.create_app()
    dependencies.app_state["repository"] = repo
    dependencies.app_state["schema_loader"] = schema_loader
    dependencies.app_state["log_collector"] = MagicMock()
    try:
        yield application
    finally:
        dependencies.app_state.clear()


@pytest.fixture
async def noauth_client(app):
    """HTTP client with NO auth override (for auth-enforcement tests)."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def client(app):
    """HTTP client authenticated as 'test' (get_current_user overridden)."""
    from src.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: "test"
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sample_metric_report() -> dict:
    """A minimal Redfish MetricReport-shaped payload for extractor/discovery."""
    return {
        "@odata.type": "#MetricReport.v1_4_2.MetricReport",
        "Id": "AllMetrics",
        "MetricValues": [
            {"MetricProperty": "/redfish/v1/Chassis/1#GPU_TEMP", "MetricValue": "42.5"},
            {"MetricProperty": "/redfish/v1/Chassis/1#GPU_POWER", "MetricValue": "310"},
            {"MetricProperty": "/redfish/v1/Chassis/1#GPU_HEALTH", "MetricValue": "OK"},
        ],
    }
