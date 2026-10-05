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
"""Collector service - Background data collection and metric export.

This process runs independently from the API and handles:
- Continuous polling of Redfish targets
- Metric extraction and export to InfluxDB
- SSE event subscriptions
- Alert management
- Background cleanup tasks
- Internal health monitoring endpoint

The collector shares the SQLite database (in WAL mode) with the API process.
"""

import asyncio
import hmac
import json
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import (
    INTERNAL_AUTH_HEADER,
    get_config,
    get_settings,
    internal_service_token,
)
from .database.repository import TargetRepository
from .exporters.base import BaseExporter, Metric
from .parser.discovery import DiscoveredMetric, MetricDiscovery
from .parser.extractor import ExtractedMetric, MetricExtractor
from .parser.schema import SchemaLoader
from .parser.unpacker import BlobUnpacker
from .redfish.poller import PollResult, RedfishPoller
from .redfish.sse_subscriber import SSEManager

# Max accepted webhook body size (1 MiB) — a Redfish Event payload is small;
# anything larger is rejected before parsing.
_WEBHOOK_MAX_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)

# How long shutdown waits for the result processor to drain its in-flight batch
# (so the final cycle's metrics reach the exporter) before backstop-cancelling it.
_PROCESSOR_DRAIN_TIMEOUT_S = 15.0


async def _init_sharding(config, settings, repository, collector_id):
    """Claim this collector's target slice and emit operability warnings.

    Returns ``(shard_manager, effective_id)``: a started ``ShardManager`` (lease
    context set, first claim done) plus the id the collector actually runs under
    (dynamic mode may slot-claim a stable ``collector-<ordinal>`` that differs from
    the passed hostname). When sharding is disabled, returns ``(None, collector_id)``
    so the caller keeps the single-collector id for stats/heatmap publishing.
    """
    if not config.sharding.enabled:
        return None, collector_id

    from .shard_manager import ShardManager

    cap = config.sharding.max_targets_per_shard
    ttl = config.sharding.lease_ttl_seconds
    dynamic = config.sharding.balance == "dynamic"
    slot_token = None
    slot_ordinal = None
    effective_id = collector_id

    if settings.collector_id:
        # Explicitly pinned id (e.g. k8s StatefulSet pod name) — stable by
        # construction; use it directly in either mode.
        effective_id = settings.collector_id
    elif dynamic:
        # Dynamic (HRW) keys ownership on the collector id, so it must be STABLE
        # across restarts, not merely unique. The hostname is unique but changes on
        # recreate, so claim the lowest free ordinal and derive a stable id. The
        # token is the (unique-per-process) hostname.
        slot_token = collector_id
        slot_ordinal = await repository.claim_collector_slot(slot_token, ttl)
        effective_id = f"collector-{slot_ordinal}"
        logger.info(
            "Dynamic sharding: claimed stable slot %d -> COLLECTOR_ID '%s' (token '%s')",
            slot_ordinal,
            effective_id,
            slot_token,
        )
    else:
        # Static mode's id-order claiming tolerates an unstable id; only warn about
        # uniqueness (a shared id would double-poll).
        logger.warning(
            "Sharding enabled but COLLECTOR_ID is unset — using hostname '%s'. Static "
            "mode needs it unique (hostname is unique under docker --scale / k8s); for "
            "dynamic mode a STABLE id is auto-assigned instead.",
            effective_id,
        )

    repository.set_shard_context(effective_id, ttl)
    shard_manager = ShardManager(
        repository,
        effective_id,
        config.sharding,
        slot_token=slot_token,
        slot_ordinal=slot_ordinal,
    )

    # Publish an initial membership row BEFORE the first claim so peers count this
    # newcomer in their fair-share denominator within one stats interval (speeds
    # dynamic convergence on scale-up).
    if dynamic:
        with suppress(Exception):
            await repository.upsert_collector_stats(effective_id, {}, 0)

    owned = await shard_manager.claim_once()
    enabled_total = len(await repository.get_enabled_target_ids())
    logger.info(
        "Sharding enabled (%s): collector '%s' owns %d/%d enabled target(s) (cap %d)",
        config.sharding.balance,
        effective_id,
        owned,
        enabled_total,
        cap,
    )
    # Static mode keeps the startup under-provision one-shot (dynamic mode checks
    # this every renew pass on the live set — see ShardManager._warn_if_under_
    # provisioned — so it self-clears after cold start instead of false-alarming).
    if not dynamic and owned >= cap:
        total_leased = await repository.count_all_shard_leases(ttl)
        if total_leased < enabled_total:
            logger.warning(
                "Fleet under-provisioned: %d enabled targets but only %d leased across "
                "all collectors (this replica is at its cap of %d). ~%d targets are "
                "unpolled — add collector replicas or raise MAX_TARGETS_PER_SHARD so "
                "replicas x cap >= fleet size.",
                enabled_total,
                total_leased,
                cap,
                enabled_total - total_leased,
            )
    return shard_manager, effective_id


# Global references for health endpoint and manual poll access
_exporter = None
_poller = None
_sse_manager = None
_alert_manager = None
_unpacker = None
_extractor = None
_discovery = None


class UTCFormatter(logging.Formatter):
    """Custom formatter that uses UTC time for all log timestamps."""

    converter = time.gmtime  # type: ignore[assignment]


