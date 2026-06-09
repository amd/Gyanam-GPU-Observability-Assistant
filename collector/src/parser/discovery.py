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
"""Auto-discovery of metrics from JSON data."""

import fnmatch
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .schema import AutoDiscoveryConfig, SchemaLoader

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredMetric:
    """A metric discovered through auto-discovery."""

    name: str
    value: float
    path: str  # JSON path where it was found
    timestamp: datetime
    metric_type: str
    tags: dict[str, str]
    unit: str = ""


class MetricDiscovery:
    """Auto-discovers metrics from JSON data based on patterns.

    Recursively walks JSON structures to find numeric values
    matching configured include/exclude patterns.
    """

    def __init__(self, schema_loader: SchemaLoader, max_recursion_depth: int = 50):
        """Initialize the discovery engine.

        Args:
            schema_loader: Schema loader for auto-discovery configuration
            max_recursion_depth: Fallback depth for JSON traversal
                (overridden by auto_discovery.max_depth from schema config)
        """
        self.schema_loader = schema_loader
        self._fallback_max_depth = max_recursion_depth

    def discover(
        self,
        data: dict[str, Any],
        host: str,
        extra_tags: dict[str, str] | None = None,
        exclude_keys: set[str] | None = None,
        timestamp: datetime | None = None,
    ) -> list[DiscoveredMetric]:
        """Discover metrics from JSON data.

        Args:
            data: Parsed JSON data to analyze
            host: Host name/IP to add as tag
            extra_tags: Additional tags to add
            exclude_keys: JSON keys already extracted by schemas (to avoid duplicates)
            timestamp: Optional timestamp to use for all metrics (defaults to now)

        Returns:
            List of discovered metrics
        """
        config = self.schema_loader.get_auto_discovery_config()

        if not config.enabled:
            return []

        discovered: list[DiscoveredMetric] = []
        if timestamp is None:
            timestamp = datetime.now(UTC)

        # Use max_depth from auto_discovery config, fall back to constructor arg
        max_depth = config.max_depth if config.max_depth > 0 else self._fallback_max_depth

        base_tags = {"host": host}
        if extra_tags:
            base_tags.update(extra_tags)

        # Recursively walk the JSON structure
        self._walk_json(
            data=data,
            path="$",
            config=config,
            timestamp=timestamp,
            base_tags=base_tags,
            discovered=discovered,
            exclude_keys=frozenset(exclude_keys) if exclude_keys else None,
            max_depth=max_depth,
        )

        logger.debug(f"Auto-discovered {len(discovered)} metrics")
        return discovered

    def _walk_json(
        self,
        data: Any,
        path: str,
        config: AutoDiscoveryConfig,
        timestamp: datetime,
        base_tags: dict[str, str],
        discovered: list[DiscoveredMetric],
        depth: int = 0,
        exclude_keys: frozenset[str] | None = None,
        max_depth: int = 50,
    ) -> None:
        """Recursively walk JSON structure to find metrics.

        Args:
            data: Current data node
            path: Current JSON path
            config: Auto-discovery configuration
            timestamp: Timestamp for discovered metrics
            base_tags: Base tags to include
            discovered: List to append discovered metrics to
            depth: Current recursion depth
            exclude_keys: JSON keys already extracted by schemas (skip these)
            max_depth: Maximum recursion depth
        """
        if exclude_keys is None:
            exclude_keys = frozenset()
        # Prevent infinite recursion
        if depth > max_depth:
            return

        if isinstance(data, dict):
            # Collect potential tags from this level
            level_tags = self._extract_tags(data, path)

            for key, value in data.items():
                child_path = f"{path}.{key}"

                if isinstance(value, dict | list):
                    # Recurse into nested structures
                    merged_tags = {**base_tags, **level_tags}
                    self._walk_json(
                        value,
                        child_path,
                        config,
                        timestamp,
                        merged_tags,
                        discovered,
                        depth + 1,
                        exclude_keys=exclude_keys,
                        max_depth=max_depth,
                    )
                elif self._is_numeric(value):
                    # Skip keys already extracted by schemas
                    if key in exclude_keys:
                        continue
                    # Check if this key matches our patterns
                    if self._should_include(key, config):
                        metric_name = self._key_to_metric_name(key, path)
                        numeric_value = self._to_numeric(value)

                        if numeric_value is not None:
                            merged_tags = {**base_tags, **level_tags}
                            discovered.append(
                                DiscoveredMetric(
                                    name=metric_name,
                                    value=numeric_value,
                                    path=child_path,
                                    timestamp=timestamp,
                                    metric_type=config.default_type,
                                    tags=merged_tags,
                                )
                            )

        elif isinstance(data, list):
            for idx, item in enumerate(data):
                child_path = f"{path}[{idx}]"
                self._walk_json(
                    item,
                    child_path,
                    config,
                    timestamp,
                    base_tags,
                    discovered,
                    depth + 1,
                    exclude_keys=exclude_keys,
                    max_depth=max_depth,
                )

    def _should_include(self, key: str, config: AutoDiscoveryConfig) -> bool:
        """Check if a key should be included based on patterns.

        Args:
            key: JSON key to check
            config: Auto-discovery configuration

        Returns:
            True if the key should be included
        """
        # Check exclude patterns first
        for pattern in config.exclude_patterns:
            if fnmatch.fnmatch(key, pattern):
                return False

        # Check include patterns
        return any(fnmatch.fnmatch(key, pattern) for pattern in config.include_patterns)

    def _is_numeric(self, value: Any) -> bool:
        """Check if a value can be converted to numeric."""
        if isinstance(value, int | float | bool):
            return True

        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                # Check for common boolean-like strings
                return value.lower() in (
                    "true",
                    "false",
                    "yes",
                    "no",
                    "on",
                    "off",
                    "enabled",
                    "disabled",
                    "ok",
                    "error",
                    "healthy",
                    "unhealthy",
                )

        return False

    def _to_numeric(self, value: Any) -> float | None:
        """Convert a value to numeric."""
        if isinstance(value, bool):
            return 1.0 if value else 0.0

        if isinstance(value, int | float):
            return float(value)

        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                lower_val = value.lower()
                if lower_val in ("true", "yes", "on", "enabled", "ok", "healthy"):
                    return 1.0
                if lower_val in ("false", "no", "off", "disabled", "error", "unhealthy"):
                    return 0.0

        return None

    def _key_to_metric_name(self, key: str, path: str) -> str:
        """Convert a JSON key to a metric name.

        Args:
            key: JSON key
            path: Full JSON path for context

        Returns:
            Sanitized metric name
        """
        # Convert camelCase to snake_case
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()

        # Remove non-alphanumeric characters except underscore
        name = re.sub(r"[^a-z0-9_]", "_", name)

        # Remove consecutive underscores
        name = re.sub(r"_+", "_", name)

        # Remove leading/trailing underscores
        name = name.strip("_")

        # Add prefix based on path context
        path_parts = path.split(".")
        if len(path_parts) > 1:
            # Try to extract a meaningful prefix from the path
            for part in reversed(path_parts[:-1]):
                part = part.lower()
                part = re.sub(r"\[\d+\]", "", part)  # Remove array indices
                part = re.sub(r"[^a-z0-9]", "", part)
                if part and part not in ("telemetry", "data", "values", "items"):
                    name = f"discovered_{part}_{name}"
                    break
            else:
                name = f"discovered_{name}"
        else:
            name = f"discovered_{name}"

        return name

    def _extract_tags(self, data: dict[str, Any], path: str) -> dict[str, str]:
        """Extract potential tags from a dictionary.

        Looks for common identifier fields to use as tags.
        """
        tags = {}

        tag_candidates = [
            ("id", "id"),
            ("ID", "id"),
            ("Id", "id"),
            ("name", "name"),
            ("Name", "name"),
            ("GPUID", "gpu_id"),
            ("GpuId", "gpu_id"),
            ("gpu_id", "gpu_id"),
            ("DeviceId", "device_id"),
            ("device_id", "device_id"),
            ("Index", "index"),
            ("index", "index"),
        ]

        for json_key, tag_name in tag_candidates:
            if json_key in data:
                value = data[json_key]
                if value is not None and not isinstance(value, dict | list):
                    tags[tag_name] = str(value)

        return tags

    def analyze_structure(self, data: dict[str, Any], max_depth: int = 10) -> dict[str, Any]:
        """Analyze JSON structure to help configure schemas.

        Args:
            data: JSON data to analyze
            max_depth: Maximum recursion depth

        Returns:
            Structure analysis with paths and value types
        """
        analysis: dict[str, Any] = {
            "paths": [],
            "numeric_fields": [],
            "array_paths": [],
            "potential_tags": [],
        }

        self._analyze_node(data, "$", analysis, 0, max_depth)

        return analysis

    def _analyze_node(
        self, data: Any, path: str, analysis: dict[str, Any], depth: int, max_depth: int
    ) -> None:
        """Recursively analyze a node in the JSON structure."""
        if depth > max_depth:
            return

        if isinstance(data, dict):
            analysis["paths"].append(path)

            # Check for potential tag fields
            for key in data:
                if key.lower() in ("id", "name", "index", "gpuid", "deviceid"):
                    analysis["potential_tags"].append(f"{path}.{key}")

            for key, value in data.items():
                child_path = f"{path}.{key}"

                if isinstance(value, int | float):
                    analysis["numeric_fields"].append(
                        {"path": child_path, "key": key, "sample_value": value}
                    )
                elif isinstance(value, dict | list):
                    self._analyze_node(value, child_path, analysis, depth + 1, max_depth)

        elif isinstance(data, list):
            if len(data) > 0:
                analysis["array_paths"].append(path)
                # Analyze first element as representative
                self._analyze_node(data[0], f"{path}[*]", analysis, depth + 1, max_depth)
