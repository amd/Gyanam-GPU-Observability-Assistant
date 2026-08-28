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
"""Alert manager for coordinating SSE and webhook alert subscriptions across targets.

Optimized for 300+ concurrent connections with efficient batching and
resource management. Automatically falls back to webhook subscriptions when
SSE is not supported or broken.
"""

import asyncio
import hashlib
import logging
import os
from collections import defaultdict, deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from urllib.parse import urlparse

from prometheus_client import Counter, Gauge

from .cper_worker import CperEnrichmentWorker
from .database.models import Target
from .database.repository import TargetRepository
from .redfish.alert_subscriber import AlertCallback, AlertEvent, AlertSubscriber
from .redfish.log_baseline import pull_baseline_alerts
from .redfish.sse_capability_check import SSESupport, check_sse_capability
from .redfish.webhook_subscriber import SubscriptionFailureType, WebhookSubscriber

logger = logging.getLogger(__name__)


def _is_unreachable_from_bmc(url: str) -> tuple[bool, str]:
    """Return (is_unreachable, reason).

    Loopback/private/empty hosts in the webhook destination URL cause every
    BMC to reject the subscription with PropertyValueFormatError, since the
    BMC POSTs to that URL from its own network and cannot reach the
    collector's localhost. Detect at startup so we don't silently brick the
    alerts feature for every target.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        return True, f"unparseable URL: {e}"

    if not parsed.scheme or not parsed.hostname:
        return True, "missing scheme or host"

    host = parsed.hostname
    if host in {"localhost", "0.0.0.0"}:
        return True, f"host '{host}' is unreachable from a BMC"

    try:
        ip = ip_address(host)
        if ip.is_loopback or ip.is_unspecified:
            return True, f"host '{host}' is loopback/unspecified"
    except ValueError:
        # Not an IP literal — treat as a routable hostname. (FQDN sanity is
        # the operator's responsibility; we only catch the obvious wrong cases.)
        pass

    return False, ""


# Prometheus metrics for alert system monitoring
ALERTS_RECEIVED_TOTAL = Counter(
    "gyanam_alerts_received_total",
    "Total number of alerts received from SSE streams",
    ["target_name", "severity"],
)

ALERTS_WRITTEN_TOTAL = Counter(
    "gyanam_alerts_written_total", "Total number of alerts written to database"
)

ALERTS_DROPPED_TOTAL = Counter(
    "gyanam_alerts_dropped_total",
    "Total number of alerts dropped due to queue full or rate limiting",
    ["reason"],
)

ALERT_QUEUE_SIZE = Gauge(
    "gyanam_alert_queue_size", "Current number of alerts waiting to be written"
)

ALERT_SUBSCRIPTIONS_ACTIVE = Gauge(
    "gyanam_alert_subscriptions_active",
    "Number of active alert subscriptions",
    ["subscription_type"],
)


class RateLimiter:
    """Token bucket rate limiter per target to prevent alert flooding.

    Uses a sliding window approach to track alerts per target over the last minute.
    Designed to be lightweight and fast for 300+ concurrent targets.
    """

    def __init__(self, max_alerts_per_minute: int = 100):
        """Initialize rate limiter.

        Args:
            max_alerts_per_minute: Maximum number of alerts allowed per target per minute
        """
        self.max_alerts = max_alerts_per_minute
        # Track alert timestamps per target using a deque (efficient for sliding window)
        self.windows: dict[int, deque] = defaultdict(deque)

    def allow(self, target_id: int) -> bool:
        """Check if alert from target_id should be allowed.

        Args:
            target_id: Target ID to check

        Returns:
            True if alert should be allowed, False if rate limit exceeded
        """
        now = datetime.now(UTC)
        window = self.windows[target_id]

        # Remove timestamps older than 1 minute (sliding window)
        cutoff = now - timedelta(minutes=1)
        while window and window[0] < cutoff:
            window.popleft()

        # Check if under limit
        if len(window) < self.max_alerts:
            window.append(now)
            return True
        return False


class AlertManager:
    """Manages SSE and webhook alert subscriptions for all enabled targets.

    Features for scalability:
    - Concurrent management of 300+ SSE or webhook connections
    - Automatic fallback to webhooks when SSE is not supported
    - Batch processing of alerts (reduces DB load)
    - Bounded queue to prevent memory issues
    - Automatic subscription management when targets change
    - Circuit breaker for repeatedly failing subscriptions
    - Periodic cleanup of old alerts
    """

    def __init__(
        self,
        repository: TargetRepository,
        enabled: bool = True,
        webhook_base_url: str = "http://localhost:8080/redfish-webhook",
        sse_endpoint: str = "/redfish/v1/EventService/SSE",
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
        reconnect_delay: int = 30,
        max_retry_duration_hours: float = 24,
        cooldown_duration_hours: float = 6,
        degraded_threshold_hours: float = 1,
        permanent_failure_retry_hours: float = 6,
        batch_size: int = 100,
        batch_interval: float = 5.0,
        max_queue_size: int = 10000,
        retention_days: int = 30,
        cleanup_interval_hours: int = 6,
        max_alerts_per_minute: int = 100,
        alert_callback: AlertCallback | None = None,
        enable_webhook_fallback: bool = True,
        force_webhook_mode: bool = False,
        baseline_pull_enabled: bool = True,
        baseline_max_entries_per_log: int = 200,
        baseline_repull_interval_minutes: int = 60,  # keep in sync with AlertsConfig
        cper_enrichment_enabled: bool = True,
        cper_poll_interval_seconds: int = 30,
        cper_max_attempts: int = 3,
        cper_batch_size: int = 20,
        cper_concurrency: int = 3,
        cper_per_target_concurrency: int = 1,
        cper_maintenance_every_cycles: int = 10,
        cper_attempt_cooldown_seconds: int = 60,
        cper_fetch_timeout: float = 60.0,
        cper_retry_failed_interval_minutes: int = 360,
        cper_decode_timeout: float = 15.0,
        cper_max_bytes: int = 8 * 1024 * 1024,
        cper_convert_path: str = "/usr/local/bin/cper-convert",
    ):
        """Initialize alert manager.

        Args:
            repository: Target repository for database operations
            enabled: Whether alert subscriptions are enabled globally
            webhook_base_url: Base URL for webhook receiver endpoint
            sse_endpoint: Default SSE endpoint path
            event_types: Event types to capture (default: Alert, StatusChange)
            severities: Severities to capture (default: Warning, Critical)
            reconnect_delay: Seconds to wait before reconnecting on error
            max_retry_duration_hours: Hours to retry before circuit breaker
            cooldown_duration_hours: Hours in cooldown before auto-resume
            degraded_threshold_hours: Hours of failures to mark as degraded
            permanent_failure_retry_hours: Hours before retrying permanent webhook
                failures. Set to 0 to disable auto-retry.
            batch_size: Number of alerts to batch before writing to DB
            batch_interval: Seconds between batch writes (even if not full)
            max_queue_size: Maximum alerts in queue (prevents memory overflow)
            retention_days: Days to retain alerts before cleanup
            cleanup_interval_hours: Hours between cleanup runs
            max_alerts_per_minute: Max alerts per target per minute (rate limiting)
            alert_callback: Optional callback for custom alert processing
            enable_webhook_fallback: Whether to use webhooks when SSE fails
            force_webhook_mode: Force webhook mode even if SSE is available (for testing)
        """
        self.repository = repository
        self.enabled = enabled
        self.webhook_base_url = webhook_base_url
        self.sse_endpoint = sse_endpoint
        self.event_types = event_types or ["Alert", "StatusChange"]
        self.severities = severities or ["Warning", "Critical"]
        self.reconnect_delay = reconnect_delay
        self.max_retry_duration_hours = max_retry_duration_hours
        self.cooldown_duration_hours = cooldown_duration_hours
        self.degraded_threshold_hours = degraded_threshold_hours
        self._permanent_failure_retry_hours = permanent_failure_retry_hours
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.max_queue_size = max_queue_size
        self.retention_days = retention_days
        self.cleanup_interval_hours = cleanup_interval_hours
        self.alert_callback = alert_callback
        self.enable_webhook_fallback = enable_webhook_fallback
        self.force_webhook_mode = force_webhook_mode
        self.baseline_pull_enabled = baseline_pull_enabled
        self.baseline_max_entries_per_log = baseline_max_entries_per_log
        self.baseline_repull_interval_minutes = baseline_repull_interval_minutes
        # CPER enrichment runs in its own worker (isolated + independently
        # testable); AlertManager just owns its lifecycle and folds in its stats.
        self._cper_worker = CperEnrichmentWorker(
            repository,
            enabled=cper_enrichment_enabled,
            poll_interval_seconds=cper_poll_interval_seconds,
            batch_size=cper_batch_size,
            max_attempts=cper_max_attempts,
            concurrency=cper_concurrency,
            per_target_concurrency=cper_per_target_concurrency,
            maintenance_every_cycles=cper_maintenance_every_cycles,
            attempt_cooldown_seconds=cper_attempt_cooldown_seconds,
            fetch_timeout=cper_fetch_timeout,
            decode_timeout=cper_decode_timeout,
            max_bytes=cper_max_bytes,
            convert_path=cper_convert_path,
            retry_failed_interval_minutes=cper_retry_failed_interval_minutes,
        )

        # Rate limiting to prevent alert flooding from misbehaving BMCs
        self._rate_limiter = RateLimiter(max_alerts_per_minute=max_alerts_per_minute)

        # Subscription management (SSE and webhook)
        self._subscribers: dict[int, AlertSubscriber] = {}
        self._webhook_subscribers: dict[int, WebhookSubscriber] = {}
        self._subscription_types: dict[int, str] = {}  # Track type per target
        self._permanently_failed: dict[int, datetime] = {}  # target_id -> next_retry_time
        # Per-target config fingerprint; a change (rotated password, moved
        # endpoint, toggled verify_ssl) triggers tear-down + re-subscribe so we
        # never keep using stale credentials/URLs.
        self._sub_fingerprints: dict[int, str] = {}
        self._running = False
        self._refresh_task: asyncio.Task | None = None
        self._batch_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._baseline_task: asyncio.Task | None = None
        # One-shot baseline pulls kept referenced so they aren't GC'd mid-flight.
        self._baseline_jobs: set[asyncio.Task] = set()
        # Bound concurrent baseline traversals (startup burst + periodic re-pull)
        # so a large fleet doesn't fan out hundreds of multi-GET pulls at once.
        self._baseline_sem = asyncio.Semaphore(10)

        # Alert queue for batch processing
        self._alert_queue: asyncio.Queue[AlertEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._alerts_received = 0
        self._alerts_written = 0
        self._alerts_dropped = 0
        self._alerts_baseline_pulled = 0

    async def start(self) -> None:
        """Start alert subscriptions for all enabled targets."""
        if not self.enabled:
            logger.info("Alert subscriptions are disabled in configuration")
            return

        if self._running:
            logger.warning("Alert manager is already running")
            return

        # Validate webhook_base_url before we burn ~30 minutes of BMC subscription
        # POSTs all returning HTTP 400. Operator can override by setting
        # GYANAM_ALLOW_LOOPBACK_WEBHOOK=1 for single-host test setups.
        if self.enable_webhook_fallback or self.force_webhook_mode:
            unreachable, reason = _is_unreachable_from_bmc(self.webhook_base_url)
            allow_loopback = os.environ.get("GYANAM_ALLOW_LOOPBACK_WEBHOOK", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if unreachable and not allow_loopback:
                logger.error(
                    "Webhook fallback disabled: webhook_base_url=%r is %s. "
                    "BMCs cannot POST events to this URL. Set "
                    "ALERT_WEBHOOK_BASE_URL to a routable address visible to "
                    "your BMCs (e.g. http://<collector-host-ip>:8081/redfish-webhook). "
                    "To force-allow loopback for single-host testing, set "
                    "GYANAM_ALLOW_LOOPBACK_WEBHOOK=1.",
                    self.webhook_base_url,
                    reason,
                )
                # Disable webhook paths so we don't waste time hitting every
                # BMC with a request it will reject.
                self.enable_webhook_fallback = False
                self.force_webhook_mode = False

        self._running = True

        # Start background tasks
        self._refresh_task = asyncio.create_task(self._refresh_subscriptions_loop())
        self._batch_task = asyncio.create_task(self._batch_processor_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self.baseline_pull_enabled and self.baseline_repull_interval_minutes > 0:
            self._baseline_task = asyncio.create_task(self._baseline_repull_loop())
        await self._cper_worker.start()

        logger.info("Alert manager started")

    async def stop(self) -> None:
        """Stop all alert subscriptions."""
        self._running = False

        # Stop the CPER enrichment worker (drains its own task).
        await self._cper_worker.stop()

        # Stop all SSE subscribers
        for subscriber in list(self._subscribers.values()):
            await subscriber.stop()
        self._subscribers.clear()

        # Delete all webhook subscriptions
        for _target_id, webhook_sub in list(self._webhook_subscribers.items()):
            await webhook_sub.delete_subscription()
        self._webhook_subscribers.clear()
        self._subscription_types.clear()

        # Cancel background tasks (including any in-flight one-shot baseline pulls)
        pending = [
            self._refresh_task,
            self._batch_task,
            self._cleanup_task,
            self._baseline_task,
        ]
        pending.extend(self._baseline_jobs)
        for task in pending:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    # Discarding result intentional — we just need the task
                    # to finish/raise CancelledError.
                    _ = await task
        self._baseline_jobs.clear()

        # Process remaining queued alerts
        await self._flush_batch()

        logger.info(
            f"Alert manager stopped (received: {self._alerts_received}, "
            f"written: {self._alerts_written})"
        )

    async def _refresh_subscriptions_loop(self) -> None:
        """Periodically refresh subscriptions based on target configuration."""
        while self._running:
            try:
                await self._refresh_subscriptions()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Error refreshing alert subscriptions: {type(e).__name__}: {e}", exc_info=True
                )
                await asyncio.sleep(10)

    async def _refresh_subscriptions(self) -> None:
        """Sync alert subscriptions with current target configuration."""
        targets = await self.repository.get_all_targets(enabled_only=True)

        # Filter targets with alert subscription enabled
        alert_targets = [t for t in targets if t.enable_alert_subscription]

        # Combined current IDs from both SSE and webhook subscribers
        current_ids = set(self._subscribers.keys()) | set(self._webhook_subscribers.keys())
        desired_ids = {t.id for t in alert_targets}
        target_by_id = {t.id: t for t in alert_targets}

        # Detect targets whose connection-relevant config changed while still
        # present: tear them down and re-add so we pick up new creds/endpoints.
        changed_ids = {
            tid
            for tid in (current_ids & desired_ids)
            if self._sub_fingerprints.get(tid) != self._target_fingerprint(target_by_id[tid])
        }
        if changed_ids:
            logger.info("Re-subscribing %d target(s) after config change", len(changed_ids))

        # Stop subscriptions for removed/disabled/changed targets
        to_remove = (current_ids - desired_ids) | changed_ids
        for target_id in to_remove:
            # Remove SSE subscriber if exists
            if target_id in self._subscribers:
                subscriber = self._subscribers.pop(target_id)
                await subscriber.stop()
                logger.info(f"Stopped SSE subscription for target {target_id}")

            # Remove webhook subscriber if exists
            if target_id in self._webhook_subscribers:
                webhook_sub = self._webhook_subscribers.pop(target_id)
                await webhook_sub.delete_subscription()
                logger.info(f"Deleted webhook subscription for target {target_id}")

            self._subscription_types.pop(target_id, None)
            self._sub_fingerprints.pop(target_id, None)
            # Only clear the permanent-failure flag when the target is genuinely
            # gone/disabled — a config change (changed_ids) is an explicit fix
            # attempt, so also clear it there to allow a retry.
            if target_id not in desired_ids or target_id in changed_ids:
                self._permanently_failed.pop(target_id, None)
            self._update_subscription_metrics()

        # Start subscriptions for new/re-enabled/changed targets. Skip
        # permanently-failed targets here (rather than inside _start_subscription)
        # so we emit one summary line instead of a per-target WARNING every minute.
        to_add = (desired_ids - current_ids) | changed_ids

        if self._permanent_failure_retry_hours > 0:
            now = datetime.now(UTC)
            due = [tid for tid, retry_at in self._permanently_failed.items() if retry_at <= now]
            for tid in due:
                self._permanently_failed.pop(tid, None)
                logger.info(
                    "Permanent webhook failure cooldown elapsed for target %d; will retry subscription",
                    tid,
                )

        skipped_failed = to_add & self._permanently_failed.keys()
        to_add -= skipped_failed
        if skipped_failed:
            if self._permanent_failure_retry_hours > 0:
                logger.warning(
                    "Skipping %d permanently-failed alert targets (each will be retried "
                    "once its %.1fh cooldown elapses).",
                    len(skipped_failed),
                    self._permanent_failure_retry_hours,
                )
            else:
                logger.warning(
                    "Skipping %d permanently-failed alert targets (auto-retry disabled; "
                    "will be retried only after target removal/re-add).",
                    len(skipped_failed),
                )
            logger.debug(
                "Permanently-failed target IDs: %s",
                sorted(skipped_failed),
            )
        for target in alert_targets:
            if target.id in to_add:
                await self._start_subscription(target)
                # Record the fingerprint only if a subscription was actually
                # established, so a failed target is retried (not treated as
                # "already subscribed with matching config").
                if target.id in self._subscribers or target.id in self._webhook_subscribers:
                    self._sub_fingerprints[target.id] = self._target_fingerprint(target)

    @staticmethod
    def _target_fingerprint(target: Target) -> str:
        """Hash of the connection-relevant config for change detection."""
        parts = [
            getattr(target, "base_url", ""),
            getattr(target, "username", "") or "",
            getattr(target, "encrypted_password", "") or "",
            getattr(target, "alert_sse_endpoint", "") or "",
            str(getattr(target, "verify_ssl", "")),
            str(getattr(target, "connection_mode", "")),
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).hexdigest()

    async def _start_subscription(self, target: Target) -> None:
        """Start alert subscription for a single target with SSE/webhook fallback."""
        try:
            # Skip permanently failed targets. Normally filtered upstream in
            # _refresh_subscriptions; this is defence-in-depth for direct calls.
            if target.id in self._permanently_failed:
                logger.debug(
                    f"Skipping {target.name} - marked as permanently failed (configuration error)"
                )
                return

            # Decrypt credentials
            password = self.repository.decrypt_password(target)
            base_url = target.base_url

            # Force webhook mode if configured (for testing/debugging)
            if self.force_webhook_mode:
                logger.info(f"Force webhook mode enabled, skipping SSE for {target.name}")
                if self.enable_webhook_fallback:
                    await self._start_webhook_subscription(target, password)
                    return
                else:
                    logger.warning(
                        f"Force webhook mode enabled but webhook fallback disabled for {target.name}"
                    )
                    return

            # Step 1: Check SSE capability first
            logger.info(f"Checking SSE capability for {target.name}...")
            sse_result = await check_sse_capability(
                base_url=base_url,
                username=target.username,
                password=password,
                verify_ssl=target.verify_ssl,
                test_duration_seconds=5.0,
            )

            logger.info(
                f"SSE capability for {target.name}: {sse_result.support.value} - {sse_result.reason}"
            )

            # Step 2: Try SSE if supported
            if sse_result.support == SSESupport.SUPPORTED:
                await self._start_sse_subscription(target, password)
                return

            # Step 3: Fallback to webhook if enabled
            if self.enable_webhook_fallback:
                logger.info(
                    f"SSE not available for {target.name}, falling back to webhook subscription"
                )
                await self._start_webhook_subscription(target, password)
                return

            # Step 4: If neither SSE nor webhook enabled, log and skip
            logger.warning(
                f"Cannot subscribe to {target.name}: SSE {sse_result.support.value}, "
                f"webhook fallback disabled"
            )

        except Exception as e:
            logger.error(
                f"Failed to start alert subscription for {target.name}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    async def _start_sse_subscription(self, target: Target, password: str) -> None:
        """Start SSE-based alert subscription."""
        try:
            endpoint = target.alert_sse_endpoint or self.sse_endpoint

            subscriber = AlertSubscriber(
                target_id=target.id,
                target_name=target.name,
                target_bmc=target.host,
                base_url=target.base_url,
                username=target.username,
                password=password,
                sse_endpoint=endpoint,
                verify_ssl=target.verify_ssl,
                callback=self._on_alert,
                reconnect_delay=self.reconnect_delay,
                max_retry_duration_hours=self.max_retry_duration_hours,
                cooldown_duration_hours=self.cooldown_duration_hours,
                degraded_threshold_hours=self.degraded_threshold_hours,
                event_types=self.event_types,
                severities=self.severities,
            )

            await subscriber.start()
            self._subscribers[target.id] = subscriber
            self._subscription_types[target.id] = "sse"
            self._update_subscription_metrics()
            logger.info(f"Started SSE subscription for {target.name}")

            # Populate existing/standing alerts once; SSE only streams new events.
            self._schedule_baseline_pull(target.id)

        except Exception as e:
            logger.error(
                f"Failed to start SSE subscription for {target.name}: " f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    async def _start_webhook_subscription(self, target: Target, password: str) -> None:
        """Start webhook-based alert subscription."""
        try:
            webhook_url = f"{self.webhook_base_url}/{target.id}"

            webhook_sub = WebhookSubscriber(
                target_id=target.id,
                target_name=target.name,
                target_bmc=target.host,
                base_url=target.base_url,
                username=target.username,
                password=password,
                webhook_url=webhook_url,
                verify_ssl=target.verify_ssl,
                event_types=self.event_types,
                severities=self.severities,
            )

            # Create subscription on BMC
            result = await webhook_sub.create_subscription()
            if result.success:
                self._webhook_subscribers[target.id] = webhook_sub
                self._subscription_types[target.id] = "webhook"
                self._update_subscription_metrics()
                logger.info(f"Started webhook subscription for {target.name}")

                # Populate existing/standing alerts once; the subscription only
                # delivers events emitted after it was created.
                self._schedule_baseline_pull(target.id)
            else:
                if result.failure_type == SubscriptionFailureType.PERMANENT:
                    if self._permanent_failure_retry_hours > 0:
                        retry_at = datetime.now(UTC) + timedelta(
                            hours=self._permanent_failure_retry_hours
                        )
                        logger.error(
                            "PERMANENT webhook subscription failure for %s: %s. "
                            "Will retry at %s (after %.1fh cooldown).",
                            target.name,
                            result.error_message,
                            retry_at.isoformat(),
                            self._permanent_failure_retry_hours,
                        )
                    else:
                        retry_at = datetime.max.replace(tzinfo=UTC)
                        logger.error(
                            "PERMANENT webhook subscription failure for %s: %s. "
                            "Auto-retry is disabled (alerts.permanent_failure_retry_hours=0); "
                            "will not retry until the target is removed and re-added.",
                            target.name,
                            result.error_message,
                        )
                    self._permanently_failed[target.id] = retry_at
                else:
                    logger.error(
                        f"Temporary webhook subscription failure for {target.name}: {result.error_message}. "
                        f"Will retry on next refresh."
                    )

        except Exception as e:
            logger.error(
                f"Failed to start webhook subscription for {target.name}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    def _update_subscription_metrics(self) -> None:
        """Update Prometheus metrics for subscription counts."""
        sse_count = len(self._subscribers)
        webhook_count = len(self._webhook_subscribers)

        ALERT_SUBSCRIPTIONS_ACTIVE.labels(subscription_type="sse").set(sse_count)
        ALERT_SUBSCRIPTIONS_ACTIVE.labels(subscription_type="webhook").set(webhook_count)

    async def process_webhook_event(self, target_id: int, event_data: dict) -> int:
        """Process incoming webhook event from a BMC.

        Args:
            target_id: Target ID that sent the event
            event_data: Redfish event payload from webhook POST

        Returns:
            Number of alerts enqueued.
        """
        webhook_sub = self._webhook_subscribers.get(target_id)
        if not webhook_sub:
            # Deferred %-style logging; target_id is an int (guaranteed by
            # FastAPI path coercion at the caller). No user-controlled text.
            logger.warning("Received webhook event for unknown target %d", int(target_id))
            return 0

        # Verify the Context token we set at subscription time. This rejects
        # blind POSTs to the (unauthenticated) webhook endpoint. NOTE: Context is
        # guessable ("target_<id>") so this is not strong auth — a shared secret
        # or mTLS is the follow-up for a hostile network.
        expected_context = f"target_{target_id}"
        received_context = event_data.get("Context")
        if received_context is not None and received_context != expected_context:
            logger.warning(
                "Webhook for target %d rejected: Context %r != expected",
                int(target_id),
                received_context,
            )
            return 0

        # Parse webhook event into AlertEvent objects
        alerts = webhook_sub.parse_webhook_event(event_data)

        # Feed alerts into processing queue
        for alert in alerts:
            self._on_alert(alert)

        logger.debug(f"Processed {len(alerts)} alerts from webhook for {webhook_sub.target_name}")
        return len(alerts)

    def _on_alert(self, alert: AlertEvent) -> None:
        """Callback for live (SSE/webhook) alerts - rate-limited, then queued."""
        # Check rate limit to prevent alert flooding from misbehaving BMCs
        if not self._rate_limiter.allow(alert.target_id):
            self._alerts_dropped += 1
            ALERTS_DROPPED_TOTAL.labels(reason="rate_limit").inc()
            logger.warning(
                f"Rate limit exceeded for {alert.target_name} "
                f"(max {self._rate_limiter.max_alerts}/min), dropping alert"
            )
            return
        self._enqueue(alert)

    def _on_baseline_alert(self, alert: AlertEvent) -> None:
        """Callback for baseline log-pull alerts - bypasses the rate limiter.

        A baseline can legitimately surface far more than 100 entries/min; the
        per-minute live-flood limiter must not throttle it. DB dedup keeps
        re-pulls idempotent.
        """
        self._enqueue(alert)

    def _enqueue(self, alert: AlertEvent) -> None:
        """Add an alert to the batch-processing queue."""
        try:
            self._alert_queue.put_nowait(alert)
            self._alerts_received += 1
            ALERTS_RECEIVED_TOTAL.labels(
                target_name=alert.target_name, severity=alert.severity
            ).inc()
            ALERT_QUEUE_SIZE.set(self._alert_queue.qsize())

            # Also invoke custom callback if provided
            if self.alert_callback:
                self.alert_callback(alert)

        except asyncio.QueueFull:
            self._alerts_dropped += 1
            ALERTS_DROPPED_TOTAL.labels(reason="queue_full").inc()
            logger.warning(
                f"Alert queue full ({self.max_queue_size}), dropping alert from {alert.target_name}"
            )

    def _schedule_baseline_pull(self, target_id: int) -> None:
        """Fire a one-shot baseline pull for a target without blocking the caller."""
        if not self.baseline_pull_enabled:
            return
        task = asyncio.create_task(self._pull_baseline_for(target_id))
        self._baseline_jobs.add(task)
        task.add_done_callback(self._baseline_jobs.discard)

    async def _pull_baseline_for(self, target_id: int) -> None:
        """Pull existing log entries for a target, reusing subscriber credentials."""
        sub = self._subscribers.get(target_id) or self._webhook_subscribers.get(target_id)
        if not sub:
            return
        async with self._baseline_sem:
            await self._do_baseline_pull(target_id, sub)

    async def _do_baseline_pull(self, target_id: int, sub) -> None:
        """Run one baseline pull (concurrency already gated by caller)."""
        try:
            count = await pull_baseline_alerts(
                target_id=sub.target_id,
                target_name=sub.target_name,
                target_bmc=sub.target_bmc,
                base_url=sub.base_url,
                username=sub.username,
                password=sub.password,
                verify_ssl=sub.verify_ssl,
                callback=self._on_baseline_alert,
                severities=self.severities,
                max_entries_per_log=self.baseline_max_entries_per_log,
                # Incremental re-pull: only fetch entries newer than last seen.
                get_cursor=lambda uri, tid=target_id: self.repository.get_log_cursor(tid, uri),
                set_cursor=lambda uri, dt, tid=target_id: self.repository.set_log_cursor(
                    tid, uri, dt
                ),
                is_known=lambda sid, tid=target_id: self.repository.alert_source_seen(tid, sid),
            )
            if count:
                self._alerts_baseline_pulled += count
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Baseline pull task failed for target %d: %s: %s",
                target_id,
                type(e).__name__,
                e,
            )

    async def _baseline_repull_loop(self) -> None:
        """Periodically re-pull baselines to catch entries missed during outages."""
        interval = self.baseline_repull_interval_minutes * 60
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                target_ids = set(self._subscribers.keys()) | set(self._webhook_subscribers.keys())
                # _pull_baseline_for self-limits via _baseline_sem, so launching
                # them together bounds concurrency without serializing the fleet.
                await asyncio.gather(
                    *(self._pull_baseline_for(tid) for tid in target_ids),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Error in baseline re-pull loop: {type(e).__name__}: {e}", exc_info=True
                )
                await asyncio.sleep(60)

    async def _batch_processor_loop(self) -> None:
        """Process alerts in batches to reduce database load."""
        batch: list[AlertEvent] = []
        last_write = datetime.now(UTC)

        while self._running:
            try:
                # Wait for alert with timeout
                try:
                    alert = await asyncio.wait_for(self._alert_queue.get(), timeout=1.0)
                    batch.append(alert)
                except TimeoutError:
                    # Expected: 1s tick with no alerts is normal — the outer
                    # loop will still flush by elapsed-time even if no new
                    # items arrived in this tick.
                    pass

                # Write batch if full or interval elapsed
                now = datetime.now(UTC)
                should_write = len(batch) >= self.batch_size or (
                    batch and (now - last_write).total_seconds() >= self.batch_interval
                )

                if should_write:
                    if await self._write_batch(batch):
                        batch.clear()
                    else:
                        # DB write failed — keep the batch and retry instead of
                        # dropping it. Bound growth so a prolonged outage can't
                        # OOM: keep the newest, count the rest as dropped
                        # (visible in metrics), not silently lost.
                        cap = max(self.batch_size * 5, self.batch_size)
                        if len(batch) > cap:
                            overflow = len(batch) - cap
                            del batch[:overflow]
                            self._alerts_dropped += overflow
                            ALERTS_DROPPED_TOTAL.labels(reason="db_error").inc(overflow)
                            logger.error(
                                "Alert store write failing; dropped %d oldest buffered "
                                "alerts to bound memory (buffer cap %d)",
                                overflow,
                                cap,
                            )
                        # Throttle retries to batch_interval regardless of the
                        # size trigger, so a failing/struggling DB isn't hammered
                        # back-to-back while the queue has a backlog.
                        await asyncio.sleep(self.batch_interval)
                    last_write = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in batch processor: {type(e).__name__}: {e}", exc_info=True)
                await asyncio.sleep(1)

        # Write remaining alerts on shutdown
        if batch:
            await self._write_batch(batch)

    async def _write_batch(self, batch: list[AlertEvent]) -> bool:
        """Write a batch of alerts to the database.

        Returns True on success, False if the DB write failed. The caller keeps
        the batch for retry on False rather than clearing it, so a transient
        Postgres blip does not silently discard alerts.
        """
        if not batch:
            return True

        try:
            written = await self.repository.create_alerts_batch(batch)
            self._alerts_written += written
            if written:
                ALERTS_WRITTEN_TOTAL.inc(written)
            ALERT_QUEUE_SIZE.set(self._alert_queue.qsize())
            logger.debug(
                f"Wrote {written}/{len(batch)} alerts to database "
                f"({len(batch) - written} deduplicated)"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to write alert batch: {type(e).__name__}: {e}", exc_info=True)
            return False

    async def _flush_batch(self) -> None:
        """Flush any remaining queued alerts."""
        batch = []
        while not self._alert_queue.empty():
            try:
                batch.append(self._alert_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if batch:
            await self._write_batch(batch)

    async def _cleanup_loop(self) -> None:
        """Periodically clean up old alerts."""
        # Short initial delay (not a full interval) so retention still runs even
        # when the collector is redeployed more often than cleanup_interval_hours,
        # which previously meant retention could effectively never run.
        first_delay = min(300, self.cleanup_interval_hours * 3600)
        interval = self.cleanup_interval_hours * 3600
        try:
            await asyncio.sleep(first_delay)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                await self._cleanup_old_alerts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {type(e).__name__}: {e}", exc_info=True)
            # Always wait a full interval before the next attempt — including
            # after a failure — so a down alert store can't spin this loop.
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _cleanup_old_alerts(self) -> None:
        """Delete alerts older than retention period."""
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        deleted = await self.repository.delete_alerts_before(cutoff)
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} alerts older than {self.retention_days} days")

    def get_stats(self) -> dict:
        """Get alert manager statistics with enhanced state information."""
        # Build subscriber stats for SSE
        sse_subscribers = [
            {
                "target_id": sub.target_id,
                "target_name": sub.target_name,
                "subscription_type": "sse",
                "is_running": sub.is_running,
                "state": sub.state.value,
                "consecutive_failures": sub.consecutive_failures,
                "failure_reason": sub.failure_reason,
                "time_in_state_hours": sub.time_in_current_state,
                "next_retry_time": sub.next_retry_time.isoformat() if sub.next_retry_time else None,
                "last_event_time": sub.last_event_time.isoformat() if sub.last_event_time else None,
            }
            for sub in self._subscribers.values()
        ]

        # Build subscriber stats for webhooks
        webhook_subscribers = [
            {
                "target_id": sub.target_id,
                "target_name": sub.target_name,
                "subscription_type": "webhook",
                "is_subscribed": sub.is_subscribed,
                "subscription_id": sub.subscription_id,
            }
            for sub in self._webhook_subscribers.values()
        ]

        return {
            "enabled": self.enabled,
            "running": self._running,
            "active_subscriptions": len(self._subscribers) + len(self._webhook_subscribers),
            "sse_subscriptions": len(self._subscribers),
            "webhook_subscriptions": len(self._webhook_subscribers),
            "permanently_failed": len(self._permanently_failed),
            "permanently_failed_targets": sorted(self._permanently_failed),
            "permanent_failure_retries": [
                {"target_id": tid, "next_retry_at": retry_at.isoformat()}
                for tid, retry_at in sorted(self._permanently_failed.items())
            ],
            "queue_size": self._alert_queue.qsize(),
            "alerts_received": self._alerts_received,
            "alerts_written": self._alerts_written,
            "alerts_dropped": self._alerts_dropped,
            "alerts_baseline_pulled": self._alerts_baseline_pulled,
            "baseline_jobs_active": len(self._baseline_jobs),
            # CPER decode counters + backlog-by-state, from the enrichment worker.
            **self._cper_worker.stats(),
            "subscribers": sse_subscribers + webhook_subscribers,
        }

    async def retry_subscription(self, target_id: int) -> bool:
        """Manually retry a failed subscription.

        Args:
            target_id: Target ID to retry

        Returns:
            True if subscription was resumed, False if not found
        """
        subscriber = self._subscribers.get(target_id)
        if not subscriber:
            return False

        logger.info(f"Manually retrying subscription for target {target_id}")
        await subscriber.resume()
        return True

    @property
    def is_running(self) -> bool:
        """Check if alert manager is running."""
        return self._running