def _sync_extract_metrics(
    result: PollResult,
    unpacker: BlobUnpacker,
    extractor: MetricExtractor,
    discovery: MetricDiscovery,
) -> tuple[list[Metric], list]:
    """Synchronous CPU/IO-bound work: unpack, parse, extract metrics.

    Designed to be called via asyncio.to_thread() to avoid blocking the
    event loop.

    Returns:
        Tuple of (list of Metric objects ready for export, list of extracted files for cleanup)
    """
    all_metrics: list[ExtractedMetric | DiscoveredMetric] = []
    # Stamp samples with when the poll actually ran, not when this (possibly
    # backlogged) extraction thread executes. Under backpressure the two can
    # differ by seconds-to-minutes, which would skew/reorder series in InfluxDB.
    timestamp = getattr(result, "poll_time", None) or datetime.now(UTC)

    extra_tags = {"target_name": result.target_name}
    if result.target_tags:
        extra_tags.update(result.target_tags)

    schema_keys = set()
    for schema in extractor.schema_loader.get_schemas():
        for field_def in schema.fields:
            schema_keys.add(field_def.json_key)

    # Fast path: pre-parsed JSON from GET
    if result.data is not None:
        seen_properties: set[str] = set()

        for report_type, report_json in result.data:
            # Guard EACH report independently: a BMC can return a non-dict body or
            # a null/garbage MetricValues on one of six reports. Without this, that
            # one bad report raises out of the whole loop and drops ALL of this
            # target's metrics for the cycle — while the poll is still recorded a
            # success, so the node looks green while going dark. One bad report
            # should cost one report.
            try:
                if not isinstance(report_json, dict):
                    raise TypeError(f"report body is {type(report_json).__name__}, expected dict")
                metric_values = report_json.get("MetricValues", [])
                if not isinstance(metric_values, list):
                    raise TypeError(
                        f"MetricValues is {type(metric_values).__name__}, expected list"
                    )

                # Dedup: first report to claim a MetricProperty wins. Skip non-dict
                # entries defensively.
                unique_values = [
                    mv
                    for mv in metric_values
                    if isinstance(mv, dict)
                    and (
                        not mv.get("MetricProperty") or mv["MetricProperty"] not in seen_properties
                    )
                ]
                seen_properties.update(
                    mv["MetricProperty"] for mv in unique_values if mv.get("MetricProperty")
                )

                if not unique_values:
                    logger.debug(f"Report '{report_type}' fully deduped for {result.target_name}")
                    continue

                deduped_data = {**report_json, "MetricValues": unique_values}
                report_tags = {**extra_tags, "report_type": report_type}

                extracted = extractor.extract_from_data(
                    data=deduped_data,
                    host=result.target_host,
                    extra_tags=report_tags,
                    timestamp=timestamp,
                )
                all_metrics.extend(extracted)

                try:
                    discovered = discovery.discover(
                        data=deduped_data,
                        host=result.target_host,
                        extra_tags=report_tags,
                        exclude_keys=schema_keys,
                        timestamp=timestamp,
                    )
                    all_metrics.extend(discovered)
                except Exception as e:
                    logger.warning(
                        f"Auto-discovery failed for {report_type} report from "
                        f"{result.target_name}: {e}"
                    )
            except Exception as e:  # noqa: BLE001 — one bad report must not drop the rest
                logger.warning(
                    f"Skipping malformed '{report_type}' report from "
                    f"{result.target_name}: {type(e).__name__}: {e}"
                )
                continue

        logger.debug(
            f"Dedup stats for {result.target_name}: "
            f"{len(seen_properties)} unique MetricProperties across {len(result.data)} reports"
        )

        metrics = [
            Metric(
                name=m.name,
                value=m.value,
                timestamp=m.timestamp,
                tags=m.tags,
                metric_type=m.metric_type,
                unit=m.unit,
            )
            for m in all_metrics
        ]
        return metrics, []

    # Slow path: blob unpacking
    if not result.content:
        return [], []

    from .parser.unpacker import ExtractedFile

    files: list[ExtractedFile] = []
    try:
        files = unpacker.unpack(result.content, result.target_name)
        if not files:
            logger.warning(f"No files extracted from blob for {result.target_name}")
            return [], files

        for extracted_file in files:
            report_tags = {**extra_tags, "report_type": extracted_file.report_type}

            # Parse the extracted file as a Redfish MetricReport JSON. Non-JSON
            # files in a diagnostic bundle are skipped (no metrics to extract).
            try:
                with open(extracted_file.path, encoding="utf-8") as fh:
                    file_data = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                logger.debug(
                    f"Skipping non-JSON file {extracted_file.original_name} "
                    f"from {result.target_name}: {e}"
                )
                continue

            extracted = extractor.extract_from_data(
                data=file_data,
                host=result.target_host,
                extra_tags=report_tags,
                timestamp=timestamp,
            )
            all_metrics.extend(extracted)

            try:
                discovered = discovery.discover(
                    data=file_data,
                    host=result.target_host,
                    extra_tags=report_tags,
                    exclude_keys=schema_keys,
                    timestamp=timestamp,
                )
                all_metrics.extend(discovered)
            except Exception as e:
                logger.warning(
                    f"Auto-discovery failed for {extracted_file.report_type} from {result.target_name}: {e}"
                )

        metrics = [
            Metric(
                name=m.name,
                value=m.value,
                timestamp=m.timestamp,
                tags=m.tags,
                metric_type=m.metric_type,
                unit=m.unit,
            )
            for m in all_metrics
        ]
        return metrics, files

    except Exception as e:
        logger.error(f"Error extracting metrics from {result.target_name}: {e}", exc_info=True)
        return [], files


