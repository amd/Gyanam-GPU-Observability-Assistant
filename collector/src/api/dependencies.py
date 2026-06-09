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
"""Shared application state and dependency providers for API service.

This module holds the global app state and getter functions used by
route modules in the API service. Note that the API service only manages:
- Target repository (database access)
- Schema loader (for validation)
- Log collector (for on-demand log collection)

Metric collection components (exporter, poller, alert_manager) are in the
separate collector service.
"""

from ..database.repository import TargetRepository
from ..log_collector import LogCollector
from ..parser.schema import SchemaLoader

# Global application state - populated during app lifespan startup
app_state: dict = {}


def _get_required(key: str) -> object:
    """Get a required value from app state, raising if not initialized."""
    value = app_state.get(key)
    if value is None:
        raise RuntimeError(
            f"Application component '{key}' is not initialized. "
            "The application may still be starting up."
        )
    return value


def get_repository() -> TargetRepository:
    """Get the target repository instance."""
    return _get_required("repository")  # type: ignore[return-value]


def get_schema_loader() -> SchemaLoader:
    """Get the schema loader instance."""
    return _get_required("schema_loader")  # type: ignore[return-value]


def get_log_collector() -> LogCollector:
    """Get the log collector instance."""
    return _get_required("log_collector")  # type: ignore[return-value]


def get_alert_manager():
    """Get the alert manager instance (None in API service - runs in collector).

    The alert manager runs in the collector service, not the API service.
    This function returns None in the API to maintain compatibility with
    routes that check for alert manager status.
    """
    return None  # Always None in API service


# The following components run in the collector service, not the API service.
# These stub functions exist only for import compatibility with routes that
# may reference them (like the poll endpoint which is disabled in API-only mode).


def get_poller():
    """Get the poller instance - NOT AVAILABLE in API service."""
    raise RuntimeError(
        "Poller is not available in the API service. "
        "Polling is handled by the separate collector service."
    )


def get_exporter():
    """Get the exporter instance - NOT AVAILABLE in API service."""
    raise RuntimeError(
        "Exporter is not available in the API service. "
        "Metric export is handled by the separate collector service."
    )


def get_unpacker():
    """Get the unpacker instance - NOT AVAILABLE in API service."""
    raise RuntimeError(
        "Unpacker is not available in the API service. "
        "Metric processing is handled by the separate collector service."
    )


def get_extractor():
    """Get the extractor instance - NOT AVAILABLE in API service."""
    raise RuntimeError(
        "Extractor is not available in the API service. "
        "Metric extraction is handled by the separate collector service."
    )


def get_discovery():
    """Get the discovery instance - NOT AVAILABLE in API service."""
    raise RuntimeError(
        "Discovery is not available in the API service. "
        "Metric discovery is handled by the separate collector service."
    )
