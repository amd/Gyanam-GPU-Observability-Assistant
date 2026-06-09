# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the schema loader against the real metrics_schema.yaml."""

from src.parser.schema import MetricSchema, SchemaLoader


def test_loads_real_schema(schema_loader):
    schemas = schema_loader.get_schemas()
    # The project ships 33 metric schemas.
    assert len(schemas) >= 30
    assert all(isinstance(s, MetricSchema) for s in schemas)
    assert all(s.path_pattern for s in schemas)


def test_get_schema_by_name(schema_loader):
    first = schema_loader.get_schemas()[0]
    found = schema_loader.get_schema_by_name(first.name)
    assert found is not None and found.name == first.name
    assert schema_loader.get_schema_by_name("does-not-exist") is None


def test_auto_discovery_config_present(schema_loader):
    cfg = schema_loader.get_auto_discovery_config()
    assert cfg is not None
    assert isinstance(cfg.include_patterns, list)
    assert isinstance(cfg.exclude_patterns, list)


def test_missing_file_falls_back_to_defaults():
    loader = SchemaLoader(schema_path="/nonexistent/schema.yaml")
    cfg = loader.load()
    # Defaults are returned rather than raising.
    assert cfg is not None
    assert cfg.auto_discovery is not None