async def process_poll_result(
    result: PollResult,
    unpacker: BlobUnpacker,
    extractor: MetricExtractor,
    discovery: MetricDiscovery,
    exporter: BaseExporter,
) -> int:
    """Process a single poll result: extract metrics and export them."""
    if not result.success or (not result.content and result.data is None):
        return 0

    from .parser.unpacker import ExtractedFile

    files: list[ExtractedFile] = []
    try:
        # Run synchronous I/O and CPU-bound work in a thread
        metrics, files = await asyncio.to_thread(
            _sync_extract_metrics, result, unpacker, extractor, discovery
        )

        if not metrics:
            logger.warning(f"No metrics extracted from {result.target_name}")
            return 0

        # Buffer metrics for export
        await exporter.write(metrics)

        if exporter.is_connected:
            logger.info(f"Exported {len(metrics)} metrics from {result.target_name}")
        else:
            logger.info(
                f"Buffered {len(metrics)} metrics from {result.target_name} "
                f"(InfluxDB disconnected, will flush on reconnect)"
            )
        return len(metrics)

    except Exception as e:
        logger.error(f"Error processing result from {result.target_name}: {e}", exc_info=True)
        return 0
    finally:
        # Always cleanup extracted files
        if files:
            unpacker.cleanup(files)


async def result_processor_task(
    poller: RedfishPoller,
    unpacker: BlobUnpacker,
    extractor: MetricExtractor,
    discovery: MetricDiscovery,
    exporter: BaseExporter,
    max_workers: int = 4,
    stop_event: asyncio.Event | None = None,
):
    """Background task to process poll results in parallel.

    On ``stop_event`` (set at shutdown, after producers have stopped), the loop
    keeps processing until the queue is fully drained, then returns — so the
    final cycle's metrics are extracted and buffered before teardown flushes.
    """
    semaphore = asyncio.Semaphore(max_workers)

    async def _process_one(result: PollResult):
        async with semaphore:
            await process_poll_result(result, unpacker, extractor, discovery, exporter)

    while True:
        try:
            results = await poller.get_results(timeout=1.0)
            if results:
                await asyncio.gather(*[_process_one(r) for r in results], return_exceptions=True)
            elif stop_event is not None and stop_event.is_set():
                # Producers are stopped and the queue drained to empty — safe to exit.
                break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in result processor: {e}")
            await asyncio.sleep(1)


async def cleanup_task(unpacker: BlobUnpacker, cleanup_interval: int, max_age_seconds: int):
    """Background task to periodically clean up old temp files."""
    while True:
        try:
            await asyncio.sleep(cleanup_interval)
            cleaned = unpacker.cleanup_old_files(max_age_seconds)
            if cleaned > 0:
                logger.info(f"Cleanup task removed {cleaned} old temp files/directories")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")


async def heatmap_snapshot_task(repository, interval: float = 20.0, collector_id: str = "") -> None:
    """Publish the in-memory heatmap cache to shared SQLite on a short interval.

    Lets the API serve the Data Hall heatmap from a fast local read instead of
    calling this process's health port, whose latency spikes during InfluxDB
    flushes. A tiny write (a few hundred host:value entries per metric). Keyed by
    collector_id so shards publish their own slice without clobbering.
    """
    from .metrics_cache import HEATMAP, HEATMAP_METRICS

    while True:
        try:
            for key in HEATMAP_METRICS:
                await repository.upsert_heatmap_snapshot(
                    key, HEATMAP.latest(key), collector_id=collector_id
                )
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001 — never let a publish error kill the loop
            logger.error("Heatmap snapshot publish failed: %s: %s", type(e).__name__, e)
        await asyncio.sleep(interval)


async def collector_stats_task(
    repository, collector_id, alert_manager, shard_manager, interval: float = 30.0
) -> None:
    """Publish this collector's stats to shared SQLite so the API can aggregate
    across shards (it can't HTTP-poll one of N collectors for a fleet view).

    Always runs — single-collector deployments publish one row the API reads
    directly, and sharded deployments publish one row per collector.
    """
    while True:
        try:
            stats = alert_manager.get_stats() if (alert_manager and alert_manager.enabled) else {}
            if shard_manager is not None:
                owned = shard_manager.owned_count
            else:
                owned = len(await repository.get_active_targets())
            await repository.upsert_collector_stats(collector_id, stats, owned)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001 — never let a publish error kill the loop
            logger.error("Collector stats publish failed: %s: %s", type(e).__name__, e)
        await asyncio.sleep(interval)


