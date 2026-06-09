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
"""SSE (Server-Sent Events) subscriber for Redfish MetricReport events.

Maintains persistent connections to BMCs that support SSE-based telemetry
streaming, as defined by the DMTF Redfish specification. Events are parsed
and fed into the same metric extraction pipeline used by the poller.
"""

import asyncio
import json
import logging
import random
import time
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from ..database.models import Target
from ..database.repository import TargetRepository
from .poller import PollResult

logger = logging.getLogger(__name__)

# Default SSE endpoint per DMTF Redfish spec
DEFAULT_SSE_ENDPOINT = "/redfish/v1/EventService/SSE"


class SSEConnection:
    """Manages a single SSE connection to a Redfish target."""

    def __init__(
        self,
        target: Target,
        repository: TargetRepository,
        username: str,
        password: str,
        token: str | None = None,
        sse_endpoint: str | None = None,
        reconnect_delay: int = 5,
        max_reconnect_delay: int = 300,
        connection_timeout: int = 30,
        target_tags: dict | None = None,
    ):
        self.target = target
        self.repository = repository
        self.username = username
        self.password = password
        self.token = token
        self.sse_endpoint = sse_endpoint or DEFAULT_SSE_ENDPOINT
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.connection_timeout = connection_timeout
        self.target_tags = target_tags or {}

        self._running = False
        self._task: asyncio.Task | None = None
        self._current_delay = reconnect_delay
        self._consecutive_errors = 0
        self._last_status_update: float = 0  # monotonic time of last DB status update

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, result_queue: asyncio.Queue) -> None:
        """Start the SSE connection loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(result_queue))
        logger.info(f"SSE subscription started for {self.target.name} ({self.target.host})")

    async def stop(self) -> None:
        """Stop the SSE connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info(f"SSE subscription stopped for {self.target.name}")

    async def _connection_loop(self, result_queue: asyncio.Queue) -> None:
        """Main loop: connect, stream events, reconnect on failure."""
        while self._running:
            try:
                await self._stream_events(result_queue)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_errors += 1
                logger.warning(
                    f"SSE connection lost for {self.target.name}: {e} "
                    f"(attempt {self._consecutive_errors}, reconnecting in {self._current_delay}s)"
                )
                if self._running:
                    # Exponential backoff with jitter to avoid thundering herd
                    jitter = random.uniform(0, self._current_delay * 0.3)
                    await asyncio.sleep(self._current_delay + jitter)
                    self._current_delay = min(self._current_delay * 2, self.max_reconnect_delay)

    # Max time (seconds) to wait for any line from the stream before assuming dead
    STREAM_ACTIVITY_TIMEOUT = 600  # 10 minutes

    async def _stream_events(self, result_queue: asyncio.Queue) -> None:
        """Open SSE connection and process events."""
        url = f"{self.target.base_url}{self.sse_endpoint}"

        # Build auth
        headers = {"Accept": "text/event-stream"}
        auth = None
        if self.token:
            headers["X-Auth-Token"] = self.token
        else:
            auth = httpx.BasicAuth(self.username, self.password)

        logger.info(f"Connecting SSE: {self.target.name} -> {url}")

        async with (
            httpx.AsyncClient(
                verify=self.target.verify_ssl,
                timeout=httpx.Timeout(self.connection_timeout, read=None),
            ) as client,
            client.stream("GET", url, headers=headers, auth=auth) as response,
        ):
            if response.status_code != 200:
                raise RuntimeError(f"SSE connection failed: HTTP {response.status_code}")

            # Reset backoff on successful connection
            self._current_delay = self.reconnect_delay
            self._consecutive_errors = 0
            logger.info(f"SSE connected: {self.target.name}")

            # Parse SSE stream with activity timeout
            event_type = ""
            event_data = []

            async for line in self._iter_lines_with_timeout(response):
                if not self._running:
                    break

                line = line.rstrip("\r\n")

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    event_data.append(line[5:].strip())
                elif line == "":
                    # Empty line = end of event
                    if event_data and event_type == "MetricReport":
                        await self._process_event(event_data, result_queue)
                    event_type = ""
                    event_data = []

    async def _iter_lines_with_timeout(self, response):
        """Wrap response.aiter_lines() with an activity timeout.

        If no line is received within STREAM_ACTIVITY_TIMEOUT, raises
        TimeoutError to trigger reconnection.
        """
        aiter = response.aiter_lines().__aiter__()
        while True:
            try:
                line = await asyncio.wait_for(
                    aiter.__anext__(), timeout=self.STREAM_ACTIVITY_TIMEOUT
                )
                yield line
            except TimeoutError:
                raise TimeoutError(
                    f"No SSE activity for {self.STREAM_ACTIVITY_TIMEOUT}s, reconnecting"
                )
            except StopAsyncIteration:
                return

    async def _process_event(self, data_lines: list[str], result_queue: asyncio.Queue) -> None:
        """Parse a MetricReport SSE event and enqueue as PollResult."""
        raw_data = "\n".join(data_lines)

        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as e:
            logger.warning(f"SSE event JSON parse error from {self.target.name}: {e}")
            return

        # Determine report type from @odata.id if available
        odata_id = payload.get("@odata.id", "")
        report_type = "sse"
        if "ProcessorMetrics" in odata_id:
            report_type = "processor"
        elif "MemoryMetrics" in odata_id:
            report_type = "memory"
        elif "ProcessorPortMetrics" in odata_id:
            report_type = "interconnect"
        elif "PlatformSensors" in odata_id:
            report_type = "platform"
        elif "HealthRollup" in odata_id:
            report_type = "health"
        elif odata_id.endswith("/All"):
            report_type = "comprehensive"

        content = raw_data.encode("utf-8")
        now = datetime.now(UTC)

        result = PollResult(
            target_id=self.target.id,
            target_name=self.target.name,
            target_host=self.target.host,
            success=True,
            content=content,
            content_type="application/json",
            error_message=None,
            poll_time=now,
            duration_ms=0,
            target_tags=self.target_tags,
            data=[(report_type, payload)],
            collection_method="sse",
        )

        try:
            result_queue.put_nowait(result)
            # Rate-limit status updates to at most once per 60 seconds
            now_mono = time.monotonic()
            if now_mono - self._last_status_update >= 60:
                await self.repository.update_poll_status(self.target.id, status="success")
                self._last_status_update = now_mono
        except asyncio.QueueFull:
            logger.warning(f"Result queue full, dropping SSE event from {self.target.name}")


