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
"""Webhook-based alert subscriber for Redfish BMCs that don't support SSE.

This module implements alert collection via Redfish Event Subscriptions (webhooks)
as a fallback for BMCs with broken or missing SSE support.
"""

import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import httpx

from .alert_subscriber import (
    AlertEvent,
    normalize_severity,
    parse_redfish_timestamp,
    severity_allowed,
)
from .http_client import make_bmc_client

logger = logging.getLogger(__name__)


class SubscriptionFailureType(Enum):
    """Types of webhook subscription failures."""

    TEMPORARY = "temporary"  # Network errors, timeouts - can retry
    PERMANENT = "permanent"  # Configuration errors, unsupported - don't retry


@dataclass
class SubscriptionResult:
    """Result of webhook subscription attempt."""

    success: bool
    failure_type: SubscriptionFailureType | None = None
    error_message: str | None = None


class WebhookSubscriber:
    """Manages webhook-based alert subscriptions for a single target.

    Instead of the collector connecting to the BMC's SSE stream,
    the BMC pushes events to the collector's webhook endpoint.
    """

    def __init__(
        self,
        target_id: int,
        target_name: str,
        target_bmc: str,
        base_url: str,
        username: str,
        password: str,
        webhook_url: str,
        verify_ssl: bool = False,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
    ):
        """Initialize webhook subscriber.

        Args:
            target_id: Target database ID
            target_name: Target display name
            target_bmc: Target BMC address
            base_url: Redfish API base URL
            username: Basic auth username
            password: Basic auth password
            webhook_url: URL where BMC will POST events (collector endpoint)
            verify_ssl: Whether to verify SSL certificates
            event_types: Event types to subscribe to
            severities: Alert severities to subscribe to (Note: filtering done by collector)
        """
        self.target_id = target_id
        self.target_name = target_name
        self.target_bmc = target_bmc
        self.base_url = base_url
        self.username = username
        self.password = password
        self.webhook_url = webhook_url
        self.verify_ssl = verify_ssl
        self.event_types = event_types or ["Alert", "StatusChange"]
        self.severities = severities or ["Warning", "Critical"]

        self._subscription_id: str | None = None
        self._subscription_url: str | None = None

    async def create_subscription(self) -> SubscriptionResult:
        """Create webhook subscription on the BMC.

        Returns:
            SubscriptionResult with success status and failure classification
        """
        subscription_payload = {
            "Destination": self.webhook_url,
            "Protocol": "Redfish",
            "EventTypes": self.event_types,
            "Context": f"target_{self.target_id}",
        }

        # Some BMCs support severity filtering in subscription
        # But many don't, so we filter on our side
        # subscription_payload["EventFormatType"] = "Event"

        try:
            async with make_bmc_client(
                auth=httpx.BasicAuth(self.username, self.password),
                verify_ssl=self.verify_ssl,
                timeout=30.0,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/redfish/v1/EventService/Subscriptions",
                    json=subscription_payload,
                )

                if response.status_code in (200, 201):
                    # Subscription created
                    subscription_data = response.json()
                    self._subscription_id = subscription_data.get("Id")
                    self._subscription_url = response.headers.get(
                        "Location",
                        f"{self.base_url}/redfish/v1/EventService/Subscriptions/{self._subscription_id}",
                    )

                    logger.info(
                        f"Created webhook subscription for {self.target_name}: "
                        f"ID={self._subscription_id}, URL={self.webhook_url}"
                    )
                    return SubscriptionResult(success=True)

                elif response.status_code == 409:
                    # Subscription may already exist
                    logger.warning(
                        f"Webhook subscription for {self.target_name} may already exist (409 Conflict)"
                    )
                    # Try to find existing subscription
                    await self._find_existing_subscription()
                    if self._subscription_id is not None:
                        return SubscriptionResult(success=True)
                    else:
                        return SubscriptionResult(
                            success=False,
                            failure_type=SubscriptionFailureType.TEMPORARY,
                            error_message="409 Conflict but could not find existing subscription",
                        )

                elif response.status_code == 400:
                    # HTTP 400 Bad Request - permanent configuration error
                    error_text = response.text

                    # Check for specific permanent failure conditions
                    is_permanent = (
                        "PropertyValueFormatError" in error_text
                        or "PropertyValueNotInList" in error_text
                        or "PropertyUnknown" in error_text
                        or "localhost" in self.webhook_url  # localhost is never reachable from BMC
                    )

                    failure_type = (
                        SubscriptionFailureType.PERMANENT
                        if is_permanent
                        else SubscriptionFailureType.TEMPORARY
                    )

                    logger.error(
                        f"Failed to create webhook subscription for {self.target_name}: "
                        f"HTTP 400 - {error_text[:500]} "
                        f"[{'PERMANENT' if is_permanent else 'TEMPORARY'} failure]"
                    )
                    logger.debug(f"Webhook payload sent: {subscription_payload}")

                    return SubscriptionResult(
                        success=False,
                        failure_type=failure_type,
                        error_message=f"HTTP 400: {error_text[:200]}",
                    )

                elif response.status_code in (501, 405):
                    # Not Implemented or Method Not Allowed - permanent
                    logger.error(
                        f"Webhook subscriptions not supported for {self.target_name}: "
                        f"HTTP {response.status_code}"
                    )
                    return SubscriptionResult(
                        success=False,
                        failure_type=SubscriptionFailureType.PERMANENT,
                        error_message=f"HTTP {response.status_code}: Webhooks not supported",
                    )

                else:
                    # Other errors - treat as temporary
                    logger.error(
                        f"Failed to create webhook subscription for {self.target_name}: "
                        f"HTTP {response.status_code} - {response.text[:200]}"
                    )
                    return SubscriptionResult(
                        success=False,
                        failure_type=SubscriptionFailureType.TEMPORARY,
                        error_message=f"HTTP {response.status_code}",
                    )

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # Network errors - temporary
            logger.error(
                f"Network error creating webhook subscription for {self.target_name}: "
                f"{type(e).__name__}: {e}"
            )
            return SubscriptionResult(
                success=False,
                failure_type=SubscriptionFailureType.TEMPORARY,
                error_message=f"Network error: {type(e).__name__}",
            )

        except Exception as e:
            # Unknown errors - treat as temporary to be safe
            logger.error(
                f"Error creating webhook subscription for {self.target_name}: "
                f"{type(e).__name__}: {e}"
            )
            return SubscriptionResult(
                success=False,
                failure_type=SubscriptionFailureType.TEMPORARY,
                error_message=f"{type(e).__name__}: {str(e)[:100]}",
            )

    async def _reconcile_by_context(self) -> bool:
        """Reconcile BMC subscriptions for this target, matched by our Context.

        Deletes subscriptions tagged ``target_{id}`` that point at a different
        destination (orphans left by a crashed/relocated collector) and adopts
        one that already points at our destination.

        Returns True if an existing subscription with our destination was adopted
        (so the caller can skip creating a duplicate); False otherwise.
        """
        context = f"target_{self.target_id}"
        try:
            async with make_bmc_client(
                auth=httpx.BasicAuth(self.username, self.password),
                verify_ssl=self.verify_ssl,
                timeout=10.0,
            ) as client:
                resp = await client.get(f"{self.base_url}/redfish/v1/EventService/Subscriptions")
                if resp.status_code != 200:
                    return False
                adopted = False
                for member in resp.json().get("Members", []):
                    odata = member.get("@odata.id")
                    if not odata:
                        continue
                    detail = await client.get(f"{self.base_url}{odata}")
                    if detail.status_code != 200:
                        continue
                    data = detail.json()
                    if data.get("Context") != context:
                        continue
                    sub_id = odata.split("/")[-1]
                    if data.get("Destination") == self.webhook_url:
                        # Ours already exists — adopt the first match rather than
                        # create a duplicate, but keep scanning so additional
                        # orphans for this target are still cleaned up.
                        if not adopted:
                            self._subscription_id = sub_id
                            self._subscription_url = f"{self.base_url}{odata}"
                            adopted = True
                            logger.info(
                                "Adopted existing webhook subscription for %s: %s",
                                self.target_name,
                                sub_id,
                            )
                        continue
                    # Orphan from a collector that no longer owns this target.
                    with suppress(Exception):
                        d = await client.delete(f"{self.base_url}{odata}")
                        if d.status_code in (200, 204, 404):
                            logger.warning(
                                "Deleted stale webhook subscription %s for %s "
                                "(orphaned destination %s)",
                                sub_id,
                                self.target_name,
                                data.get("Destination"),
                            )
                return adopted
        except Exception as e:  # noqa: BLE001 — reconcile is best-effort
            logger.debug("Webhook reconcile-by-context failed for %s: %s", self.target_name, e)
        return False

    async def _find_existing_subscription(self) -> None:
        """Try to find existing subscription matching our webhook URL."""
        try:
            async with make_bmc_client(
                auth=httpx.BasicAuth(self.username, self.password),
                verify_ssl=self.verify_ssl,
                timeout=10.0,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/redfish/v1/EventService/Subscriptions"
                )

                if response.status_code == 200:
                    subscriptions = response.json().get("Members", [])
                    for sub in subscriptions:
                        sub_id = sub.get("@odata.id", "").split("/")[-1]
                        # Fetch subscription details
                        sub_response = await client.get(f"{self.base_url}{sub.get('@odata.id')}")
                        if sub_response.status_code == 200:
                            sub_data = sub_response.json()
                            if sub_data.get("Destination") == self.webhook_url:
                                self._subscription_id = sub_id
                                self._subscription_url = f"{self.base_url}{sub.get('@odata.id')}"
                                logger.info(
                                    f"Found existing subscription for {self.target_name}: {sub_id}"
                                )
                                return
        except Exception as e:
            logger.debug(f"Error finding existing subscription: {e}")

    async def delete_subscription(self) -> bool:
        """Delete webhook subscription from the BMC.

        Returns:
            True if subscription deleted successfully
        """
        if not self._subscription_url:
            logger.warning(f"No subscription URL to delete for {self.target_name}")
            return False

        try:
            async with make_bmc_client(
                auth=httpx.BasicAuth(self.username, self.password),
                verify_ssl=self.verify_ssl,
                timeout=10.0,
            ) as client:
                response = await client.delete(self._subscription_url)

                if response.status_code in (200, 204):
                    logger.info(f"Deleted webhook subscription for {self.target_name}")
                    self._subscription_id = None
                    self._subscription_url = None
                    return True
                elif response.status_code == 404:
                    logger.info(
                        f"Webhook subscription for {self.target_name} already deleted (404)"
                    )
                    self._subscription_id = None
                    self._subscription_url = None
                    return True
                else:
                    logger.error(
                        f"Failed to delete webhook subscription for {self.target_name}: "
                        f"HTTP {response.status_code}"
                    )
                    return False

        except Exception as e:
            logger.error(
                f"Error deleting webhook subscription for {self.target_name}: "
                f"{type(e).__name__}: {e}"
            )
            return False

    async def verify_subscription(self) -> bool:
        """Verify subscription still exists on BMC.

        Returns:
            True if subscription exists and is active
        """
        if not self._subscription_url:
            return False

        try:
            async with make_bmc_client(
                auth=httpx.BasicAuth(self.username, self.password),
                verify_ssl=self.verify_ssl,
                timeout=10.0,
            ) as client:
                response = await client.get(self._subscription_url)
                return bool(response.status_code == 200)

        except Exception as e:  # noqa: BLE001 — verification is best-effort
            logger.warning(
                f"Failed to verify webhook subscription for {self.target_name}: "
                f"{type(e).__name__}: {e}"
            )
            return False

    def parse_webhook_event(self, event_data: dict) -> list[AlertEvent]:
        """Parse webhook event from BMC into AlertEvent objects.

        Args:
            event_data: JSON payload from BMC webhook POST

        Returns:
            List of AlertEvent objects (Redfish events can contain multiple events)
        """
        alerts = []

        # Redfish webhook format:
        # {
        #   "@odata.type": "#Event.v1_x_x.Event",
        #   "Events": [...]
        # }
        # Some BMCs POST a single bare event object (no "Events" wrapper); accept
        # that shape too instead of silently dropping it.
        events = event_data.get("Events")
        if not isinstance(events, list):
            events = (
                [event_data] if (event_data.get("MessageId") or event_data.get("Message")) else []
            )

        for event in events:
            if not isinstance(event, dict):
                continue
            # Redfish v1.7+ deprecated EventType; only filter on it when present.
            event_type = event.get("EventType") or ""
            severity, sev_present = normalize_severity(event)

            if event_type and self.event_types and event_type not in self.event_types:
                logger.debug(
                    "Dropping webhook event from %s: event_type=%r not in %s",
                    self.target_name,
                    event_type,
                    self.event_types,
                )
                continue
            if not severity_allowed(severity, sev_present, self.severities):
                logger.debug(
                    "Dropping webhook event from %s: severity=%r not in %s",
                    self.target_name,
                    severity,
                    self.severities,
                )
                continue

            if not event_type:
                event_type = "Alert"

            # Parse timestamp (tolerant of sloppy BMC formats)
            event_ts = parse_redfish_timestamp(event.get("EventTimestamp"))

            # Extract origin of condition
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
                raw=event,
            )

            alerts.append(alert)

        return alerts

    @property
    def subscription_id(self) -> str | None:
        """Get subscription ID."""
        return self._subscription_id

    @property
    def is_subscribed(self) -> bool:
        """Check if subscription is active."""
        return self._subscription_id is not None