def create_health_app() -> FastAPI:
    """Create a minimal FastAPI app for health monitoring."""
    app = FastAPI(title="Collector Health API", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def basic_health():
        """Basic health check."""
        return {"status": "healthy", "service": "collector"}

    @app.get("/health/detailed")
    async def detailed_health():
        """Detailed health check with collector component status."""

        # Check exporter
        exporter_healthy = False
        exporter_message = "Not initialized"
        influxdb_metrics = {}

        if _exporter:
            try:
                exporter_healthy, exporter_message = await _exporter.health_check()
                if hasattr(_exporter, "get_health_metrics"):
                    influxdb_metrics = _exporter.get_health_metrics()
            except Exception as e:
                # Log the full exception; expose only the type name in the
                # HTTP response so we don't leak internal stack details.
                logger.warning(f"Exporter health check failed: {e}", exc_info=True)
                exporter_message = f"health-check error ({type(e).__name__})"
                exporter_healthy = False

        # Check poller. "running" is the lifecycle flag; "progress" is liveness
        # (is the loop actually completing polls?) — a stalled loop still reads
        # running, so health must consider progress, not just the flag.
        poller_status = "not_initialized"
        poller_info = {}
        poller_progressing = False
        if _poller:
            poller_status = "running" if _poller.is_running else "stopped"
            if hasattr(_poller, "get_stats"):
                with suppress(Exception):
                    poller_info = _poller.get_stats()
            if hasattr(_poller, "is_making_progress"):
                with suppress(Exception):
                    poller_progressing = _poller.is_making_progress()
            else:
                poller_progressing = _poller.is_running

        # Check SSE manager
        sse_status = "not_initialized"
        if _sse_manager:
            sse_status = "running"

        # Check alert manager
        alert_status = "disabled"
        alert_info = {}
        if _alert_manager:
            try:
                alert_status = "enabled"
                if hasattr(_alert_manager, "get_stats"):
                    alert_info = _alert_manager.get_stats()
                # Report alert-store (PostgreSQL) reachability.
                repo = getattr(_alert_manager, "repository", None)
                if repo is not None and hasattr(repo, "ping_alert_store"):
                    alert_info["alert_store_connected"] = await repo.ping_alert_store()
            except Exception as e:
                # Don't expose raw exception text on the health endpoint;
                # type-name only — the full stack is logged separately.
                logger.warning(f"Alert manager stats failed: {e}", exc_info=True)
                alert_status = f"error: {type(e).__name__}"

        # Fold the alert subsystem into overall health: when alerts are enabled,
        # a down alert store (Postgres) means ingestion is failing silently — the
        # service should report degraded rather than healthy.
        alert_healthy = True
        if _alert_manager and alert_status == "enabled":
            store_connected = alert_info.get("alert_store_connected")
            if store_connected is False:
                alert_healthy = False
        elif isinstance(alert_status, str) and alert_status.startswith("error:"):
            alert_healthy = False

        # The exporter's own ping (`exporter_healthy`) stays True while the
        # client object is alive even if no write has landed for a long time (a
        # live client doesn't prove the pipeline is writing). So the TOP-LINE
        # status folds in the exporter's CRITICAL integrity checks
        # (connected, writes_recent,
        # no_data_loss, buffer_not_full) — the cases that mean the pipeline is
        # genuinely failing or losing data. Pure PERFORMANCE degradation (high
        # InfluxDB write latency, transient batch failures that auto-recover
        # without dropping points) is reported via `performance_degraded` and
        # the influxdb_export detail block, NOT as a top-line "degraded" — a
        # slow-but-lossless pipeline is still doing its job.
        pipeline_healthy = exporter_healthy
        performance_degraded = False
        if isinstance(influxdb_metrics, dict):
            critical = influxdb_metrics.get("health_details", {}).get("critical_checks")
            if isinstance(critical, dict):
                pipeline_healthy = exporter_healthy and all(critical.values())
            # Critical is fine but the exporter's combined is_healthy is False ->
            # performance-only degradation (latency / transient failure rate).
            if pipeline_healthy and influxdb_metrics.get("is_healthy") is False:
                performance_degraded = True

        overall_healthy = (
            pipeline_healthy and poller_status == "running" and poller_progressing and alert_healthy
        )

        return {
            "status": "healthy" if overall_healthy else "degraded",
            "performance_degraded": performance_degraded,
            "service": "collector",
            "metrics_backend": "influxdb",
            "components": {
                "exporter": {
                    "healthy": exporter_healthy,
                    "message": exporter_message,
                    "backend": "influxdb",
                },
                "influxdb_export": influxdb_metrics,
                "poller": {"status": poller_status, **poller_info},
                "sse_manager": {"status": sse_status},
                "alert_manager": {"status": alert_status, **alert_info},
            },
        }

    @app.get("/alerts/manager-stats")
    async def get_alert_manager_stats():
        """Get alert manager statistics for API service."""

        if not _alert_manager or not _alert_manager.enabled:
            return {"enabled": False, "subscribers": []}

        try:
            stats = _alert_manager.get_stats()
            return {"enabled": True, **stats}
        except Exception as e:
            logger.warning(f"Alert manager get_stats failed: {e}", exc_info=True)
            return {
                "enabled": False,
                "error": f"stats unavailable ({type(e).__name__})",
                "subscribers": [],
            }

    @app.post("/poll/{target_id}")
    async def trigger_manual_poll(target_id: int, request: Request):
        """Trigger an immediate poll of a target from API service request.

        This endpoint mutates state (forces a BMC poll), so it requires the
        shared internal token. The control server listens on the internal
        network only, but we still authenticate to prevent any other container
        from driving arbitrary polls (BMC DoS / SSRF-ish amplification).
        """

        expected = internal_service_token()
        if expected is not None and not hmac.compare_digest(
            request.headers.get(INTERNAL_AUTH_HEADER, ""), expected
        ):
            return JSONResponse(status_code=403, content={"error": "forbidden"})

        if not _poller:
            return JSONResponse(
                status_code=503,
                content={"success": False, "error_message": "Poller not initialized"},
            )

        # Trigger the poll
        result = await _poller.poll_single(target_id)

        if not result:
            return JSONResponse(
                status_code=404, content={"success": False, "error_message": "Target not found"}
            )

        # Process result through the pipeline (unpack -> extract -> export)
        metrics_count = 0
        if (
            result.success
            and (result.content or result.data is not None)
            and _unpacker
            and _extractor
            and _discovery
            and _exporter
        ):
            try:
                metrics_count = await process_poll_result(
                    result, _unpacker, _extractor, _discovery, _exporter
                )
            except Exception as e:
                logger.error(f"Error processing poll result: {e}")

        # Calculate content size
        if result.data is not None:
            content_size = sum(len(json.dumps(d)) for _, d in result.data)
        else:
            content_size = len(result.content) if result.content else 0

        return {
            "success": result.success,
            "content_size": content_size,
            "collection_method": result.collection_method,
            "duration_ms": result.duration_ms,
            "error_message": result.error_message,
            "metrics_exported": metrics_count,
        }

    @app.post("/redfish-webhook/{target_id}")
    async def redfish_webhook_receiver(target_id: int, request: Request):
        """Receive webhook events from Redfish BMCs.

        This endpoint receives HTTP POST requests from BMCs when events occur.
        The BMC must have a subscription configured pointing to this endpoint.

        Args:
            target_id: Target database ID
            request: FastAPI request containing event payload

        Returns:
            HTTP 200 OK to acknowledge receipt
        """

        # target_id is an int (FastAPI path-param coercion); using deferred
        # %-style logging both follows best practice and gives CodeQL a clear
        # signal that no user-controlled text is interpolated into the message.
        tid = int(target_id)

        if not _alert_manager:
            logger.warning("Received webhook for target %d but alert manager not initialized", tid)
            return {"status": "error", "message": "Alert manager not initialized"}

        try:
            # Bound payload size before parsing so a malicious/broken BMC can't
            # OOM the receiver with a giant body.
            raw = await request.body()
            if len(raw) > _WEBHOOK_MAX_BYTES:
                logger.warning(
                    "Webhook from target %d rejected: body %d bytes exceeds cap %d",
                    tid,
                    len(raw),
                    _WEBHOOK_MAX_BYTES,
                )
                return {"status": "error", "message": "payload too large"}

            event_data = json.loads(raw) if raw else {}
            if not isinstance(event_data, dict):
                return {"status": "error", "message": "invalid payload"}

            # Forward to alert manager; it verifies the Context token and parses.
            processed = await _alert_manager.process_webhook_event(tid, event_data)

            return {"status": "ok", "events_received": processed}

        except Exception as e:
            # event_data is BMC-controlled, so its exception text may contain
            # arbitrary characters. exc_info captures the full traceback
            # safely; the bare message goes through logging's own escaping.
            logger.error(
                "Error processing webhook from target %d: %s",
                tid,
                type(e).__name__,
                exc_info=True,
            )
            # Keep "message" key for shape-consistency with the
            # not-initialized branch above; value is the exception type only
            # (BMC-controlled exception text never leaves the server). Still
            # 200 OK to prevent retry storms from misbehaving BMCs.
            return {
                "status": "error",
                "message": f"processing failed ({type(e).__name__})",
            }

    return app


class _NoSignalServer(uvicorn.Server):
    """uvicorn Server that does NOT capture process signals.

    CRITICAL: uvicorn's default ``capture_signals()`` runs in the main thread
    (where this server lives, as an asyncio task) and overwrites SIGTERM/SIGINT
    with its own handlers via ``signal.signal``. That would clobber the
    collector's handlers, routing shutdown to uvicorn (which stops only the
    health server) and leaving the collector's shutdown_event unset — so
    poller.stop()/exporter flush/DB close never run and buffered metrics are
    lost. No-op'ing signal capture lets the collector own shutdown; the health
    server stops when its task is cancelled during the collector's teardown.
    """

    @contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


async def run_health_server(app: FastAPI, port: int = 8081):
    """Run the health monitoring HTTP server."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="error",  # Minimal logging for health server
        access_log=False,
    )
    server = _NoSignalServer(config)
    await server.serve()


async def _shutdown_collector(
    *,
    poller=None,
    sse_manager=None,
    alert_manager=None,
    exporter=None,
    repository=None,
    extract_pool=None,
    processor_task=None,
    processor_stop_event=None,
    cleanup_bg_task=None,
    health_server_task=None,
    inventory_task=None,
    heatmap_snapshot_bg_task=None,
    shard_manager=None,
    shard_bg_task=None,
    collector_stats_bg_task=None,
) -> None:
    """Ordered, best-effort teardown. Safe to call with any subset of resources
    (partial startup): every step is guarded so one failure can't skip the rest.

    Order matters for data integrity:
      1. Stop producers (poller, SSE) so nothing new enters the result queue.
      2. Let the still-running processor drain the queue it already holds, THEN
         cancel it — otherwise the final cycle's metrics are dropped.
      3. Stop remaining background tasks.
      4. Flush+close the exporter (writes the drained buffer), then the DB, then
         the extraction pool.
    """
    logger.info("Collector service shutting down...")

    # 1. Stop producers first.
    if poller is not None:
        with suppress(Exception):
            await poller.stop()
    if sse_manager is not None:
        with suppress(Exception):
            await sse_manager.stop()

    # 2. Ask the processor to stop *cooperatively*: it finishes the batch it is
    #    currently extracting and drains anything already enqueued (producers are
    #    stopped, so the queue cannot grow), then returns on its own. Awaiting its
    #    natural completion — rather than cancelling on a qsize()==0 snapshot —
    #    guarantees the final cycle's metrics reach the exporter buffer before we
    #    flush. Cancel only as a backstop if it overruns the drain budget.
    if processor_task is not None:
        if processor_stop_event is not None:
            processor_stop_event.set()
            try:
                await asyncio.wait_for(processor_task, timeout=_PROCESSOR_DRAIN_TIMEOUT_S)
            except (TimeoutError, Exception):  # noqa: BLE001 — backstop-cancel below
                processor_task.cancel()
        else:
            processor_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await processor_task

    # 3. Stop the remaining background tasks.
    for task in (
        cleanup_bg_task,
        health_server_task,
        inventory_task,
        heatmap_snapshot_bg_task,
        shard_bg_task,
        collector_stats_bg_task,
    ):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    # Release shard leases so surviving collectors reclaim this shard's targets
    # immediately rather than waiting out the TTL.
    if shard_manager is not None:
        await shard_manager.release()

    if alert_manager is not None:
        with suppress(Exception):
            await alert_manager.stop()

    # 4. Flush+close the exporter (buffer now includes the drained cycle).
    if exporter is not None:
        with suppress(Exception):
            await exporter.close()
    if repository is not None:
        with suppress(Exception):
            await repository.close()
    if extract_pool is not None:
        with suppress(Exception):
            extract_pool.shutdown(wait=True)

    logger.info("Collector service shutdown complete")


async def run_collector():
    """Main collector service loop."""
    global _exporter, _poller, _sse_manager, _alert_manager, _unpacker, _extractor, _discovery

    config = get_config()
    settings = get_settings()

    logger.info("Starting GPU Metrics Collector Service...")
    logger.info("This process handles background data collection and metric export")

    # Install shutdown signalling BEFORE the long init (DB-readiness retry alone
    # can take ~60s). With uvicorn's own signal handlers disabled (see
    # run_health_server), the collector owns SIGTERM/SIGINT; handling them early
    # means a SIGTERM during startup sets the event and leads to a graceful exit
    # through the finally block, not an abrupt default-handler termination.
    shutdown_event = asyncio.Event()
    try:
        _loop = asyncio.get_running_loop()
        for _sig in (signal.SIGINT, signal.SIGTERM):
            _loop.add_signal_handler(_sig, shutdown_event.set)
    except (NotImplementedError, RuntimeError):
        # add_signal_handler is unavailable on some platforms (e.g. Windows);
        # fall back to signal.signal.
        def _signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating shutdown...")
            shutdown_event.set()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    # Resource holders — pre-declared so the finally block can tear down whatever
    # was successfully created even when startup fails partway through.
    extract_pool = None
    repository = None
    exporter = None
    poller = None
    sse_manager = None
    alert_manager = None
    processor_task = None
    processor_stop_event = None
    cleanup_bg_task = None
    health_server_task = None
    inventory_task = None
    heatmap_snapshot_bg_task = None
    shard_manager = None
    shard_bg_task = None
    collector_stats_bg_task = None
    collector_id = ""

    try:
        # Replace the asyncio default executor (default size: min(32, cpu_count+4),
        # which under cgroup cpus=N can be as low as 5) with a dedicated pool sized
        # for the extraction workload. Every poll result enqueues one
        # asyncio.to_thread(_sync_extract_metrics, ...) call; under-sizing this
        # silently queues extraction work and stalls the result processor.
        extract_workers = max(16, config.parser.max_concurrent_processors * 2)
        extract_pool = ThreadPoolExecutor(max_workers=extract_workers, thread_name_prefix="extract")
        asyncio.get_running_loop().set_default_executor(extract_pool)
        logger.info(f"Extraction thread pool initialized with {extract_workers} workers")

        # Initialize database repository
        repository = TargetRepository(
            database_url=settings.database_url,
            encryption_key=settings.encryption_key,
            alerts_database_url=settings.alerts_database_url,
        )

        # Wait for database to be ready (in case API is still initializing it)
        max_retries = 30
        for attempt in range(max_retries):
            if shutdown_event.is_set():
                logger.info("Shutdown requested during startup; aborting initialization")
                return
            try:
                await repository.init_db()
                logger.info("Database connection established")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Database not ready (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error("Failed to connect to database after maximum retries")
                    raise

        # Horizontal sharding: claim this collector's target slice before any
        # polling/subscription loop runs. When disabled, the single collector owns
        # everything (get_active_targets returns all enabled targets).
        from .config import get_collector_id

        collector_id = get_collector_id()
        # _init_sharding may slot-claim a stable "collector-<ordinal>" id in dynamic
        # mode; adopt it so stats/heatmap publishing matches the id the shard owns
        # its leases under.
        shard_manager, collector_id = await _init_sharding(
            config, settings, repository, collector_id
        )

        # Initialize schema loader
        schema_loader = SchemaLoader(settings.schema_path)
        try:
            schema_loader.load()
            logger.info(f"Schema loaded from {settings.schema_path}")
        except ValueError as e:
            logger.warning(
                f"Failed to load schema from {settings.schema_path}: {e}. "
                "Using default auto-discovery configuration."
            )

        # Initialize metrics exporter
        exporter: BaseExporter
        from .exporters.influxdb import InfluxDBExporter

        exporter = InfluxDBExporter(
            url=config.influxdb.url,
            token=settings.influxdb_token,
            org=config.influxdb.org,
            bucket=config.influxdb.bucket,
            batch_size=config.influxdb.batch_size,
            flush_interval=config.influxdb.flush_interval_seconds,
            verify_ssl=config.influxdb.verify_ssl,
            write_timeout_ms=config.influxdb.write_timeout_ms,
            max_concurrent_writes=config.influxdb.max_concurrent_writes,
        )
        try:
            await exporter.connect()
            logger.info("InfluxDB exporter connected successfully")
        except ConnectionError as e:
            logger.error(
                f"Could not connect to InfluxDB: {e}. "
                "Collector will start in degraded mode - metrics will be buffered."
            )

        # Set global reference for health endpoint
        _exporter = exporter

        # Initialize poller
        poller = RedfishPoller(
            repository=repository,
            poll_interval=config.polling.interval_seconds,
            timeout=config.polling.timeout_seconds,
            max_concurrent=config.polling.max_concurrent,
            task_poll_interval=config.polling.task_poll_interval,
            task_timeout=config.polling.task_timeout,
            download_timeout=config.polling.download_timeout,
            error_retry_interval=config.polling.error_retry_interval,
            collect_endpoint=config.redfish.collect_endpoint,
            collect_body=config.redfish.collect_body,
            metric_reports=config.redfish.metric_reports,
        )

        # Initialize blob processing components
        unpacker = BlobUnpacker(
            temp_dir=config.blob.temp_dir,
            cleanup_after_parse=config.blob.cleanup_after_parse,
            max_blob_size=config.blob.max_blob_size,
        )
        extractor = MetricExtractor(schema_loader)
        discovery = MetricDiscovery(
            schema_loader, max_recursion_depth=config.parser.max_recursion_depth
        )

        # Set global references for manual poll endpoint
        _unpacker = unpacker
        _extractor = extractor
        _discovery = discovery

        # Start the poller
        await poller.start()
        logger.info(
            f"Poller started (interval: {config.polling.interval_seconds}s, "
            f"max concurrent: {config.polling.max_concurrent})"
        )

        # Set global reference for health endpoint
        _poller = poller

        # Initialize and start SSE Manager
        sse_manager = SSEManager(
            repository=repository,
            result_queue=poller._result_queue,
            reconnect_delay=config.sse.reconnect_delay,
            max_reconnect_delay=config.sse.max_reconnect_delay,
            connection_timeout=config.sse.connection_timeout,
            default_sse_endpoint=config.sse.default_endpoint,
        )
        await sse_manager.start()
        logger.info("SSE Manager started")

        # Set global reference for health endpoint
        _sse_manager = sse_manager

        # Policy engine: automated diagnostic-log collection on critical events.
        # Runs here (collector process) alongside alert ingestion; uses its own
        # LogCollector writing to the same shared storage + collected_logs table
        # as the API process's on-demand collector.
        policy_engine = None
        if config.policy.enabled:
            from .log_collector import LogCollector
            from .policy import PolicyEngine

            policy_log_collector = LogCollector(
                repository=repository,
                storage_dir=config.collected_logs.storage_dir,
                max_concurrent=config.collected_logs.max_concurrent_collections,
                timeout=config.polling.timeout_seconds,
                task_poll_interval=config.polling.task_poll_interval,
                task_timeout=config.collected_logs.task_timeout,
                download_timeout=config.collected_logs.download_timeout,
                collect_endpoint=config.redfish.collect_endpoint,
                collect_body=config.redfish.collect_body,
            )
            policy_engine = PolicyEngine(repository, policy_log_collector, config.policy)
            logger.info(
                "Policy engine enabled (mode=%s, rearm=%ss)",
                config.policy.operating_mode,
                config.policy.rearm_seconds,
            )

        # Initialize and start Alert Manager
        alert_manager = None
        if config.alerts.enabled:
            from .alert_manager import AlertManager

            alert_manager = AlertManager(
                repository=repository,
                enabled=config.alerts.enabled,
                policy_engine=policy_engine,
                webhook_base_url=config.alerts.webhook_base_url,
                sse_endpoint=config.alerts.sse_endpoint,
                event_types=config.alerts.event_types,
                severities=config.alerts.severities,
                reconnect_delay=config.alerts.reconnect_delay,
                max_retry_duration_hours=config.alerts.max_retry_duration_hours,
                cooldown_duration_hours=config.alerts.cooldown_duration_hours,
                degraded_threshold_hours=config.alerts.degraded_threshold_hours,
                permanent_failure_retry_hours=config.alerts.permanent_failure_retry_hours,
                batch_size=config.alerts.batch_size,
                batch_interval=config.alerts.batch_interval,
                max_queue_size=config.alerts.max_queue_size,
                retention_days=config.alerts.retention_days,
                cleanup_interval_hours=config.alerts.cleanup_interval_hours,
                max_alerts_per_minute=config.alerts.max_alerts_per_minute,
                enable_webhook_fallback=config.alerts.enable_webhook_fallback,
                force_webhook_mode=config.alerts.force_webhook_mode,
                baseline_pull_enabled=config.alerts.baseline_pull_enabled,
                baseline_max_entries_per_log=config.alerts.baseline_max_entries_per_log,
                baseline_repull_interval_minutes=config.alerts.baseline_repull_interval_minutes,
                cper_enrichment_enabled=config.alerts.cper_enrichment_enabled,
                cper_poll_interval_seconds=config.alerts.cper_poll_interval_seconds,
                cper_max_attempts=config.alerts.cper_max_attempts,
                cper_batch_size=config.alerts.cper_batch_size,
                cper_concurrency=config.alerts.cper_concurrency,
                cper_per_target_concurrency=config.alerts.cper_per_target_concurrency,
                cper_maintenance_every_cycles=config.alerts.cper_maintenance_every_cycles,
                cper_attempt_cooldown_seconds=config.alerts.cper_attempt_cooldown_seconds,
                cper_fetch_timeout=config.alerts.cper_fetch_timeout,
                cper_retry_failed_interval_minutes=config.alerts.cper_retry_failed_interval_minutes,
                cper_decode_timeout=config.alerts.cper_decode_timeout,
                cper_max_bytes=config.alerts.cper_max_bytes,
                cper_convert_path=config.alerts.cper_convert_path,
            )
            await alert_manager.start()
            logger.info(
                f"Alert Manager started (subscriptions: enabled, batch: {config.alerts.batch_size})"
            )
        else:
            logger.info("Alert Manager disabled in configuration")

        # Set global reference for health endpoint
        _alert_manager = alert_manager

        # Start background tasks
        processor_stop_event = asyncio.Event()
        processor_task = asyncio.create_task(
            result_processor_task(
                poller,
                unpacker,
                extractor,
                discovery,
                exporter,
                max_workers=config.parser.max_concurrent_processors,
                stop_event=processor_stop_event,
            )
        )

        cleanup_interval = max(config.blob.cleanup_max_age_seconds // 2, 300)
        cleanup_bg_task = asyncio.create_task(
            cleanup_task(unpacker, cleanup_interval, config.blob.cleanup_max_age_seconds)
        )

        # Background inventory enricher: one-time (+ staleness) Redfish inventory pull
        if config.inventory.enabled:
            from .inventory.enricher import InventoryEnricher

            inventory_enricher = InventoryEnricher(repository, config.inventory, config.location)
            inventory_task = asyncio.create_task(inventory_enricher.run())
            logger.info("Inventory enricher background task started")

        # Shard renew loop (reclaims dead shards' targets, picks up new ones).
        if shard_manager is not None:
            shard_bg_task = asyncio.create_task(shard_manager.run())

        # Publish this collector's stats to shared SQLite for the API to aggregate.
        # In dynamic sharding this row IS the membership heartbeat (peers read the
        # live-collector set from fresh collector_stats rows), so publish at the
        # claim cadence — this is what makes ShardConfig's "membership_ttl >= 2 ×
        # claim_interval" invariant actually hold. Single-collector deployments keep
        # the lighter 30s default.
        stats_interval = config.sharding.claim_interval_seconds if config.sharding.enabled else 30.0
        collector_stats_bg_task = asyncio.create_task(
            collector_stats_task(
                repository, collector_id, alert_manager, shard_manager, interval=stats_interval
            )
        )

        # Publish the heatmap snapshot to shared SQLite so the API reads it
        # locally instead of calling this process (whose event loop stalls during
        # InfluxDB flushes). Keyed by collector_id so shards don't clobber.
        heatmap_snapshot_bg_task = asyncio.create_task(
            heatmap_snapshot_task(repository, collector_id=collector_id)
        )
        logger.info("Heatmap snapshot publisher started")

        # Start health monitoring HTTP server on port 8081
        health_app = create_health_app()
        health_server_task = asyncio.create_task(run_health_server(health_app, port=8081))
        logger.info("Health monitoring server started on port 8081")

        logger.info("✅ Collector service startup complete - background processing active")

        # Block until a shutdown signal arrives (or was already requested).
        await shutdown_event.wait()
    finally:
        # Always runs — graceful shutdown on signal AND cleanup on any
        # partial-init exception. Order (stop producers -> drain -> stop
        # processor -> flush exporter) is handled by the helper.
        await _shutdown_collector(
            poller=poller,
            sse_manager=sse_manager,
            alert_manager=alert_manager,
            exporter=exporter,
            repository=repository,
            extract_pool=extract_pool,
            processor_task=processor_task,
            processor_stop_event=processor_stop_event,
            cleanup_bg_task=cleanup_bg_task,
            health_server_task=health_server_task,
            inventory_task=inventory_task,
            heatmap_snapshot_bg_task=heatmap_snapshot_bg_task,
            shard_manager=shard_manager,
            shard_bg_task=shard_bg_task,
            collector_stats_bg_task=collector_stats_bg_task,
        )


def run():
    """Entry point for collector service."""
    config = get_config()

    # Configure logging with UTC timestamps
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    formatter = UTCFormatter(config.logging.format)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    settings = get_settings()
    if not settings.encryption_key:
        logger.error("ENCRYPTION_KEY environment variable is required")
        sys.exit(1)

    # Fail closed on a missing alerts DB URL when alerts are enabled: otherwise
    # the service starts "healthy" and every alert write fails at runtime with
    # only a buried log line as the signal.
    if config.alerts.enabled and not settings.alerts_database_url:
        logger.error(
            "ALERTS_DATABASE_URL is required when alerts are enabled "
            "(set it, or disable alerts in config)."
        )
        sys.exit(1)

    if not settings.influxdb_token:
        logger.warning("INFLUXDB_TOKEN not set, InfluxDB writes will fail")

    # Warn when the webhook fallback is enabled but points at a loopback address:
    # a BMC can't POST alerts to the collector's localhost, so targets that don't
    # support SSE silently get no alert coverage. Operators must set a routable
    # ALERT_WEBHOOK_BASE_URL for the fallback to actually work.
    if (
        config.alerts.enabled
        and config.alerts.enable_webhook_fallback
        and any(h in config.alerts.webhook_base_url for h in ("localhost", "127.0.0.1"))
    ):
        logger.warning(
            "Alert webhook fallback is enabled but webhook_base_url is loopback "
            "(%s); BMCs cannot reach it, so SSE-incapable targets will have NO "
            "alert coverage. Set ALERT_WEBHOOK_BASE_URL to a collector address "
            "reachable from the BMC network.",
            config.alerts.webhook_base_url,
        )

    # Run the collector service
    try:
        asyncio.run(run_collector())
    except KeyboardInterrupt:
        logger.info("Collector service interrupted by user")
    except Exception as e:
        logger.error(f"Collector service crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
