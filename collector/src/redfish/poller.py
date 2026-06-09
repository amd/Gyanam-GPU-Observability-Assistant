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
"""Round-robin poller for Redfish telemetry collection."""

import asyncio
import logging
import random
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..database.models import Target
from ..database.repository import TargetRepository
from .client import RedfishClient

# How often the status writer drains pending poll-status updates into the DB.
# Batching coalesces concurrent commits that otherwise produce SQLite WAL
# 'database is locked' errors at high concurrency.
_STATUS_FLUSH_INTERVAL_S = 5.0

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    """Result of a single poll operation."""

    target_id: int
    target_name: str
    target_host: str
    success: bool
    content: bytes
    content_type: str
    error_message: str | None
    poll_time: datetime
    duration_ms: float
    target_tags: dict[str, str] = None  # Custom tags from target config
    data: list | None = None  # List of (report_type, json_dict) tuples from GET (bypasses unpacker)
    collection_method: str = "task"  # "get" or "task" for logging


# Type for callback function that processes poll results
PollCallback = Callable[[PollResult], None]


class RedfishPoller:
    """Manages round-robin polling of multiple Redfish targets.

    Polls targets in a round-robin fashion, respecting the configured
    polling interval and maximum concurrency.
    """

    def __init__(
        self,
        repository: TargetRepository,
        poll_interval: int = 300,
        timeout: int = 30,
        max_concurrent: int = 10,
        task_poll_interval: int = 5,
        task_timeout: int = 300,
        download_timeout: int = 300,
        error_retry_interval: int = 10,
        collect_endpoint: str | None = None,
        collect_body: dict | None = None,
        callback: PollCallback | None = None,
        metric_reports: list | None = None,
    ):
        """Initialize the poller.

        Args:
            repository: Target repository for fetching configurations
            poll_interval: Default seconds between polls (can be overridden per target)
            timeout: HTTP request timeout in seconds
            max_concurrent: Maximum concurrent polling operations
            task_poll_interval: Seconds between task status polls
            task_timeout: Maximum seconds to wait for task completion
            download_timeout: Timeout for large file downloads
            error_retry_interval: Seconds to wait before retry on error
            collect_endpoint: Default Redfish collection endpoint
            collect_body: Default request body for collection
            callback: Optional callback function for processing results
            metric_reports: List of MetricReportConfig for direct GET (empty disables)
        """
        self.repository = repository
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.task_poll_interval = task_poll_interval
        self.task_timeout = task_timeout
        self.download_timeout = download_timeout
        self.error_retry_interval = error_retry_interval
        self.collect_endpoint = collect_endpoint
        self.collect_body = collect_body
        self.callback = callback
        self.metric_reports = metric_reports or []
        # Circuit breaker: skip targets after this many consecutive failures.
        # They'll be retried every circuit_breaker_recheck_multiplier poll cycles.
        self.circuit_breaker_threshold = 5
        self.circuit_breaker_recheck_multiplier = 6  # retry every 6x normal interval

        self._running = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._poll_task: asyncio.Task | None = None
        self._status_writer_task: asyncio.Task | None = None
        # Limit queue size to prevent memory issues if results aren't consumed
        self._result_queue: asyncio.Queue[PollResult] = asyncio.Queue(maxsize=1000)
        # Track next poll time per target to support poll_interval_override
        self._next_poll_time: dict[int, datetime] = {}
        # In-flight poll tasks keyed by target id. Prevents a slow poll from being
        # double-scheduled and lets us await all on shutdown.
        self._inflight: dict[int, asyncio.Task] = {}
        # Pending status updates that the status writer batches into a single
        # SQL transaction. Latest update per target wins.
        # value: (status, error_message)
        self._pending_status: dict[int, tuple[str, str | None]] = {}
        self._pending_status_lock = asyncio.Lock()
        # Per-target cached RedfishClient. Reused across polls to avoid the
        # ~50-200ms TCP+TLS handshake and the Redfish session POST/DELETE
        # round-trip on every cycle. Evicted on any poll failure.
        self._clients: dict[int, RedfishClient] = {}
        self._client_locks: dict[int, asyncio.Lock] = {}
        # Stats
        self._polls_started = 0
        self._polls_completed = 0
        self._polls_dropped_queue_full = 0
        self._client_cache_hits = 0
        self._client_cache_misses = 0

    async def start(self) -> None:
        """Start the polling loop."""
        if self._running:
            logger.warning("Poller is already running")
            return

        self._running = True

        # Initialize staggered poll schedule to avoid burst loads
        await self._initialize_staggered_schedule()

        self._poll_task = asyncio.create_task(self._poll_loop())
        self._status_writer_task = asyncio.create_task(self._status_writer_loop())
        logger.info(
            f"Poller started (interval={self.poll_interval}s, max_concurrent={self.max_concurrent})"
        )

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task

        # Let in-flight polls drain (bounded — they hold the semaphore so they
        # are naturally capped at max_concurrent).
        if self._inflight:
            logger.info(f"Waiting for {len(self._inflight)} in-flight polls to complete...")
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*self._inflight.values(), return_exceptions=True)

        if self._status_writer_task:
            # Flush remaining status updates before stopping the writer.
            await self._flush_pending_status()
            self._status_writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._status_writer_task

        # Close all cached Redfish clients (session DELETE + httpx aclose).
        if self._clients:
            logger.info(f"Closing {len(self._clients)} cached Redfish clients...")
            close_tasks = [self._close_client(tid) for tid in list(self._clients.keys())]
            await asyncio.gather(*close_tasks, return_exceptions=True)

        logger.info("Poller stopped")

    async def _get_client(
        self,
        target: Target,
        password: str,
        token: str | None,
        ssh_transport,
    ) -> RedfishClient | None:
        """Return a connected client for `target`, creating one if needed.

        Cached clients are reused across polls so we pay the TCP+TLS handshake
        and Redfish session creation cost once instead of every cycle. Eviction
        happens on the failure path via `_close_client`.
        """
        # Per-target lock so concurrent paths (unlikely since _inflight prevents
        # parallel polls of the same target, but defensive) don't both build a
        # client at the same time.
        lock = self._client_locks.setdefault(target.id, asyncio.Lock())
        async with lock:
            cached = self._clients.get(target.id)
            if cached is not None:
                self._client_cache_hits += 1
                return cached

            self._client_cache_misses += 1
            client = RedfishClient(
                base_url=target.base_url,
                username=target.username,
                password=password,
                token=token,
                timeout=self.timeout,
                verify_ssl=target.verify_ssl,
                task_poll_interval=self.task_poll_interval,
                task_timeout=self.task_timeout,
                download_timeout=self.download_timeout,
                cleanup_task_on_success=True,
                ssh_transport=ssh_transport,
            )
            try:
                await client.connect()
            except Exception as e:
                logger.warning(f"Failed to connect new client for {target.name}: {e}")
                with suppress(Exception):
                    await client.close()
                return None

            self._clients[target.id] = client
            return client

    async def _close_client(self, target_id: int) -> None:
        """Close and evict a cached client (called on failure and on stop)."""
        client = self._clients.pop(target_id, None)
        if client is not None:
            with suppress(Exception):
                await client.close()

    async def _schedule_new_targets(self, new_targets: list[Target]) -> None:
        """Schedule newly added targets with stagger to maintain load distribution.

        When targets are added dynamically (after startup), we need to integrate them
        into the existing schedule without creating burst loads.

        Strategy: Spread new targets evenly across the polling interval, finding
        gaps in the existing schedule.

        Args:
            new_targets: List of targets that need to be scheduled
        """
        if not new_targets:
            return

        now = datetime.now(UTC)
        base_interval = self.poll_interval

        # Calculate stagger delay for new targets
        # Spread them evenly across the interval
        stagger_seconds = base_interval / len(new_targets)

        logger.info(
            f"Scheduling {len(new_targets)} new targets with stagger "
            f"(~{stagger_seconds:.2f}s between each)"
        )

        for idx, target in enumerate(new_targets):
            # Get target-specific interval
            interval = self._get_target_interval(target)

            # Calculate offset for this new target
            # Spread evenly across interval starting from a random point
            # to avoid clustering new targets at the same offset
            random_start = random.uniform(0, interval * 0.1)  # Start within first 10%
            offset_seconds = (random_start + (idx * stagger_seconds)) % interval

            # Add jitter
            jitter = random.uniform(-interval * 0.01, interval * 0.01)
            final_offset = max(0, offset_seconds + jitter)

            # Schedule
            next_poll = now + timedelta(seconds=final_offset)
            self._next_poll_time[target.id] = next_poll

            logger.info(
                f"New target '{target.name}' scheduled for first poll in {final_offset:.1f}s"
            )

    async def _initialize_staggered_schedule(self) -> None:
        """Initialize staggered poll schedule for all targets.

        Distributes targets evenly across the polling interval to avoid
        burst loads where all targets poll simultaneously. This provides:
        - Smooth, predictable load on InfluxDB
        - Better resource utilization (CPU, memory, network)
        - Lower peak buffer usage
        - Easier scaling to 300+ endpoints

        For 300 targets with 300s interval:
        - Target 0 polls at t=0s
        - Target 1 polls at t=1s
        - Target 299 polls at t=299s
        - Result: ~1 target/second constant rate vs 300 targets every 5min burst
        """
        targets = await self.repository.get_all_targets(enabled_only=True)

        if not targets:
            return

        # Filter out SSE targets (managed separately by SSEManager)
        poll_targets = [t for t in targets if t.connection_mode != "sse"]

        if not poll_targets:
            return

        now = datetime.now(UTC)
        base_interval = self.poll_interval

        # Calculate stagger delay per target to spread evenly across interval
        stagger_seconds = base_interval / len(poll_targets)

        logger.info(
            f"Initializing staggered polling for {len(poll_targets)} targets "
            f"(~{stagger_seconds:.2f}s between each target)"
        )

        for idx, target in enumerate(poll_targets):
            # Skip if already scheduled (e.g., after poller restart)
            if target.id in self._next_poll_time:
                continue

            # Get target-specific interval (respects poll_interval_override)
            interval = self._get_target_interval(target)

            # Calculate initial offset: spread evenly across the interval
            # Target 0 polls immediately, target N polls at N * stagger_seconds
            offset_seconds = (idx * stagger_seconds) % interval

            # Add small random jitter (±1% of interval) to avoid exact synchronization
            # This prevents targets from slowly drifting back together over time
            jitter = random.uniform(-interval * 0.01, interval * 0.01)

            # Ensure offset is never negative (can happen if jitter is negative for first targets)
            final_offset = max(0, offset_seconds + jitter)

            next_poll = now + timedelta(seconds=final_offset)
            self._next_poll_time[target.id] = next_poll

            if idx < 3 or idx >= len(poll_targets) - 3:
                # Log first 3 and last 3 for visibility
                logger.debug(
                    f"Target '{target.name}' (#{idx}) scheduled for first poll in "
                    f"{offset_seconds:.1f}s"
                )

        logger.info(
            f"Staggered schedule initialized: polls spread across {base_interval}s interval"
        )

    async def _poll_loop(self) -> None:
        """Main polling loop.

        Fires off due-target polls as background tasks and returns quickly.
        Concurrency is bounded by `_semaphore`; pacing is bounded by the
        per-target `_next_poll_time` (set before the poll starts), so a slow
        cycle cannot accumulate a backlog that produces a thundering-herd
        spike on the next iteration.
        """
        # Tick frequently enough that small per-target intervals are responsive.
        tick = max(1, min(self.poll_interval, 5))
        while self._running:
            try:
                await self._schedule_due_polls()
                await asyncio.sleep(tick)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in poll loop: {e}", exc_info=True)
                await asyncio.sleep(self.error_retry_interval)

    def _get_target_interval(self, target: Target, poll_succeeded: bool = True) -> int:
        """Get the effective poll interval for a target.

        Targets that have exceeded the circuit breaker threshold are polled
        at a much lower frequency to avoid wasting resources.

        Args:
            target: Target configuration (may have stale consecutive_failures)
            poll_succeeded: Whether the most recent poll succeeded. Used to
                compute the up-to-date failure count without a DB re-read.
        """
        base_interval = self.poll_interval
        if target.poll_interval_override and target.poll_interval_override > 0:
            base_interval = target.poll_interval_override

        # Compute the effective failure count after this poll
        if poll_succeeded:
            effective_failures = 0
        else:
            effective_failures = target.consecutive_failures + 1

        if effective_failures >= self.circuit_breaker_threshold:
            return base_interval * self.circuit_breaker_recheck_multiplier

        return base_interval

    async def _schedule_due_polls(self) -> None:
        """Fire off poll tasks for targets that are due.

        Returns as soon as tasks are dispatched. Does NOT wait for polls
        to complete — that's what causes the thundering-herd spike when one
        cycle runs long. Each poll task self-completes and self-reports
        its result via the result queue and pending status buffer.
        """
        targets = await self.repository.get_all_targets(enabled_only=True)

        if not targets:
            logger.debug("No enabled targets to poll")
            return

        now = datetime.now(UTC)
        due_targets: list[Target] = []
        new_targets: list[Target] = []

        for target in targets:
            # Skip SSE targets — they are managed by SSEManager
            if target.connection_mode == "sse":
                continue
            # Skip targets whose previous poll is still in-flight. Their
            # next_poll_time will be set when that poll completes.
            if target.id in self._inflight:
                continue

            next_time = self._next_poll_time.get(target.id)

            if next_time is None:
                # New target - schedule with stagger to avoid burst
                new_targets.append(target)
            elif now >= next_time:
                due_targets.append(target)

        # Schedule new targets with stagger to maintain smooth load distribution
        if new_targets:
            await self._schedule_new_targets(new_targets)
            # Don't poll them immediately - they'll be picked up in next cycle

        # Clean up schedule entries for targets that no longer exist, and
        # close their cached Redfish clients so we don't leak sessions.
        active_ids = {t.id for t in targets}
        for stale_id in list(self._next_poll_time.keys()):
            if stale_id not in active_ids:
                del self._next_poll_time[stale_id]
        for stale_id in list(self._clients.keys()):
            if stale_id not in active_ids:
                # Schedule eviction; awaiting close inline would slow down the
                # scheduler. Fire-and-forget is fine — the done-callback isn't
                # needed because we've already removed from _clients.
                asyncio.create_task(self._close_client(stale_id))
                self._client_locks.pop(stale_id, None)

        if not due_targets:
            return

        # Pre-schedule the next poll time BEFORE firing the task. This is the
        # key change vs. the old gather()-based loop: the loop iteration that
        # sees a target as due immediately advances its next_poll_time, so the
        # next tick won't see the same target as due, even if the poll itself
        # takes longer than the poll interval.
        for target in due_targets:
            interval = self._get_target_interval(target, poll_succeeded=True)
            jitter = random.uniform(-interval * 0.02, interval * 0.02)
            self._next_poll_time[target.id] = now + timedelta(seconds=interval + jitter)

        logger.info(
            f"Scheduling {len(due_targets)} due polls "
            f"(of {len(targets)} total, {len(self._inflight)} in-flight)"
        )

        for target in due_targets:
            task = asyncio.create_task(self._run_poll(target))
            self._inflight[target.id] = task
            task.add_done_callback(self._on_poll_task_done)
            self._polls_started += 1

    def _on_poll_task_done(self, task: asyncio.Task) -> None:
        """Cleanup callback — remove the task from the in-flight map."""
        # Find and remove by identity (target_id is captured implicitly).
        for tid, t in list(self._inflight.items()):
            if t is task:
                del self._inflight[tid]
                break
        # Surface unexpected exceptions; cancelled tasks are normal on shutdown.
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(f"Poll task crashed: {type(exc).__name__}: {exc}")

    async def _run_poll(self, target: Target) -> None:
        """Execute a single poll under the concurrency semaphore.

        On completion, enqueues the result and records a pending status
        update (batched into a single SQL transaction by the status writer
        to avoid SQLite WAL write contention at high concurrency).
        """
        result: PollResult | None = None
        try:
            async with self._semaphore:
                result = await self._poll_target(target)
        except Exception as e:
            logger.error(f"Error in _run_poll for {target.name}: {e}", exc_info=True)
            return
        finally:
            self._polls_completed += 1

        if result is None:
            return

        # Enqueue for downstream processing (non-blocking, drops on overflow).
        try:
            self._result_queue.put_nowait(result)
        except asyncio.QueueFull:
            self._polls_dropped_queue_full += 1
            logger.warning(
                f"Result queue full, dropping result from {result.target_name} "
                f"(total dropped: {self._polls_dropped_queue_full})"
            )

        # Record status for batched DB write.
        status = "success" if result.success else "error"
        async with self._pending_status_lock:
            self._pending_status[target.id] = (status, result.error_message)

        # If the poll failed and tripped the circuit breaker, override the
        # optimistic next_poll_time set in _schedule_due_polls with the
        # backed-off interval. We must re-fetch the target to get the new
        # consecutive_failures count — but only on failure (rare path).
        if not result.success:
            try:
                fresh = await self.repository.get_target(target.id)
                if fresh is not None:
                    interval = self._get_target_interval(fresh, poll_succeeded=False)
                    jitter = random.uniform(-interval * 0.02, interval * 0.02)
                    self._next_poll_time[target.id] = datetime.now(UTC) + timedelta(
                        seconds=interval + jitter
                    )
            except Exception as e:
                logger.debug(f"Could not refresh schedule for failed {target.name}: {e}")

        # User callback (e.g., test hooks). Failures shouldn't break polling.
        if self.callback:
            try:
                self.callback(result)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    async def _status_writer_loop(self) -> None:
        """Periodically batch pending poll-status updates into one SQL transaction.

        Without batching, 100 concurrent polls each call session.commit() in
        update_poll_status, producing 'database is locked' errors under WAL.
        """
        while self._running:
            try:
                await asyncio.sleep(_STATUS_FLUSH_INTERVAL_S)
                await self._flush_pending_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in status writer: {e}", exc_info=True)
                await asyncio.sleep(_STATUS_FLUSH_INTERVAL_S)

    async def _flush_pending_status(self) -> None:
        """Drain the pending status dict and commit all updates in one transaction."""
        async with self._pending_status_lock:
            if not self._pending_status:
                return
            updates = self._pending_status
            self._pending_status = {}

        try:
            await self.repository.update_poll_status_batch(updates)
        except Exception as e:
            logger.error(f"Failed to flush {len(updates)} status updates: {e}")
            # Re-merge so we retry next tick. Newer writes win.
            async with self._pending_status_lock:
                for tid, val in updates.items():
                    self._pending_status.setdefault(tid, val)

    async def _poll_target(self, target: Target) -> PollResult:
        """Poll a single target, trying GET-first then falling back to task-based.

        If ``metric_reports`` is configured, parallel GETs are attempted first.
        Specific reports are processed before 'All' for dedup priority.
        On total failure the existing task-based workflow runs as fallback.

        Args:
            target: Target configuration

        Returns:
            PollResult with the outcome
        """
        import json

        start_time = datetime.now(UTC)
        target_tags = self.repository.get_target_tags(target)

        # Resolve per-target metric report overrides (fall back to global config)
        target_reports = self.repository.get_target_metric_reports(target)
        reports_to_use = target_reports if target_reports else self.metric_reports

        client: RedfishClient | None = None
        try:
            # Get decrypted credentials
            password = self.repository.decrypt_password(target)
            token = self.repository.decrypt_token(target)

            # Create SSH transport if target uses proxy mode. The transport
            # is owned by the cached client and reused across polls.
            ssh_transport = None
            if target.connection_mode == "ssh_proxy" and target.id not in self._clients:
                from .ssh_transport import SSHTransport

                ssh_key = self.repository.decrypt_ssh_key(target)
                ssh_password = self.repository.decrypt_ssh_password(target)
                ssh_transport = SSHTransport(
                    proxy_host=target.ssh_proxy_host,
                    proxy_port=target.ssh_proxy_port or 22,
                    proxy_username=target.ssh_proxy_username or "root",
                    ssh_key=ssh_key,
                    ssh_password=ssh_password,
                    command_template=target.ssh_command_template,
                    command_timeout=self.download_timeout,
                    verify_ssl=target.verify_ssl,
                )

            client = await self._get_client(target, password, token, ssh_transport)
            if client is None:
                # Connect failure already logged; surface as a failed poll.
                end_time = datetime.now(UTC)
                duration_ms = (end_time - start_time).total_seconds() * 1000
                return PollResult(
                    target_id=target.id,
                    target_name=target.name,
                    target_host=target.host,
                    success=False,
                    content=b"",
                    content_type="",
                    error_message="Failed to establish Redfish client connection",
                    poll_time=start_time,
                    duration_ms=duration_ms,
                    target_tags=target_tags,
                )

            # Client lifecycle is managed by the poller (cached across polls),
            # not by `async with`, so no auto-close at the end of this scope.
            collection_method = "task"
            response = None
            parsed_data = None

            # --- GET-first attempt: parallel fetch of all reports ---
            if reports_to_use:

                async def _fetch_report(report_cfg):
                    """Fetch a single report and return (report_type, json_dict) or None."""
                    uri = report_cfg.uri if hasattr(report_cfg, "uri") else report_cfg["uri"]
                    rtype = (
                        report_cfg.report_type
                        if hasattr(report_cfg, "report_type")
                        else report_cfg["report_type"]
                    )
                    get_resp = await client.get_metric_report(uri)
                    if not get_resp.success:
                        logger.info(
                            f"GET {rtype} report failed for {target.name}: "
                            f"{get_resp.error_message}"
                        )
                        return None
                    try:
                        data = json.loads(get_resp.content)
                        logger.debug(
                            f"GET {rtype} report succeeded for {target.name} "
                            f"({len(get_resp.content)} bytes)"
                        )
                        return (rtype, data, get_resp)
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logger.warning(f"GET {rtype} JSON parse error for {target.name}: {e}")
                        return None

                # Fire all GETs in parallel
                fetch_results = await asyncio.gather(
                    *[_fetch_report(rc) for rc in reports_to_use], return_exceptions=True
                )

                # Collect successful results preserving config order
                report_tuples = []
                last_resp = None
                for fr in fetch_results:
                    if isinstance(fr, Exception):
                        logger.warning(f"Report fetch exception for {target.name}: {fr}")
                        continue
                    if fr is not None:
                        rtype, data, resp = fr  # type: ignore[misc]
                        report_tuples.append((rtype, data))
                        last_resp = resp

                if report_tuples:
                    parsed_data = report_tuples
                    response = last_resp  # Use last successful response for content/type
                    collection_method = "get"
                    logger.info(
                        f"GET collected {len(report_tuples)}/{len(reports_to_use)} "
                        f"reports for {target.name}"
                    )
                else:
                    logger.info(
                        f"All GET reports failed for {target.name}, "
                        f"falling back to task-based collection"
                    )

            # --- Task-based fallback ---
            if response is None:
                endpoint = target.telemetry_endpoint or self.collect_endpoint
                response = await client.collect_diagnostic_data(
                    collect_endpoint=endpoint, collect_body=self.collect_body
                )

            end_time = datetime.now(UTC)
            duration_ms = (end_time - start_time).total_seconds() * 1000

            # If the fast-path GET produced structured `data`, drop the raw
            # response bytes — the downstream pipeline only consults `data` and
            # carrying the blob bloats the result queue (1000 slots × ~200KB
            # is a quarter-GB held for nothing).
            content_bytes = b"" if parsed_data is not None else response.content
            content_type = "" if parsed_data is not None else response.content_type

            result = PollResult(
                target_id=target.id,
                target_name=target.name,
                target_host=target.host,
                success=response.success,
                content=content_bytes,
                content_type=content_type,
                error_message=response.error_message,
                poll_time=start_time,
                duration_ms=duration_ms,
                target_tags=target_tags,
                data=parsed_data,
                collection_method=collection_method,
            )

            # NOTE: status update is deliberately not committed here. The
            # caller (_run_poll for the main loop, poll_single for manual
            # triggers) decides whether to batch or commit immediately.

            if response.success:
                logger.debug(
                    f"Successfully polled {target.name} via {collection_method} "
                    f"({len(response.content)} bytes in {duration_ms:.0f}ms)"
                )
            else:
                logger.warning(f"Failed to poll {target.name}: {response.error_message}")
                # Drop the cached client so the next poll re-handshakes —
                # most logical failures here (401 expired session, auth
                # change) are recovered by a fresh session.
                await self._close_client(target.id)

            return result

        except Exception as e:
            end_time = datetime.now(UTC)
            duration_ms = (end_time - start_time).total_seconds() * 1000

            error_msg = str(e)
            logger.error(f"Error polling {target.name}: {error_msg}")

            # Evict the cached client — a network error, auth failure, or any
            # other in-flight exception leaves the connection in an unknown
            # state. The next poll will rebuild fresh.
            await self._close_client(target.id)

            return PollResult(
                target_id=target.id,
                target_name=target.name,
                target_host=target.host,
                success=False,
                content=b"",
                content_type="",
                error_message=error_msg,
                poll_time=start_time,
                duration_ms=duration_ms,
                target_tags=target_tags,
            )

    async def poll_single(self, target_id: int) -> PollResult | None:
        """Poll a single target on demand.

        Manual polls bypass the batch status writer, so the status update
        is committed inline.

        Args:
            target_id: Target ID to poll

        Returns:
            PollResult or None if target not found
        """
        target = await self.repository.get_target(target_id)
        if not target:
            return None

        result = await self._poll_target(target)
        with suppress(Exception):
            await self.repository.update_poll_status(
                target.id,
                status="success" if result.success else "error",
                error_message=result.error_message,
            )
        return result

    def get_stats(self) -> dict:
        """Return poller stats for the health endpoint."""
        total_lookups = self._client_cache_hits + self._client_cache_misses
        hit_rate = round(self._client_cache_hits / total_lookups * 100, 1) if total_lookups else 0.0
        return {
            "polls_started": self._polls_started,
            "polls_completed": self._polls_completed,
            "inflight": len(self._inflight),
            "pending_status_updates": len(self._pending_status),
            "result_queue_size": self._result_queue.qsize(),
            "result_queue_drops": self._polls_dropped_queue_full,
            "scheduled_targets": len(self._next_poll_time),
            "cached_clients": len(self._clients),
            "client_cache_hit_rate_pct": hit_rate,
            "client_cache_hits": self._client_cache_hits,
            "client_cache_misses": self._client_cache_misses,
        }

    async def get_results(self, timeout: float = 0.1) -> list[PollResult]:
        """Get pending poll results from the queue.

        Args:
            timeout: How long to wait for results

        Returns:
            List of PollResult objects
        """
        results = []
        try:
            while True:
                result = await asyncio.wait_for(self._result_queue.get(), timeout=timeout)
                results.append(result)
        except TimeoutError:
            pass
        return results

    @property
    def is_running(self) -> bool:
        """Check if the poller is running."""
        return self._running
