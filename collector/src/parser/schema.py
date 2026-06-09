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
"""Schema loader and validator for metric extraction rules."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class FieldDefinition:
    """Definition of a metric field to extract."""

    json_key: str
    metric_name: str
    type: str = "gauge"  # gauge, counter
    unit: str = ""


@dataclass
class TagDefinition:
    """Definition of a tag to extract."""

    json_key: str
    tag_name: str


@dataclass
class MetricSchema:
    """Schema for extracting metrics from a JSON path."""

    name: str
    description: str
    path_pattern: str  # JSONPath pattern
    fields: list[FieldDefinition] = field(default_factory=list)
    tags_from: list[TagDefinition] = field(default_factory=list)


@dataclass
class AutoDiscoveryConfig:
    """Configuration for automatic metric discovery."""

    enabled: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    default_type: str = "gauge"
    max_depth: int = 50


@dataclass
class SchemaConfig:
    """Complete schema configuration."""

    schemas: list[MetricSchema] = field(default_factory=list)
    auto_discovery: AutoDiscoveryConfig = field(default_factory=AutoDiscoveryConfig)


class SchemaLoader:
    """Loads and validates metric extraction schemas from YAML."""

    def __init__(self, schema_path: str | None = None):
        """Initialize the schema loader.

        Args:
            schema_path: Path to the metrics schema YAML file
        """
        self.schema_path = Path(schema_path) if schema_path else None
        self._config: SchemaConfig | None = None

    def load(self) -> SchemaConfig:
        """Load the schema configuration.

        Returns:
            SchemaConfig object

        Raises:
            FileNotFoundError: If schema file doesn't exist
            ValueError: If schema is invalid
        """
        if self._config is not None:
            return self._config

        if not self.schema_path or not self.schema_path.exists():
            logger.warning(f"Schema file not found: {self.schema_path}, using defaults")
            self._config = self._get_default_config()
            return self._config

        try:
            with open(self.schema_path) as f:
                raw_config = yaml.safe_load(f)

            self._config = self._parse_config(raw_config)
            logger.info(f"Loaded {len(self._config.schemas)} metric schemas")
            return self._config

        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in schema file: {e}")
        except Exception as e:
            raise ValueError(f"Failed to load schema: {e}")

    def reload(self) -> SchemaConfig:
        """Reload the schema configuration from disk."""
        self._config = None
        return self.load()

    def _parse_config(self, raw: dict[str, Any]) -> SchemaConfig:
        """Parse raw YAML config into SchemaConfig object."""
        schemas = []

        for schema_def in raw.get("schemas", []):
            fields = [
                FieldDefinition(
                    json_key=f["json_key"],
                    metric_name=f["metric_name"],
                    type=f.get("type", "gauge"),
                    unit=f.get("unit", ""),
                )
                for f in schema_def.get("fields", [])
            ]

            tags = [
                TagDefinition(json_key=t["json_key"], tag_name=t["tag_name"])
                for t in schema_def.get("tags_from", [])
            ]

            schemas.append(
                MetricSchema(
                    name=schema_def["name"],
                    description=schema_def.get("description", ""),
                    path_pattern=schema_def["path_pattern"],
                    fields=fields,
                    tags_from=tags,
                )
            )

        auto_discovery_raw = raw.get("auto_discovery", {})
        auto_discovery = AutoDiscoveryConfig(
            enabled=auto_discovery_raw.get("enabled", True),
            include_patterns=auto_discovery_raw.get("include_patterns", []),
            exclude_patterns=auto_discovery_raw.get("exclude_patterns", []),
            default_type=auto_discovery_raw.get("default_type", "gauge"),
            max_depth=auto_discovery_raw.get("max_depth", 50),
        )

        return SchemaConfig(schemas=schemas, auto_discovery=auto_discovery)

    def _get_default_config(self) -> SchemaConfig:
        """Get default schema configuration."""
        return SchemaConfig(
            schemas=[],
            auto_discovery=AutoDiscoveryConfig(
                enabled=True,
                include_patterns=[
                    "*Temp*",
                    "*Temperature*",
                    "*Watts*",
                    "*Power*",
                    "*Percent*",
                    "*Util*",
                    "*Bytes*",
                    "*MHz*",
                    "*Clock*",
                    "*Count*",
                    "*Rate*",
                ],
                exclude_patterns=[
                    "*Timestamp*",
                    "*Version*",
                    "*Serial*",
                    "*UUID*",
                    "*Name*",
                    "*ID*",
                    "*Description*",
                ],
                default_type="gauge",
            ),
        )

    def get_schemas(self) -> list[MetricSchema]:
        """Get all defined metric schemas."""
        config = self.load()
        return config.schemas

    def get_auto_discovery_config(self) -> AutoDiscoveryConfig:
        """Get auto-discovery configuration."""
        config = self.load()
        return config.auto_discovery

    def get_schema_by_name(self, name: str) -> MetricSchema | None:
        """Get a specific schema by name."""
        config = self.load()
        for schema in config.schemas:
            if schema.name == name:
                return schema
        return None
