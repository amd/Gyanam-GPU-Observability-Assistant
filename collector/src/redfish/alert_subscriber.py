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
"""SSE alert subscriber for a single Redfish target."""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import httpx

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Classification of SSE connection errors."""

    PERMANENT = "permanent"  # Don't retry (auth failed, not supported, etc.)
    TRANSIENT = "transient"  # Retry with backoff (network issues, timeouts)


class SubscriptionState(Enum):
    """Enhanced subscription states for better monitoring."""

    CONNECTED = "connected"  # Active SSE stream, receiving events
    RECONNECTING = "reconnecting"  # Recent failure (< degraded threshold), actively retrying
    DEGRADED = "degraded"  # Failing for > degraded_threshold, retrying with longer delays
    ON_COOLDOWN = "on_cooldown"  # Exceeded max retry duration, in cooldown before auto-resume
    FAILED_PERMANENT = "failed_permanent"  # Permanent error (auth, not supported)
    STOPPED = "stopped"  # Manually stopped by user


@dataclass
class AlertEvent:
    """Parsed alert event from Redfish SSE."""

    target_id: int
    target_name: str
    target_bmc: str
    severity: str  # Critical, Warning, OK
    message: str
    message_id: str | None
    event_type: str  # Alert, StatusChange, etc.
    origin_of_condition: str | None
    event_timestamp: datetime | None
    received_at: datetime


# Callback type for alert processing
AlertCallback = Callable[[AlertEvent], None]


class AlertSubscriber:
    """Manages SSE alert subscription for a single target.

    Optimized for long-running connections with automatic reconnection.
    Designed to be lightweight so 300+ instances can run concurrently.

    Note: Each subscriber maintains its own httpx.AsyncClient for a dedicated
    long-lived SSE connection. Connection pooling is not used because SSE
    requires persistent streaming connections that last hours/days.
    """

    def __init__(
        self,
        target_id: int,
        target_name: str,
        target_bmc: str,
        base_url: str,
        username: str,
        password: str,
        sse_endpoint: str = "/redfish/v1/EventService/SSE",
        verify_ssl: bool = False,
        callback: AlertCallback | None = None,
        reconnect_delay: int = 30,
        max_retry_duration_hours: float = 24,
        cooldown_duration_hours: float = 6,
        degraded_threshold_hours: float = 1,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
    ):
        """Initialize alert subscriber.

        Args:
            target_id: Target database ID
            target_name: Target display name
            target_bmc: Target BMC address
            base_url: Redfish API base URL
            username: Basic auth username
            password: Basic auth password
            sse_endpoint: SSE endpoint path
            verify_ssl: Whether to verify SSL certificates
            callback: Optional callback for processing alert events
            reconnect_delay: Seconds to wait before reconnecting on error
            max_retry_duration_hours: Hours to retry before entering circuit breaker
            cooldown_duration_hours: Hours in cooldown before auto-resume
            degraded_threshold_hours: Hours of failures to mark as degraded
            event_types: Filter events by type (default: Alert, StatusChange)
            severities: Filter by severity (default: Warning, Critical)
        """
        self.target_id = target_id
        self.target_name = target_name
        self.target_bmc = target_bmc
        self.base_url = base_url
        self.username = username
        self.password = password
        self.sse_endpoint = sse_endpoint
        self.verify_ssl = verify_ssl
        self.callback = callback
        self.reconnect_delay = reconnect_delay
        self.max_retry_duration_hours = max_retry_duration_hours
        self.cooldown_duration_hours = cooldown_duration_hours
        self.degraded_threshold_hours = degraded_threshold_hours
        self.event_types = event_types or ["Alert", "StatusChange"]
        self.severities = severities or ["Warning", "Critical"]

        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_event_time: datetime | None = None
        self._first_failure_time: datetime | None = None
        self._cooldown_start_time: datetime | None = None
        self._state = SubscriptionState.STOPPED
        self._failure_reason: str | None = None
        self._next_retry_time: datetime | None = None

    async def start(self) -> None:
        """Start the SSE subscription."""
        if self._running:
            logger.warning(f"Alert subscriber for {self.target_name} is already running")
            return

        self._running = True
        self._state = SubscriptionState.RECONNECTING
        self._task = asyncio.create_task(self._subscribe_loop())
        logger.info(f"Started alert subscription for {self.target_name}")

    async def stop(self) -> None:
        """Stop the SSE subscription."""
        self._running = False
        self._state = SubscriptionState.STOPPED
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        logger.info(f"Stopped alert subscription for {self.target_name}")

    async def resume(self) -> None:
        """Manually resume a failed/circuit_open subscription."""
        logger.info(f"Manually resuming subscription for {self.target_name}")
        # Reset failure tracking
        self._consecutive_failures = 0
        self._first_failure_time = None
        self._cooldown_start_time = None
        self._failure_reason = None
        self._next_retry_time = None

        # Restart if not already running
        if not self._running:
            await self.start()
        else:
            # Already running, just reset state to reconnecting
            self._state = SubscriptionState.RECONNECTING

    async def _subscribe_loop(self) -> None:
        """Main subscription loop with auto-reconnect and circuit breaker."""
        while self._running:
            try:
                # Check if in cooldown period
                if self._cooldown_start_time:
                    cooldown_elapsed = (
                        datetime.now(UTC) - self._cooldown_start_time
                    ).total_seconds() / 3600
                    if cooldown_elapsed < self.cooldown_duration_hours:
                        # Still in cooldown
                        self._state = SubscriptionState.ON_COOLDOWN
                        remaining = self.cooldown_duration_hours - cooldown_elapsed
                        logger.debug(
                            f"Subscription for {self.target_name} on cooldown, "
                            f"{remaining:.1f}h remaining"
                        )
                        await asyncio.sleep(60)  # Check every minute
                        continue
                    else:
                        # Cooldown expired, attempt auto-resume
                        logger.info(
                            f"Cooldown expired for {self.target_name}, attempting auto-resume"
                        )
                        self._cooldown_start_time = None
                        self._first_failure_time = None
                        self._consecutive_failures = 0
                        self._state = SubscriptionState.RECONNECTING

                # Attempt connection
                await self._connect_and_listen()

                # If we reach here, connection ended normally (stopped by user)
                # State was already set to CONNECTED inside _connect_and_listen()

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Classify error
                error_category, error_reason = self._classify_error(e)

                # Track first failure time
                if self._consecutive_failures == 0:
                    self._first_failure_time = datetime.now(UTC)

                self._consecutive_failures += 1
                self._failure_reason = error_reason

                # Log based on error category and verbosity needs
                if error_category == ErrorCategory.PERMANENT:
                    # For permanent errors, log once at WARNING (not ERROR)
                    # Include exception type for debugging
                    if self._consecutive_failures == 1:
                        logger.warning(
                            f"Permanent error for {self.target_name}: {error_reason} "
                            f"[{type(e).__name__}]. Stopping subscription. Manual intervention required."
                        )
                    self._state = SubscriptionState.FAILED_PERMANENT
                    self._running = False
                    break

                else:  # TRANSIENT
                    # For transient errors, log with context but reduce frequency
                    # Log every attempt for first 5, then every 5th attempt
                    if self._consecutive_failures <= 5 or self._consecutive_failures % 5 == 0:
                        logger.error(
                            f"Alert subscription error for {self.target_name} "
                            f"(failure #{self._consecutive_failures}, {error_category.value}): "
                            f"{type(e).__name__}: {error_reason}"
                        )
                        # Include traceback for first 3 failures at ERROR level (not DEBUG)
                        if self._consecutive_failures <= 3:
                            logger.error("Subscription error details:", exc_info=True)

                # Check if exceeded max retry duration
                if self._first_failure_time:
                    elapsed_hours = (
                        datetime.now(UTC) - self._first_failure_time
                    ).total_seconds() / 3600
                    if elapsed_hours >= self.max_retry_duration_hours:
                        logger.warning(
                            f"Max retry duration ({self.max_retry_duration_hours}h) exceeded "
                            f"for {self.target_name}. Entering cooldown for "
                            f"{self.cooldown_duration_hours}h."
                        )
                        self._cooldown_start_time = datetime.now(UTC)
                        self._state = SubscriptionState.ON_COOLDOWN
                        continue

                    # Update state based on failure duration
                    if elapsed_hours >= self.degraded_threshold_hours:
                        self._state = SubscriptionState.DEGRADED
                    else:
                        self._state = SubscriptionState.RECONNECTING

                # Use exponential backoff schedule
                backoff_delay = self._calculate_backoff_delay()
                from datetime import timedelta

                self._next_retry_time = datetime.now(UTC) + timedelta(seconds=backoff_delay)
                logger.info(
                    f"Reconnecting to {self.target_name} in {backoff_delay}s "
                    f"(failure #{self._consecutive_failures}, backoff schedule)"
                )
                await asyncio.sleep(backoff_delay)

    async def _connect_and_listen(self) -> None:
        """Connect to SSE endpoint and process events."""
        url = f"{self.base_url}{self.sse_endpoint}"
        auth = httpx.BasicAuth(self.username, self.password)

        # Use short timeout for connection, no timeout for reading stream
        timeout = httpx.Timeout(30.0, read=None)

        async with (
            httpx.AsyncClient(
                auth=auth, verify=self.verify_ssl, timeout=timeout, follow_redirects=True
            ) as client,
            client.stream("GET", url) as response,
        ):
            # Check status code but don't access response.text in streaming mode
            if response.status_code != 200:
                # Read a small amount of error response for debugging
                error_preview = ""
                try:
                    error_bytes = await response.aread()
                    error_preview = error_bytes[:200].decode("utf-8", errors="ignore")
                except Exception:
                    # Best-effort preview only — if reading the body fails
                    # we still want to raise the underlying status-code error.
                    pass
                raise RuntimeError(
                    f"SSE connection failed: HTTP {response.status_code} {error_preview}"
                )

            logger.info(f"SSE alert stream connected for {self.target_name}")

            # Connection successful - update state immediately
            self._consecutive_failures = 0
            self._first_failure_time = None
            self._failure_reason = None
            self._next_retry_time = None
            self._state = SubscriptionState.CONNECTED

            # Track connection time and events to detect bad endpoints
            from datetime import UTC, datetime

            connect_time = datetime.now(UTC)
            event_count = 0

            async for line in response.aiter_lines():
                if not self._running:
                    break

                line = line.strip()
                if not line:
                    continue  # Empty line

                if line.startswith(":"):
                    # SSE keep-alive comment - reset timeout timer
                    connect_time = datetime.now(UTC)
                    continue

                # SSE format: "data: {...}"
                if line.startswith("data:"):
                    event_count += 1
                    data_str = line[5:].strip()
                    try:
                        self._process_event_data(data_str)
                    except Exception as e:
                        logger.warning(
                            f"Failed to process SSE event from {self.target_name}: "
                            f"{type(e).__name__}: {e}",
                            exc_info=True,
                        )

            # If stream closed within 30 seconds without sending events, treat as error
            # This detects BMCs that redirect to invalid endpoints (like /timeout.asp)
            # Increased threshold to 30s to account for keep-alive comments
            elapsed = (datetime.now(UTC) - connect_time).total_seconds()
            if elapsed < 30.0 and event_count == 0:
                raise RuntimeError(
                    f"SSE stream closed after {elapsed:.1f}s without sending events "
                    f"(possible invalid endpoint or BMC doesn't support SSE)"
                )

    def _process_event_data(self, data_str: str) -> None:
        """Parse and process SSE event data.

        Redfish SSE events have format:
        {
          "@odata.type": "#Event.v1_7_0.Event",
          "Events": [{
            "EventType": "Alert",
            "Severity": "Critical",
            "Message": "Temperature exceeded critical threshold",
            "MessageId": "Thermal.1.0.OverTemperature",
            "OriginOfCondition": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
            "EventTimestamp": "2026-04-22T10:30:00Z"
          }]
        }
        """
        data = json.loads(data_str)
        events = data.get("Events", [])

        for event in events:
            event_type = event.get("EventType", "")
            severity = event.get("Severity", "")

            # Filter by event type and severity
            if event_type not in self.event_types:
                continue
            if severity not in self.severities:
                continue

            # Parse event timestamp - handle multiple formats
            event_ts_str = event.get("EventTimestamp")
            event_ts = None
            if event_ts_str:
                try:
                    # Try ISO format with Z suffix (most common)
                    if event_ts_str.endswith("Z"):
                        event_ts = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
                    else:
                        # Try parsing as-is (handles +00:00, -07:00, etc.)
                        event_ts = datetime.fromisoformat(event_ts_str)

                    # Ensure timezone-aware (convert naive to UTC if needed)
                    if event_ts and event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=UTC)
                except (ValueError, AttributeError) as e:
                    logger.debug(f"Failed to parse EventTimestamp '{event_ts_str}': {e}")

            # Extract origin of condition URI
            origin = event.get("OriginOfCondition", {})
            origin_uri = None
            if isinstance(origin, dict):
                origin_uri = origin.get("@odata.id")
            elif isinstance(origin, str):
                origin_uri = origin

            alert = AlertEvent(
                target_id=self.target_id,
                target_name=self.target_name,
                target_bmc=self.target_bmc,
                severity=severity,
                message=event.get("Message", ""),
                message_id=event.get("MessageId"),
                event_type=event_type,
                origin_of_condition=origin_uri,
                event_timestamp=event_ts,
                received_at=datetime.now(UTC),
            )

            self._last_event_time = datetime.now(UTC)

            # Invoke callback
            if self.callback:
                try:
                    self.callback(alert)
                except Exception as e:
                    logger.error(f"Alert callback error for {self.target_name}: {e}", exc_info=True)

    def _classify_error(self, error: Exception) -> tuple[ErrorCategory, str]:
        """Classify error as permanent or transient with improved logic.

        Args:
            error: The exception that occurred

        Returns:
            Tuple of (ErrorCategory, human-readable reason string)
        """
        error_str = str(error).lower()

        # Permanent errors - stop immediately
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            http_status_errors = {
                401: "Authentication failed (401 Unauthorized)",
                403: "Access forbidden (403)",
                404: "SSE endpoint not found (404) - BMC does not support SSE",
                405: "SSE not supported (405 Method Not Allowed)",
                501: "SSE not implemented by BMC (501)",
            }
            if status in http_status_errors:
                return ErrorCategory.PERMANENT, http_status_errors[status]

        # Check for HTTP errors in RuntimeError messages (from _connect_and_listen)
        if isinstance(error, RuntimeError) and "http" in error_str:
            if "http 401" in error_str:
                return ErrorCategory.PERMANENT, "Authentication failed (401 Unauthorized)"
            elif "http 403" in error_str:
                return ErrorCategory.PERMANENT, "Access forbidden (403)"
            elif "http 404" in error_str:
                return (
                    ErrorCategory.PERMANENT,
                    "SSE endpoint not found (404) - BMC does not support SSE",
                )
            elif "http 405" in error_str:
                return ErrorCategory.PERMANENT, "SSE not supported (405 Method Not Allowed)"
            elif "http 501" in error_str:
                return ErrorCategory.PERMANENT, "SSE not implemented by BMC (501)"

        # Connection refused - permanent after extended period
        if "connection refused" in error_str or "cannot connect" in error_str:
            if self._consecutive_failures > 10:
                return (
                    ErrorCategory.PERMANENT,
                    "Connection refused for extended period - BMC may be offline or firewalled",
                )
            return ErrorCategory.TRANSIENT, "Connection refused (BMC may be restarting)"

        # Connection errors - check for extended failures
        if isinstance(error, httpx.ConnectError | OSError):
            if self._consecutive_failures > 10:
                return (
                    ErrorCategory.PERMANENT,
                    "Connection failures for extended period - network issue or BMC offline",
                )
            return ErrorCategory.TRANSIENT, "Network connection error"

        # Check for SSL/TLS certificate errors
        if (
            "certificate" in error_str
            or "ssl" in error_str
            or isinstance(error, httpx.ConnectError)
        ) and "certificate verify failed" in error_str:
            return (
                ErrorCategory.PERMANENT,
                "SSL certificate validation failed (set verify_ssl=false to ignore)",
            )

        # Check for invalid endpoint detection (stream closes immediately)
        if "closed after" in error_str and "without sending events" in error_str:
            return ErrorCategory.PERMANENT, "Invalid SSE endpoint (stream closes immediately)"

        # Timeout - transient but becomes permanent if persistent
        if isinstance(error, httpx.TimeoutException | asyncio.TimeoutError):
            if self._consecutive_failures > 15:
                return ErrorCategory.PERMANENT, "Persistent timeout - BMC not responding"
            return ErrorCategory.TRANSIENT, "Connection timeout"

        # Network unreachable - permanent after retries
        if "network unreachable" in error_str or "no route to host" in error_str:
            if self._consecutive_failures > 5:
                return ErrorCategory.PERMANENT, "Network unreachable - routing issue"
            return ErrorCategory.TRANSIENT, "Network unreachable"

        if "connection reset" in error_str or "broken pipe" in error_str:
            return ErrorCategory.TRANSIENT, "Connection reset by BMC"

        # Default to transient for unknown errors
        return ErrorCategory.TRANSIENT, f"Unexpected error: {type(error).__name__}: {str(error)}"

    def _calculate_backoff_delay(self) -> int:
        """Calculate exponential backoff delay based on failure count.

        Returns:
            Delay in seconds before next retry
        """
        # Exponential backoff: 30s, 1m, 2m, 5m, 10m, 30m, 1h, 2h (max)
        delays = [30, 60, 120, 300, 600, 1800, 3600, 7200]
        idx = min(self._consecutive_failures - 1, len(delays) - 1)
        return delays[idx] if idx >= 0 else 30

    @property
    def is_running(self) -> bool:
        """Check if subscriber is running."""
        return self._running

    @property
    def consecutive_failures(self) -> int:
        """Get consecutive failure count."""
        return self._consecutive_failures

    @property
    def last_event_time(self) -> datetime | None:
        """Get timestamp of last received event."""
        return self._last_event_time

    @property
    def state(self) -> SubscriptionState:
        """Get current subscription state."""
        return self._state

    @property
    def failure_reason(self) -> str | None:
        """Get human-readable failure reason."""
        return self._failure_reason

    @property
    def next_retry_time(self) -> datetime | None:
        """Get next scheduled retry time."""
        return self._next_retry_time

    @property
    def time_in_current_state(self) -> float | None:
        """Get time in current state (hours), or None if connected."""
        if self._state == SubscriptionState.CONNECTED or self._first_failure_time is None:
            return None
        return (datetime.now(UTC) - self._first_failure_time).total_seconds() / 3600