class SSEManager:
    """Manages SSE subscriptions for all SSE-enabled targets.

    Runs alongside the RedfishPoller. SSE targets are not polled;
    instead, this manager maintains persistent streaming connections.
    """

    def __init__(
        self,
        repository: TargetRepository,
        result_queue: asyncio.Queue,
        reconnect_delay: int = 5,
        max_reconnect_delay: int = 300,
        connection_timeout: int = 30,
        default_sse_endpoint: str = DEFAULT_SSE_ENDPOINT,
    ):
        self.repository = repository
        self.result_queue = result_queue
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.connection_timeout = connection_timeout
        self.default_sse_endpoint = default_sse_endpoint

        self._connections: dict[int, SSEConnection] = {}
        self._sync_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the SSE manager and initial connections."""
        if self._running:
            return
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("SSE Manager started")

    async def stop(self) -> None:
        """Stop all SSE connections."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task

        # Stop all active connections
        for conn in self._connections.values():
            await conn.stop()
        self._connections.clear()
        logger.info("SSE Manager stopped")

    async def _sync_loop(self) -> None:
        """Periodically sync SSE connections with target database.

        Starts connections for new SSE targets, stops connections for
        removed/disabled targets.
        """
        while self._running:
            try:
                await self._sync_connections()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SSE sync error: {e}")
            await asyncio.sleep(30)  # Re-sync every 30 seconds

    async def _sync_connections(self) -> None:
        """Reconcile active SSE connections with the target database."""
        targets = await self.repository.get_all_targets(enabled_only=True)

        # Find SSE targets
        sse_targets = {t.id: t for t in targets if t.connection_mode == "sse"}

        # Stop connections for targets that are no longer SSE/enabled
        for target_id in list(self._connections.keys()):
            if target_id not in sse_targets:
                logger.info(f"Stopping SSE for removed/disabled target {target_id}")
                await self._connections[target_id].stop()
                del self._connections[target_id]

        # Start connections for new SSE targets
        for target_id, target in sse_targets.items():
            if target_id not in self._connections:
                await self._start_connection(target)

    async def _start_connection(self, target: Target) -> None:
        """Create and start an SSE connection for a target."""
        password = self.repository.decrypt_password(target)
        token = self.repository.decrypt_token(target)
        target_tags = self.repository.get_target_tags(target)

        sse_endpoint = target.sse_endpoint or self.default_sse_endpoint

        conn = SSEConnection(
            target=target,
            repository=self.repository,
            username=target.username,
            password=password,
            token=token,
            sse_endpoint=sse_endpoint,
            reconnect_delay=self.reconnect_delay,
            max_reconnect_delay=self.max_reconnect_delay,
            connection_timeout=self.connection_timeout,
            target_tags=target_tags,
        )

        self._connections[target.id] = conn
        await conn.start(self.result_queue)

    @property
    def active_connections(self) -> int:
        """Number of active SSE connections."""
        return len(self._connections)
