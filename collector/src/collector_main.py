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
- Metric extraction and export to InfluxDB/Prometheus
- SSE event subscriptions
- Alert management
- Background cleanup tasks
- Internal health monitoring endpoint

The collector shares the SQLite database (in WAL mode) with the API process.
"""

import asyncio
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import get_config, get_settings
from .database.repository import TargetRepository
from .exporters.base import BaseExporter, Metric
from .parser.discovery import DiscoveredMetric, MetricDiscovery
from .parser.extractor import ExtractedMetric, MetricExtractor
from .parser.schema import SchemaLoader
from .parser.unpacker import BlobUnpacker
from .redfish.poller import PollResult, RedfishPoller
from .redfish.sse_subscriber import SSEManager

logger = logging.getLogger(__name__)

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
    timestamp = datetime.now(UTC)

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
            metric_values = report_json.get("MetricValues", [])

            # Dedup: first report to claim a MetricProperty wins
            unique_values = [
                mv
                for mv in metric_values
                if not mv.get("MetricProperty") or mv["MetricProperty"] not in seen_properties
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
                    f"Auto-discovery failed for {report_type} report from {result.target_name}: {e}"
                )

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
            report_tags = {**extra_tags, "report_type": extracted_file.report_type}  # type: ignore[attr-defined]
            extracted = extractor.extract_from_file(  # type: ignore[attr-defined]
                extracted_file.path,
                result.target_host,
                report_tags,
                timestamp,
            )
            all_metrics.extend(extracted)

            try:
                discovered = discovery.discover_from_file(  # type: ignore[attr-defined]
                    extracted_file.path,
                    result.target_host,
                    report_tags,
                    exclude_keys=schema_keys,
                    timestamp=timestamp,
                )
                all_metrics.extend(discovered)
            except Exception as e:
                logger.warning(
                    f"Auto-discovery failed for {extracted_file.report_type} from {result.target_name}: {e}"  # type: ignore[attr-defined]
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
):
    """Background task to process poll results in parallel."""
    semaphore = asyncio.Semaphore(max_workers)

    async def _process_one(result: PollResult):
        async with semaphore:
            await process_poll_result(result, unpacker, extractor, discovery, exporter)

    while True:
        try:
            results = await poller.get_results(timeout=1.0)
            if results:
                await asyncio.gather(*[_process_one(r) for r in results], return_exceptions=True)
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
        global _exporter, _poller, _sse_manager, _alert_manager

        settings = get_settings()
        backend = settings.metrics_backend

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

        # Check poller
        poller_status = "not_initialized"
        poller_info = {}
        if _poller:
            poller_status = "running" if _poller.is_running else "stopped"
            if hasattr(_poller, "get_stats"):
                with suppress(Exception):
                    poller_info = _poller.get_stats()

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
            except Exception as e:
                # Don't expose raw exception text on the health endpoint;
                # type-name only — the full stack is logged separately.
                logger.warning(f"Alert manager stats failed: {e}", exc_info=True)
                alert_status = f"error: {type(e).__name__}"

        overall_healthy = exporter_healthy and poller_status == "running"

        return {
            "status": "healthy" if overall_healthy else "degraded",
            "service": "collector",
            "metrics_backend": backend,
            "components": {
                "exporter": {
                    "healthy": exporter_healthy,
                    "message": exporter_message,
                    "backend": backend,
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
        global _alert_manager

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
    async def trigger_manual_poll(target_id: int):
        """Trigger an immediate poll of a target from API service request."""
        global _poller, _unpacker, _extractor, _discovery, _exporter

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
            import json

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
        global _alert_manager

        # target_id is an int (FastAPI path-param coercion); using deferred
        # %-style logging both follows best practice and gives CodeQL a clear
        # signal that no user-controlled text is interpolated into the message.
        tid = int(target_id)

        if not _alert_manager:
            logger.warning("Received webhook for target %d but alert manager not initialized", tid)
            return {"status": "error", "message": "Alert manager not initialized"}

        try:
            # Parse JSON payload from BMC
            event_data = await request.json()
            event_count = len(event_data.get("Events", []))

            logger.debug("Received webhook from target %d: %d events", tid, event_count)

            # Forward to alert manager for processing
            await _alert_manager.process_webhook_event(tid, event_data)

            return {"status": "ok", "events_received": event_count}

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


async def run_health_server(app: FastAPI, port: int = 8081):
    """Run the health monitoring HTTP server."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="error",  # Minimal logging for health server
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_collector():
    """Main collector service loop."""
    global _exporter, _poller, _sse_manager, _alert_manager, _unpacker, _extractor, _discovery

    config = get_config()
    settings = get_settings()

    logger.info("Starting GPU Metrics Collector Service...")
    logger.info("This process handles background data collection and metric export")

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
        database_url=settings.database_url, encryption_key=settings.encryption_key
    )

    # Wait for database to be ready (in case API is still initializing it)
    max_retries = 30
    for attempt in range(max_retries):
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
    if settings.metrics_backend == "prometheus":
        from .exporters.prometheus import PrometheusExporter

        exporter = PrometheusExporter()
        await exporter.connect()
        logger.info("Prometheus exporter initialized")
    else:
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

    # Initialize and start Alert Manager
    alert_manager = None
    if config.alerts.enabled:
        from .alert_manager import AlertManager

        alert_manager = AlertManager(
            repository=repository,
            enabled=config.alerts.enabled,
            webhook_base_url=config.alerts.webhook_base_url,
            sse_endpoint=config.alerts.sse_endpoint,
            event_types=config.alerts.event_types,
            severities=config.alerts.severities,
            reconnect_delay=config.alerts.reconnect_delay,
            max_retry_duration_hours=config.alerts.max_retry_duration_hours,
            cooldown_duration_hours=config.alerts.cooldown_duration_hours,
            degraded_threshold_hours=config.alerts.degraded_threshold_hours,
            batch_size=config.alerts.batch_size,
            batch_interval=config.alerts.batch_interval,
            max_queue_size=config.alerts.max_queue_size,
            retention_days=config.alerts.retention_days,
            cleanup_interval_hours=config.alerts.cleanup_interval_hours,
            max_alerts_per_minute=config.alerts.max_alerts_per_minute,
            enable_webhook_fallback=config.alerts.enable_webhook_fallback,
            force_webhook_mode=config.alerts.force_webhook_mode,
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
    processor_task = asyncio.create_task(
        result_processor_task(
            poller,
            unpacker,
            extractor,
            discovery,
            exporter,
            max_workers=config.parser.max_concurrent_processors,
        )
    )

    cleanup_interval = max(config.blob.cleanup_max_age_seconds // 2, 300)
    cleanup_bg_task = asyncio.create_task(
        cleanup_task(unpacker, cleanup_interval, config.blob.cleanup_max_age_seconds)
    )

    # Start health monitoring HTTP server on port 8081
    health_app = create_health_app()
    health_server_task = asyncio.create_task(run_health_server(health_app, port=8081))
    logger.info("Health monitoring server started on port 8081")

    logger.info("✅ Collector service startup complete - background processing active")

    # Wait for shutdown signal
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, initiating shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await shutdown_event.wait()

    # Shutdown sequence
    logger.info("Collector service shutting down...")

    # Cancel background tasks
    processor_task.cancel()
    cleanup_bg_task.cancel()
    health_server_task.cancel()

    # Discarding await result intentional — we just need the task to
    # finish/raise CancelledError, which `suppress` swallows.
    with suppress(asyncio.CancelledError):
        _ = await processor_task
    with suppress(asyncio.CancelledError):
        _ = await cleanup_bg_task
    with suppress(asyncio.CancelledError):
        _ = await health_server_task

    # Stop services
    await poller.stop()
    await sse_manager.stop()
    if alert_manager:
        await alert_manager.stop()

    # Close exporter
    await exporter.close()

    # Close database
    await repository.close()

    # Drain extraction thread pool; let in-flight extracts finish.
    extract_pool.shutdown(wait=True)

    logger.info("Collector service shutdown complete")


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

    if not settings.influxdb_token:
        logger.warning("INFLUXDB_TOKEN not set, InfluxDB writes will fail")

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
