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
"""Metric extractor using JSONPath-based schema definitions."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jsonpath_ng.exceptions import JsonPathParserError
from jsonpath_ng.ext import parse as jsonpath_parse

from .redfish_log_parser import RedfishLogParser
from .schema import MetricSchema, SchemaLoader

logger = logging.getLogger(__name__)


@dataclass
class ExtractedMetric:
    """A single extracted metric value."""

    name: str
    value: float
    timestamp: datetime
    metric_type: str  # gauge, counter
    unit: str
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "value": self.value,
            "timestamp": self.timestamp.isoformat(),
            "type": self.metric_type,
            "unit": self.unit,
            "tags": self.tags,
        }


class MetricExtractor:
    """Extracts metrics from JSON files using schema definitions.

    Uses JSONPath patterns to locate metric values in complex JSON
    structures and extracts them according to the schema configuration.
    """

    def __init__(self, schema_loader: SchemaLoader):
        """Initialize the extractor.

        Args:
            schema_loader: Schema loader for metric definitions
        """
        self.schema_loader = schema_loader
        self._compiled_patterns: dict[str, Any] = {}
        self.redfish_log_parser = RedfishLogParser()

    def extract_from_data(
        self,
        data: dict[str, Any],
        host: str,
        extra_tags: dict[str, str] | None = None,
        timestamp: datetime | None = None,
    ) -> list[ExtractedMetric]:
        """Extract metrics from parsed JSON data.

        Args:
            data: Parsed JSON data
            host: Host name/IP to add as tag
            extra_tags: Additional tags to add to all metrics
            timestamp: Optional timestamp to use for all metrics (defaults to now)

        Returns:
            List of extracted metrics
        """
        metrics: list[ExtractedMetric] = []
        if timestamp is None:
            timestamp = datetime.now(UTC)

        # Base tags applied to all metrics
        base_tags = {"host": host}
        if extra_tags:
            base_tags.update(extra_tags)

        # Extract using defined schemas
        schemas = self.schema_loader.get_schemas()
        for schema in schemas:
            schema_metrics = self._extract_with_schema(data, schema, timestamp, base_tags)
            metrics.extend(schema_metrics)

        logger.debug(f"Extracted {len(metrics)} metrics using schemas")
        return metrics

    def _extract_with_schema(
        self,
        data: dict[str, Any],
        schema: MetricSchema,
        timestamp: datetime,
        base_tags: dict[str, str],
    ) -> list[ExtractedMetric]:
        """Extract metrics using a specific schema.

        Args:
            data: JSON data to extract from
            schema: Schema to use for extraction
            timestamp: Timestamp to apply to metrics
            base_tags: Base tags to include

        Returns:
            List of extracted metrics
        """
        metrics: list[ExtractedMetric] = []

        # Compile and cache the JSONPath pattern
        pattern = self._get_compiled_pattern(schema.path_pattern)
        if pattern is None:
            return metrics

        # Find all matches for the path pattern
        matches = pattern.find(data)

        for match in matches:
            match_data = match.value

            if not isinstance(match_data, dict):
                continue

            # Extract tags from this match
            match_tags = base_tags.copy()
            for tag_def in schema.tags_from:
                if tag_def.json_key in match_data:
                    tag_value = match_data[tag_def.json_key]
                    if tag_value is not None:
                        match_tags[tag_def.tag_name] = str(tag_value)

            # Extract field values
            for field_def in schema.fields:
                if field_def.json_key not in match_data:
                    continue

                raw_value = match_data[field_def.json_key]
                numeric_value = self._to_numeric(raw_value)

                if numeric_value is not None:
                    metrics.append(
                        ExtractedMetric(
                            name=field_def.metric_name,
                            value=numeric_value,
                            timestamp=timestamp,
                            metric_type=field_def.type,
                            unit=field_def.unit,
                            tags=match_tags.copy(),
                        )
                    )

        return metrics

    def _get_compiled_pattern(self, pattern: str) -> Any | None:
        """Get a compiled JSONPath pattern, caching for reuse."""
        if pattern in self._compiled_patterns:
            return self._compiled_patterns[pattern]

        try:
            compiled = jsonpath_parse(pattern)
            self._compiled_patterns[pattern] = compiled
            return compiled
        except JsonPathParserError as e:
            logger.error(f"Invalid JSONPath pattern '{pattern}': {e}")
            self._compiled_patterns[pattern] = None
            return None

    def _to_numeric(self, value: Any) -> float | None:
        """Convert a value to numeric, returning None if not possible."""
        if value is None:
            return None

        if isinstance(value, bool):
            return 1.0 if value else 0.0

        if isinstance(value, int | float):
            return float(value)

        if isinstance(value, str):
            # Try to parse numeric string
            try:
                return float(value)
            except ValueError:
                pass

            # Handle common string representations
            lower_val = value.lower()
            if lower_val in ("true", "yes", "on", "enabled", "ok", "healthy"):
                return 1.0
            if lower_val in ("false", "no", "off", "disabled", "error", "unhealthy"):
                return 0.0

        return None
