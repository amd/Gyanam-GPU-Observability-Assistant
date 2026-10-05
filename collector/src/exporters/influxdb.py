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
"""InfluxDB exporter for time series metrics."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from influxdb_client import Point, WritePrecision
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

if TYPE_CHECKING:
    from influxdb_client.client.write_api_async import WriteApiAsync

from ..metrics_cache import CACHED_METRIC_NAMES, HEATMAP
from .base import BaseExporter, Metric

logger = logging.getLogger(__name__)


def _record_heatmap(metrics: list[Metric]) -> None:
    """Feed the small set of heatmap-relevant metrics into the hot cache.

    Cheap O(1) dict writes for a filtered subset — no effect on the export path
    for the vast majority of metrics, which aren't cached.
    """
    for m in metrics:
        if m.name in CACHED_METRIC_NAMES:
            HEATMAP.record(m.tags.get("host"), m.name, m.value)


# Batches at/above this point count serialize to line protocol off the event
# loop; smaller ones encode inline to skip the thread-hop.
_SERIALIZE_OFFLOAD_THRESHOLD = 256


def _serialize_batch(batch: list[Point]) -> list[str]:
    """Encode Points to InfluxDB line protocol (ms precision).

    CPU-bound for large batches, so callers run it off the event loop. Pre-
    serializing means the write path ships ready line protocol instead of
    encoding synchronously inside the async write (which stalls polling + the
    heatmap endpoint during a flush).
    """
    return [p.to_line_protocol(precision=WritePrecision.MS) for p in batch]


def _describe_point(point: Point) -> str:
    """Render one Point as line protocol for logging the offending value when a
    batch is bisected down to its single rejected point. Falls back to repr()."""
    try:
        return _serialize_batch([point])[0]
    except Exception:  # noqa: BLE001 — logging aid must never raise
        return repr(point)


# Force a reconnect after this many consecutive batch failures. Without this,
# batch-level errors (ServerDisconnectedError, ClientOSError) never null
# _write_api, so the flush loop never enters its reconnect path and writes
# silently stop forever.
_CONSECUTIVE_FAILURE_RECONNECT_THRESHOLD = 3

# Reject ping() as healthy if it doesn't return within this many seconds.
# Otherwise _reconnect() can hang holding _connection_lock, which blocks
# every other coroutine that needs the write_api.
_PING_TIMEOUT_S = 10.0

# A write that hasn't happened in this many seconds means the pipeline is
# effectively dead, even if the client object thinks it's connected.
_PIPELINE_DEAD_AFTER_S = 600.0


class InfluxDBExporter(BaseExporter):
    """Exports metrics to InfluxDB 2.x.

    Supports batched async writes for high-volume metrics.
    """

    # Absolute ceiling on buffered points during an outage, independent of
    # batch_size, to bound worst-case memory (~hundreds of MB of Point objects).
    _MAX_BUFFER_ABSOLUTE = 500_000

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        batch_size: int = 1000,
        flush_interval: int = 10,
        verify_ssl: bool = False,
        write_timeout_ms: int = 90000,
        max_concurrent_writes: int = 5,
    ):
        """Initialize the InfluxDB exporter.

        Args:
            url: InfluxDB URL (e.g., http://localhost:8086)
            token: InfluxDB authentication token
            org: InfluxDB organization
            bucket: InfluxDB bucket to write to
            batch_size: Maximum metrics per write batch (reduced to 1000 for better throughput)
            flush_interval: Seconds between automatic flushes
            verify_ssl: Whether to verify SSL certificates
            write_timeout_ms: Write operation timeout in milliseconds (default 90s)
            max_concurrent_writes: Maximum number of parallel write operations (default 5)
        """
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.verify_ssl = verify_ssl
        self.write_timeout_ms = write_timeout_ms
        self.max_concurrent_writes = max_concurrent_writes

        self._client: InfluxDBClientAsync | None = None
        self._write_api: WriteApiAsync | None = None
        self._buffer: list[Point] = []
        self._buffer_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()  # Protects _write_api state changes
        self._flush_task: asyncio.Task | None = None
        self._running = False
        # Maximum buffer size to prevent memory issues during InfluxDB outages.
        # For 300+ endpoints we want a large buffer, but sizing it purely as a
        # multiple of batch_size is a trap: raising batch_size for throughput
        # (e.g. 10_000) would silently balloon the cap to millions of points and
        # OOM the process during an outage. Keep the 200x heuristic but clamp it
        # to an absolute ceiling that bounds worst-case memory.
        self._max_buffer_size = min(batch_size * 200, self._MAX_BUFFER_ABSOLUTE)
        self._write_semaphore = asyncio.Semaphore(max_concurrent_writes)  # Limit concurrent writes
        self._dropped_points = 0
        self._rejected_points = 0  # points dropped because InfluxDB rejected them (4xx)
        # Dedicated small pool for line-protocol serialization so it never
        # competes with the (much larger, GIL-heavy) metric-extraction pool that
        # backs asyncio.to_thread. Serialization is light but must not queue
        # behind extraction during a flush.
        self._serialize_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="influx-serialize"
        )
        self._reconnect_delay = 10.0  # seconds between reconnection attempts
        self._max_reconnect_delay = 300.0  # max backoff

        # Producers signal this when they put data into the buffer. The flush
        # loop waits on it instead of sleeping a fixed interval, so writes
        # happen promptly without producers ever awaiting the network.
        self._flush_event = asyncio.Event()

        # Consecutive batch failures across all parallel writers. When this
        # exceeds the threshold, we forcibly null _write_api so the flush loop
        # enters its reconnect path. Without this the exporter can stay in a
        # "connected but every write fails" state indefinitely.
        self._consecutive_batch_failures = 0

        # Log the "token doesn't match the volume" diagnosis at most once, so an
        # auth failure doesn't drown the logs but is still clearly explained.
        self._auth_hint_logged = False

        # Write operation tracking
        self._write_count = 0
        self._total_points_written = 0
        self._last_write_time: datetime | None = None
        self._write_failures_24h = 0
        self._write_latency_ms: float = 0  # Moving average of write latency
        self._successful_batches = 0
        self._failed_batches = 0
        self._reconnect_count = 0

    async def connect(
        self, max_retries: int = 5, initial_delay: float = 1.0, max_delay: float = 30.0
    ) -> None:
        """Establish connection to InfluxDB with retry logic.

        Args:
            max_retries: Maximum number of connection attempts
            initial_delay: Initial delay between retries in seconds
            max_delay: Maximum delay between retries in seconds
        """
        delay = initial_delay
        last_error = None

        for attempt in range(max_retries):
            try:
                client = InfluxDBClientAsync(
                    url=self.url,
                    token=self.token,
                    org=self.org,
                    verify_ssl=self.verify_ssl,
                    timeout=self.write_timeout_ms,
                    enable_gzip=True,  # Compress writes (20-40% reduction)
                )

                # Verify connection by pinging
                if await client.ping():
                    # Atomically update connection state under lock
                    async with self._connection_lock:
                        self._client = client
                        self._write_api = self._client.write_api()

                    self._start_flush_loop()
                    logger.info(f"Connected to InfluxDB at {self.url}")
                    return
                else:
                    raise ConnectionError("InfluxDB ping failed")

            except Exception as e:
                last_error = e
                if client:
                    with suppress(Exception):
                        await client.close()

                if attempt < max_retries - 1:
                    logger.warning(
                        f"InfluxDB connection attempt {attempt + 1}/{max_retries} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)  # Exponential backoff

        # All retries failed - start flush loop anyway so it can reconnect later
        self._start_flush_loop()

        logger.error(f"Failed to connect to InfluxDB after {max_retries} attempts: {last_error}")
        raise ConnectionError(
            f"Could not connect to InfluxDB at {self.url} after {max_retries} attempts"
        ) from last_error

    def _start_flush_loop(self) -> None:
        """Start the background flush loop if not already running."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _force_reset_connection(self) -> None:
        """Null the write_api so the flush loop enters its reconnect path.

        Called when consecutive failures indicate the cached connection is
        broken even though the client object thinks it's fine.
        """
        async with self._connection_lock:
            if self._client:
                # Close in the background; don't block the caller. The next
                # _reconnect() call also defensively closes any leftover.
                with suppress(Exception):
                    await self._client.close()
            self._client = None
            self._write_api = None
        # Reset the counter so we don't immediately try to reset again before
        # the reconnect path gets a chance to run.
        self._consecutive_batch_failures = 0

    async def _reconnect(self) -> bool:
        """Attempt to reconnect to InfluxDB.

        Returns:
            True if reconnection succeeded
        """
        logger.info(f"Attempting to reconnect to InfluxDB at {self.url}...")

        async with self._connection_lock:
            # Close existing client if any
            if self._client:
                with suppress(Exception):
                    await self._client.close()
                self._client = None
                self._write_api = None

            try:
                self._client = InfluxDBClientAsync(
                    url=self.url,
                    token=self.token,
                    org=self.org,
                    verify_ssl=self.verify_ssl,
                    timeout=self.write_timeout_ms,
                    enable_gzip=True,  # Compress writes (20-40% reduction)
                )
                # Cap the ping — without this, a hung TCP connect or a
                # half-dead server can hold _connection_lock indefinitely
                # and block every other coroutine that wants the write_api.
                ping_ok = await asyncio.wait_for(self._client.ping(), timeout=_PING_TIMEOUT_S)
                if ping_ok:
                    self._write_api = self._client.write_api()
                    self._reconnect_delay = 10.0  # reset backoff
                    self._reconnect_count += 1
                    logger.info("Reconnected to InfluxDB successfully")
                    return True
                else:
                    raise ConnectionError("InfluxDB ping failed")
            except TimeoutError:
                logger.warning(
                    f"Reconnection ping timed out after {_PING_TIMEOUT_S}s — "
                    "treating as failed reconnect"
                )
                if self._client:
                    with suppress(Exception):
                        await self._client.close()
                    self._client = None
                    self._write_api = None
                return False
            except Exception as e:
                logger.warning(f"Reconnection failed: {e}")
                if self._client:
                    with suppress(Exception):
                        await self._client.close()
                    self._client = None
                    self._write_api = None
                return False

    async def close(self) -> None:
        """Close the InfluxDB connection."""
        self._running = False

        # Cancel flush task
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task

        # Flush remaining buffer
        await self._flush_buffer()

        # Close client under lock
        async with self._connection_lock:
            if self._client:
                await self._client.close()
                self._client = None
                self._write_api = None

        # Tear down the serialization pool (don't wait on in-flight — the final
        # flush above already completed; threads are daemonic serializers).
        self._serialize_executor.shutdown(wait=False, cancel_futures=True)

        logger.info("Disconnected from InfluxDB")

    async def write(self, metrics: list[Metric]) -> bool:
        """Buffer metrics for asynchronous writing to InfluxDB.

        This call NEVER awaits the network. The flush loop owns all writes;
        producers only enqueue. If the buffer crosses batch_size we signal
        the flush loop via an event so it wakes immediately instead of
        sleeping the full flush_interval.

        Returns:
            True if metrics were buffered successfully (always).
        """
        _record_heatmap(metrics)
        points = [self._metric_to_point(m) for m in metrics]

        should_signal = False
        async with self._buffer_lock:
            self._buffer.extend(points)
            # Enforce max buffer size to prevent unbounded growth. Keep the
            # NEWEST points by dropping from the front. This correctly caps even
            # when a single write is larger than the buffer.
            if len(self._buffer) > self._max_buffer_size:
                overflow = len(self._buffer) - self._max_buffer_size
                self._dropped_points += overflow
                self._buffer = self._buffer[overflow:]
                logger.warning(
                    f"Buffer overflow on write: dropped {overflow} oldest points "
                    f"(total dropped: {self._dropped_points})"
                )
            should_signal = len(self._buffer) >= self.batch_size

        if should_signal:
            self._flush_event.set()

        return True

    async def write_immediate(self, metrics: list[Metric]) -> bool:
        """Write metrics immediately without buffering.

        Args:
            metrics: List of Metric objects to write

        Returns:
            True if write was successful
        """
        # Atomic snapshot to prevent race condition
        async with self._connection_lock:
            write_api = self._write_api

        if not write_api:
            logger.error("Not connected to InfluxDB")
            return False

        points = [self._metric_to_point(m) for m in metrics]

        try:
            await write_api.write(bucket=self.bucket, org=self.org, record=points)
            logger.debug(f"Wrote {len(points)} metrics to InfluxDB")
            return True
        except Exception as e:
            logger.error(f"Failed to write to InfluxDB: {type(e).__name__}: {e}", exc_info=True)
            return False

    async def _flush_loop(self) -> None:
        """Background task that owns all writes to InfluxDB.

        Waits on `_flush_event` so producers can signal an immediate flush
        when the buffer fills, with `flush_interval` as a fallback timeout.
        When disconnected, attempts reconnection with exponential backoff.
        """
        while self._running:
            try:
                if not self._write_api:
                    # Disconnected: wait with backoff, then try to reconnect.
                    await asyncio.sleep(self._reconnect_delay)
                    if await self._reconnect():
                        # Drain whatever has accumulated.
                        await self._flush_buffer()
                    else:
                        self._reconnect_delay = min(
                            self._reconnect_delay * 2, self._max_reconnect_delay
                        )
                else:
                    # Connected: wake on event OR after flush_interval, whichever first.
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._flush_event.wait(), timeout=self.flush_interval
                        )
                    self._flush_event.clear()
                    await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in flush loop: {e}", exc_info=True)
                # Sleep before retrying to prevent tight error loop
                await asyncio.sleep(self.flush_interval)

    @staticmethod
    def _is_unprocessable(error: Exception, err_text: str) -> bool:
        """True when InfluxDB permanently rejected the payload (vs a transient
        network/5xx error worth retrying).

        A 400/422 is a client-side payload problem (malformed line protocol,
        non-finite value, type conflict) — the identical batch will never be
        accepted, so it must be dropped, not re-queued.
        """
        status = getattr(getattr(error, "response", None), "status", None)
        if status is None:
            status = getattr(error, "status", None)
        if status in (400, 422):
            return True
        return any(
            s in err_text
            for s in ("unprocessable", "unable to parse", "partial write", "not finite")
        )

    async def _write_batch_with_semaphore(
        self,
        write_api: "WriteApiAsync",
        batch: list[Point],
        batch_num: int,
        total_batches: int,
    ) -> tuple[list[Point], int, float]:
        """Write a single batch, holding one write-semaphore slot for the whole
        attempt (including any bisection), so overall write concurrency stays
        bounded by max_concurrent_writes.

        Returns (retry_points, written_points, duration_ms):
            retry_points   points to re-queue after a TRANSIENT failure ([] on
                           clean success or on permanent rejection).
            written_points count of points InfluxDB actually accepted.
            duration_ms    latency sample for a clean single-shot write (0 when
                           the batch was bisected or failed).
        """
        async with self._write_semaphore:
            return await self._write_or_bisect(write_api, batch, batch_num, total_batches)

    async def _write_or_bisect(
        self,
        write_api: "WriteApiAsync",
        batch: list[Point],
        batch_num: int,
        total_batches: int,
    ) -> tuple[list[Point], int, float]:
        """Attempt one write; on a permanent 4xx rejection, bisect to isolate and
        drop only the offending point(s). Assumes the caller holds the write
        semaphore (recurses without re-acquiring it)."""
        if not batch:
            return ([], 0, 0.0)
        try:
            write_start = datetime.now(UTC)
            # Serialize to line protocol off the event loop for big batches so a
            # flush doesn't block polling + the heatmap endpoint; pass
            # write_precision=ms since pre-serialized records carry no Point
            # precision for the client to infer.
            if len(batch) >= _SERIALIZE_OFFLOAD_THRESHOLD:
                loop = asyncio.get_running_loop()
                record = await loop.run_in_executor(
                    self._serialize_executor, _serialize_batch, batch
                )
            else:
                record = _serialize_batch(batch)
            await asyncio.wait_for(
                write_api.write(
                    bucket=self.bucket,
                    org=self.org,
                    record=record,
                    write_precision=WritePrecision.MS,
                ),
                timeout=self.write_timeout_ms / 1000.0,
            )
            write_duration_ms = (datetime.now(UTC) - write_start).total_seconds() * 1000
            return ([], len(batch), write_duration_ms)

        except TimeoutError:
            logger.warning(
                f"Timeout writing batch {batch_num}/{total_batches} "
                f"({len(batch)} points) to InfluxDB after {self.write_timeout_ms}ms"
            )
            return (batch, 0, 0.0)
        except Exception as batch_error:
            logger.error(
                f"Error writing batch {batch_num}/{total_batches} "
                f"({len(batch)} points): {type(batch_error).__name__}: {batch_error}"
            )
            err_text = str(batch_error).lower()
            # Diagnose the common, hard-to-root-cause auth failure once.
            if not self._auth_hint_logged and ("401" in err_text or "unauthorized" in err_text):
                self._auth_hint_logged = True
                logger.error(
                    "InfluxDB rejected the token (401 Unauthorized). This usually "
                    "means INFLUXDB_TOKEN no longer matches the token stored in the "
                    "influxdb_data volume (e.g. .env was regenerated or a volume was "
                    "copied/restored). Run './gyanam.sh doctor' to confirm and recover."
                )
            # Permanent rejection (unparseable line protocol / unprocessable
            # entity): retrying the identical batch will fail every cycle. Bisect
            # to isolate the bad point(s) and drop only those — one NaN would
            # otherwise take the whole (up to batch_size) batch of good points
            # with it, silently losing a large share of fleet metrics.
            if self._is_unprocessable(batch_error, err_text):
                return await self._isolate_unprocessable(write_api, batch, batch_num, total_batches)
            return (batch, 0, 0.0)

    async def _isolate_unprocessable(
        self,
        write_api: "WriteApiAsync",
        batch: list[Point],
        batch_num: int,
        total_batches: int,
    ) -> tuple[list[Point], int, float]:
        """Recursively bisect a permanently-rejected batch, writing the good
        halves and dropping only the point(s) InfluxDB won't accept. A good
        sub-batch is accepted whole and never re-sent, so each good point is
        written exactly once; only sub-batches containing a bad point re-split."""
        if len(batch) == 1:
            self._rejected_points += 1
            logger.error(
                "Dropping 1 unprocessable point (batch %d/%d, total rejected: %d): %s "
                "— likely a malformed value (NaN/Inf) or a field type conflict.",
                batch_num,
                total_batches,
                self._rejected_points,
                _describe_point(batch[0]),
            )
            return ([], 0, 0.0)
        mid = len(batch) // 2
        left_retry, left_written, _ = await self._write_or_bisect(
            write_api, batch[:mid], batch_num, total_batches
        )
        right_retry, right_written, _ = await self._write_or_bisect(
            write_api, batch[mid:], batch_num, total_batches
        )
        return (left_retry + right_retry, left_written + right_written, 0.0)

    async def _flush_buffer(self) -> None:
        """Flush the metric buffer to InfluxDB.

        Uses atomic snapshot pattern to prevent race conditions where _write_api
        becomes None between the check and use.
        """
        # Extract points from buffer under lock
        async with self._buffer_lock:
            if not self._buffer:
                return

            points = self._buffer
            self._buffer = []

        # Create atomic snapshot of write_api under connection lock
        # This prevents race condition where another coroutine sets _write_api = None
        # between our check and use
        async with self._connection_lock:
            write_api = self._write_api

        if not write_api:
            logger.warning(f"Cannot flush {len(points)} points: not connected")
            # Re-add points but respect max buffer size
            await self._re_add_points(points)
            return

        try:
            # Write in smaller batches to avoid timeouts
            # Split large buffers into manageable chunks
            batches = [
                points[i : i + self.batch_size] for i in range(0, len(points), self.batch_size)
            ]
            total_batches = len(batches)

            # Write batches concurrently (up to max_concurrent_writes in parallel)
            # This provides significant throughput improvement for high-scale deployments
            tasks = [
                self._write_batch_with_semaphore(write_api, batch, idx + 1, total_batches)
                for idx, batch in enumerate(batches)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            successful_batches = 0
            successful_points = 0
            failed_batches = []
            write_durations = []

            for result, batch in zip(results, batches, strict=False):
                # Handle exceptions raised during gather
                if isinstance(result, Exception):
                    logger.error(
                        f"Unhandled exception in batch write: {type(result).__name__}: {result}",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    failed_batches.append(batch)
                    self._failed_batches += 1
                else:
                    # Type narrowing: result is tuple here, not Exception.
                    # (retry_points, written_points, duration_ms) — a bisected
                    # batch can report BOTH written good points AND a transient
                    # retry remainder; permanently-rejected points are already
                    # tallied in _rejected_points and appear in neither.
                    retry_points, written_points, duration_ms = cast(
                        "tuple[list, int, float]", result
                    )
                    if written_points:
                        successful_batches += 1
                        successful_points += written_points
                        self._successful_batches += 1
                        if duration_ms:
                            write_durations.append(duration_ms)
                    if retry_points:
                        failed_batches.append(retry_points)
                        self._failed_batches += 1

            # Update moving average of write latency using successful writes
            if write_durations:
                avg_duration = sum(write_durations) / len(write_durations)
                if self._write_latency_ms == 0:
                    self._write_latency_ms = avg_duration
                else:
                    # 10% weight to new sample, 90% to historical average
                    self._write_latency_ms = (0.9 * self._write_latency_ms) + (0.1 * avg_duration)

            # Track successful writes
            if successful_batches > 0:
                # ANY successful batch resets the consecutive-failure counter.
                self._consecutive_batch_failures = 0
                self._write_count += 1
                self._total_points_written += successful_points
                self._last_write_time = datetime.now(UTC)

                # Log at INFO level with summary every 10 writes
                if self._write_count % 10 == 0:
                    logger.info(
                        f"InfluxDB export: batch #{self._write_count}, "
                        f"{successful_points} points written, "
                        f"{len(failed_batches)} batches failed, "
                        f"avg latency: {self._write_latency_ms:.1f}ms, "
                        f"total: {self._total_points_written} points, "
                        f"dropped: {self._dropped_points}"
                    )
                else:
                    logger.debug(
                        f"Flushed {successful_points} metrics to InfluxDB "
                        f"(latency: {self._write_latency_ms:.1f}ms)"
                    )

            # Re-add failed batches for retry, and bump the failure counter.
            if failed_batches:
                failed_points = [p for batch in failed_batches for p in batch]
                self._write_failures_24h += len(failed_batches)
                # Treat the whole flush as failing only if NOTHING got through.
                # A partial failure shouldn't force a reconnect, but full failure
                # of every batch in this flush almost certainly means the
                # connection (or InfluxDB) is unhealthy.
                if successful_batches == 0:
                    self._consecutive_batch_failures += 1
                logger.warning(
                    f"Re-queuing {len(failed_points)} points from {len(failed_batches)} "
                    f"failed batches for retry "
                    f"(consecutive_full_flush_failures={self._consecutive_batch_failures})"
                )
                await self._re_add_points(failed_points)

        except Exception as e:
            logger.error(f"Failed to flush metrics: {type(e).__name__}: {e}", exc_info=True)
            self._write_failures_24h += 1
            self._consecutive_batch_failures += 1
            # Re-add all points to buffer for retry, respecting max size
            await self._re_add_points(points)

        # If we've failed enough times in a row, force a reconnect. Without
        # this the client object can stay "connected" while every write fails,
        # so writes stop without the client ever reporting itself unhealthy.
        if self._consecutive_batch_failures >= _CONSECUTIVE_FAILURE_RECONNECT_THRESHOLD:
            logger.warning(
                f"Forcing InfluxDB reconnect after "
                f"{self._consecutive_batch_failures} consecutive failed flushes"
            )
            await self._force_reset_connection()

    async def _re_add_points(self, points: list[Point]) -> None:
        """Re-add points to buffer with size limit to prevent unbounded growth.

        Args:
            points: Points to re-add to the buffer
        """
        async with self._buffer_lock:
            combined = points + self._buffer
            if len(combined) > self._max_buffer_size:
                # Drop oldest points to stay within limit
                dropped = len(combined) - self._max_buffer_size
                self._dropped_points += dropped
                self._buffer = combined[-self._max_buffer_size :]
                logger.warning(
                    f"Buffer overflow: dropped {dropped} oldest points "
                    f"(total dropped: {self._dropped_points})"
                )
            else:
                self._buffer = combined

    def _metric_to_point(self, metric: Metric) -> Point:
        """Convert a Metric to an InfluxDB Point.

        Args:
            metric: Metric object to convert

        Returns:
            InfluxDB Point object
        """
        point = Point(metric.name)

        # Add tags
        for tag_name, tag_value in metric.tags.items():
            point.tag(tag_name, tag_value)

        # Add metadata tags
        if metric.unit:
            point.tag("unit", metric.unit)
        point.tag("metric_type", metric.metric_type)

        # Add the value field
        point.field("value", metric.value)

        # Set timestamp
        point.time(metric.timestamp, WritePrecision.MS)

        return point

    async def health_check(self) -> tuple[bool, str]:
        """Check if connected to InfluxDB.

        Returns:
            Tuple of (is_healthy, message)
        """
        if not self._client:
            return False, "Not connected"

        try:
            ready = await self._client.ping()
            if ready:
                return True, f"Connected to InfluxDB at {self.url}"
            else:
                return False, "InfluxDB ping failed"
        except Exception as e:
            # Log full exception locally; return only the type so the public
            # health endpoint doesn't expose stack-trace-style details.
            logger.warning(f"InfluxDB health check failed: {e}", exc_info=True)
            return False, f"InfluxDB health check failed ({type(e).__name__})"

    async def query(self, flux_query: str) -> list[dict]:
        """Execute a Flux query.

        Args:
            flux_query: Flux query string

        Returns:
            List of result records as dictionaries
        """
        if not self._client:
            raise RuntimeError("Not connected to InfluxDB")

        query_api = self._client.query_api()
        tables = await query_api.query(flux_query)

        results = []
        for table in tables:
            for record in table.records:
                results.append(record.values)

        return results

    @property
    def buffer_size(self) -> int:
        """Get the current buffer size."""
        return len(self._buffer)

    @property
    def dropped_points(self) -> int:
        """Get the total number of dropped points due to buffer overflow."""
        return self._dropped_points

    @property
    def is_connected(self) -> bool:
        """Check if connected to InfluxDB."""
        return self._client is not None

    def get_health_metrics(self) -> dict:
        """Get export health metrics for monitoring.

        Returns:
            Dictionary with health metrics

        Note: This is a synchronous method, so we cannot use async locks.
        However, reading _write_api for a None check is atomic in Python,
        so this is safe for read-only health reporting.
        """

        now = datetime.now(UTC)

        # Calculate time since last write
        time_since_write_seconds = None
        if self._last_write_time:
            time_since_write_seconds = (now - self._last_write_time).total_seconds()

        # Calculate buffer utilization
        buffer_utilization_pct = (len(self._buffer) / self._max_buffer_size) * 100

        # Connection state. The bare object check ("write_api is not None") is
        # not enough: that object can stay alive indefinitely while no writes
        # are landing. So we also require a recent successful write (or no
        # writes attempted yet).
        write_api_alive = self._write_api is not None
        pipeline_recent = (
            self._last_write_time is None
            or time_since_write_seconds is not None
            and time_since_write_seconds < _PIPELINE_DEAD_AFTER_S
        )
        is_connected = write_api_alive and pipeline_recent

        # Calculate intelligent health metrics
        total_batches = self._successful_batches + self._failed_batches
        failure_rate_pct = (self._failed_batches / total_batches * 100) if total_batches > 0 else 0
        # Rejected (dropped-as-unprocessable) points as a fraction of all points
        # the pipeline tried to persist. A small rate is tolerable (one bad BMC
        # value); a sustained high rate is a real, otherwise-invisible data-loss
        # signal, so it degrades performance health.
        attempted_points = self._total_points_written + self._rejected_points
        rejected_rate_pct = (
            (self._rejected_points / attempted_points * 100) if attempted_points > 0 else 0
        )

        # Intelligent health assessment based on multiple factors
        # A system is healthy if it meets ALL critical criteria and MOST performance criteria
        critical_checks = {
            "connected": is_connected,
            "writes_recent": pipeline_recent,
            "no_data_loss": self._dropped_points == 0,  # Zero tolerance for data loss
            "buffer_not_full": buffer_utilization_pct < 95,  # Critical threshold
        }

        performance_checks = {
            "low_failure_rate": failure_rate_pct <= 2.0,  # <= 2% failure rate is acceptable
            "acceptable_latency": self._write_latency_ms < 3000,  # 3s threshold (relaxed)
            "buffer_healthy": buffer_utilization_pct < 80,  # Ideal threshold
            "minimal_failures": self._write_failures_24h < 50,  # Scaled for volume
            "low_rejected_rate": rejected_rate_pct <= 1.0,  # dropped-unprocessable visibility
        }

        # Critical checks must ALL pass
        critical_healthy = all(critical_checks.values())

        # Performance checks - tolerate at most one failing check.
        performance_score = sum(performance_checks.values())
        performance_healthy = performance_score >= len(performance_checks) - 1

        # Overall health: critical checks pass AND performance is acceptable
        is_healthy = critical_healthy and performance_healthy

        # Detailed health status for debugging
        health_details = {
            "critical_checks": critical_checks,
            "performance_checks": performance_checks,
            "performance_score": f"{performance_score}/{len(performance_checks)}",
            "write_api_alive": write_api_alive,
            "consecutive_batch_failures": self._consecutive_batch_failures,
            "reconnects": self._reconnect_count,
        }

        return {
            "connected": is_connected,
            "last_write_time": self._last_write_time.isoformat() if self._last_write_time else None,
            "time_since_last_write_seconds": time_since_write_seconds,
            "total_batches_written": self._write_count,
            "total_points_written": self._total_points_written,
            "total_points_dropped": self._dropped_points,
            "total_points_rejected": self._rejected_points,
            "write_failures_24h": self._write_failures_24h,
            "successful_batches": self._successful_batches,
            "failed_batches": self._failed_batches,
            "failure_rate_pct": round(failure_rate_pct, 2),
            "avg_write_latency_ms": round(self._write_latency_ms, 2),
            "buffer_size": len(self._buffer),
            "buffer_max_size": self._max_buffer_size,
            "buffer_utilization_pct": round(buffer_utilization_pct, 2),
            "is_healthy": is_healthy,
            "health_details": health_details,
        }
