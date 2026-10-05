# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Automated diagnostic-log-collection policy engine.

Evaluates incoming Redfish alerts against the configured policy and, on a
fatal/critical event, triggers a diagnostic-log collection — enforcing a
per-target rearm window (hysteresis) so an event storm can't drive a tight
collection loop. This is the executing side of the Redfish PolicyService gyanam
exposes read-only (see src/api/routes/redfish.py): TriggerCondition.Type=Event
-> Response.ResponseAction=HTTP POST CollectDiagnosticData.

Runs in the collector process, co-located with alert ingestion (AlertManager)
and a LogCollector. All work is best-effort: a failure to evaluate or collect
is logged and never propagates back into the alert pipeline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive DB timestamp to aware UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class PolicyEngine:
    """Decides whether an alert should trigger diagnostic-log collection."""

    def __init__(self, repository, log_collector, config, now=None):
        self._repo = repository
        self._log_collector = log_collector
        self._cfg = config
        # Injectable clock (aware UTC) for deterministic tests.
        self._now = now or (lambda: datetime.now(UTC))

    def _severity_matches(self, alert) -> bool:
        sev = (getattr(alert, "severity", "") or "").casefold()
        return sev in {s.casefold() for s in self._cfg.trigger_severities}

    def _message_id_matches(self, alert) -> bool:
        allow = self._cfg.trigger_message_ids
        if not allow:
            return True  # no allow-list -> any MessageId at a triggering severity
        return (getattr(alert, "message_id", None) or "") in set(allow)

    async def _rearm_active(self, target_id: int) -> bool:
        """True if a policy collection for this target is within the rearm window."""
        last = _as_aware_utc(await self._repo.get_last_policy_collection_time(target_id))
        if last is None:
            return False
        elapsed = (self._now() - last).total_seconds()
        return bool(elapsed < self._cfg.rearm_seconds)

    async def on_alert(self, alert) -> bool:
        """Evaluate the policy for one alert; collect if it fires.

        Returns True only when a collection was actually triggered. Never raises.
        """
        try:
            if not self._cfg.enabled or self._cfg.operating_mode == "Disabled":
                return False
            if not self._severity_matches(alert) or not self._message_id_matches(alert):
                return False

            target_id = getattr(alert, "target_id", None)
            if target_id is None:
                return False

            if await self._rearm_active(target_id):
                logger.info(
                    "Policy: rearm active for target %s (< %ss since last collection); "
                    "skipping diagnostic collection",
                    target_id,
                    self._cfg.rearm_seconds,
                )
                return False

            if self._cfg.operating_mode != "Enabled":
                # AlertOnly: evaluate + log, but do not collect.
                logger.info(
                    "Policy (AlertOnly): would collect diagnostics for target %s on %s",
                    target_id,
                    getattr(alert, "message_id", None),
                )
                return False

            logger.info(
                "Policy: collecting diagnostics for target %s (severity=%s, MessageId=%s)",
                target_id,
                getattr(alert, "severity", None),
                getattr(alert, "message_id", None),
            )
            result = await self._log_collector.collect_single(
                target_id,
                trigger="policy",
                trigger_message_id=getattr(alert, "message_id", None),
            )
            if not result.get("success"):
                logger.warning(
                    "Policy: diagnostic collection for target %s did not succeed: %s",
                    target_id,
                    result.get("error"),
                )
            return bool(result.get("success"))
        except Exception as e:  # noqa: BLE001 — must never break the alert pipeline
            logger.error("Policy engine error: %s: %s", type(e).__name__, e, exc_info=True)
            return False
