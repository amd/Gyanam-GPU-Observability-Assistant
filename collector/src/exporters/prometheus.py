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
"""Prometheus metrics exporter.

Holds the latest metric values in memory as Prometheus Gauges and
serves them via a /metrics endpoint for Prometheus to scrape.
"""

import logging
import re

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from .base import BaseExporter, Metric

logger = logging.getLogger(__name__)

# Prometheus metric name pattern
_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")


def sanitize_metric_name(name: str) -> str:
    """Sanitize a metric name for Prometheus compatibility.

    Prometheus names must match [a-zA-Z_:][a-zA-Z0-9_:]*
    """
    sanitized = _NAME_RE.sub("_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized.lower()


class PrometheusExporter(BaseExporter):
    """Exports metrics via Prometheus exposition format.

    Stores the latest value of each metric as a Gauge. Prometheus
    scrapes the /metrics endpoint to collect current values.
    """

    def __init__(self):
        self._registry = CollectorRegistry()
        self._gauges: dict[str, Gauge] = {}
        self._gauge_label_names: dict[str, tuple[str, ...]] = {}
        self._connected = False
        self._metrics_written = 0
        self._label_mismatch_warned: set[str] = set()

    async def connect(self) -> None:
        """Mark the exporter as ready (no external connection needed)."""
        self._connected = True
        logger.info("Prometheus exporter ready")

    async def close(self) -> None:
        """Shut down the exporter."""
        self._connected = False

    async def write(self, metrics: list[Metric]) -> bool:
        """Update Prometheus Gauges with the latest metric values.

        Each Metric is mapped to a Gauge with its tags as labels.
        """
        if not self._connected:
            return False

        for metric in metrics:
            name = sanitize_metric_name(metric.name)
            label_names = tuple(sorted(metric.tags.keys()))

            if name not in self._gauges:
                # Create new Gauge for this metric
                description = f"{metric.name}"
                if metric.unit:
                    description += f" ({metric.unit})"
                try:
                    self._gauges[name] = Gauge(
                        name,
                        description,
                        labelnames=label_names,
                        registry=self._registry,
                    )
                    self._gauge_label_names[name] = label_names
                except ValueError as e:
                    # Duplicate registration or invalid name
                    logger.warning(f"Cannot create Prometheus gauge '{name}': {e}")
                    continue
            elif self._gauge_label_names[name] != label_names:
                # Label set mismatch — log once and skip
                if name not in self._label_mismatch_warned:
                    logger.warning(
                        f"Prometheus label mismatch for '{name}': "
                        f"expected {self._gauge_label_names[name]}, got {label_names}"
                    )
                    self._label_mismatch_warned.add(name)
                continue

            try:
                self._gauges[name].labels(**metric.tags).set(metric.value)
            except Exception as e:
                logger.debug(f"Error setting gauge {name}: {e}")

        self._metrics_written += len(metrics)
        return True

    async def health_check(self) -> tuple[bool, str]:
        """Check exporter health."""
        if self._connected:
            return True, (
                f"Prometheus exporter active "
                f"({len(self._gauges)} metric families, "
                f"{self._metrics_written} points written)"
            )
        return False, "Prometheus exporter not initialized"

    @property
    def is_connected(self) -> bool:
        """Always ready once initialized (in-process, no external dependency)."""
        return self._connected  # type: ignore[no-any-return]

    @property
    def registry(self) -> CollectorRegistry:
        """Access the Prometheus registry for /metrics endpoint."""
        return self._registry

    def generate_metrics(self) -> bytes:
        """Generate Prometheus exposition format output."""
        return generate_latest(self._registry)  # type: ignore[no-any-return]
