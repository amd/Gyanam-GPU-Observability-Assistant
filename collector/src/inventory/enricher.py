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
"""Background inventory enricher for the collector service.

Reads basic inventory from each reachable system **once** (and again only after
a staleness interval), reconciles the BMC-reported location against the
name-derived placement, updates the chassis height, and persists the result.
Unreachable systems are simply retried on the next pass, so a system that was
down at onboarding gets enriched when it recovers — without hammering it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from ..location.hostname_parser import parse_system_location
from ..redfish.client import RedfishClient
from ..redfish.report_discovery import discover_metric_reports
from ..util.timeutil import naive_utc_now as _naive_utc_now
from .collector import collect_inventory
from .reconcile import check_location

logger = logging.getLogger(__name__)


def _placement_dict(p) -> dict | None:
    if p is None:
        return None
    return {"hall": p.hall, "row": p.row, "rack": p.rack, "rack_u": p.rack_u}


async def apply_inventory(repository, target, inv, location_config) -> None:
    """Persist a collected :class:`Inventory`: inventory JSON, location
    reconciliation, chassis-height unit size, and BMC-authoritative placement.

    Shared by the background enricher and the on-demand Test-Connection path.
    Placement/height writes honour the precedence guard, so a manual placement
    is never clobbered.
    """
    name_placement = parse_system_location(target.name, target.host, location_config)
    check = check_location(name_placement, inv.placement)

    payload = inv.to_dict()
    payload["bmc_location"] = _placement_dict(inv.placement)
    payload["name_location"] = _placement_dict(name_placement)

    await repository.set_target_inventory(
        target.id,
        inventory_json=json.dumps(payload),
        source="redfish",
        location_check=check,
        updated_at=_naive_utc_now(),
    )

    if inv.placement is not None:
        inv.placement.height_u = inv.height_u or inv.placement.height_u
        await repository.set_target_location(target.id, inv.placement)
    elif inv.height_u:
        # Height-only update (BMC reported chassis height but no rack placement).
        # Goes through the precedence guard so a manual placement isn't touched.
        await repository.set_target_height(target.id, inv.height_u, source="redfish")


class InventoryEnricher:
    """Periodically fills basic inventory for systems that lack it (or is stale)."""

    def __init__(self, repository, inv_config, location_config):
        self._repo = repository
        self._cfg = inv_config
        self._loc_cfg = location_config
        self._sem = asyncio.Semaphore(inv_config.max_concurrent)

    async def run(self) -> None:
        """Run the enrichment loop until cancelled."""
        logger.info(
            "Inventory enricher started (every %ss, refresh after %sh)",
            self._cfg.collect_interval_seconds,
            self._cfg.refresh_interval_hours,
        )
        while True:
            try:
                await self._run_pass()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad pass must not kill the loop
                logger.error("Inventory enrichment pass failed: %s: %s", type(e).__name__, e)
            await asyncio.sleep(self._cfg.collect_interval_seconds)

    async def _run_pass(self) -> None:
        targets = await self._repo.get_active_targets()
        due = [t for t in targets if self._is_due(t) or self._needs_discovery(t)]
        if not due:
            return
        logger.info("Inventory enricher: %d system(s) due", len(due))
        await asyncio.gather(*(self._enrich_one(t) for t in due), return_exceptions=True)

    def _is_due(self, target) -> bool:
        if target.inventory_updated_at is None:
            return True  # never pulled — the one-time case
        if self._cfg.refresh_interval_hours <= 0:
            return False  # one-time only; never refresh
        age = _naive_utc_now() - target.inventory_updated_at
        return bool(age >= timedelta(hours=self._cfg.refresh_interval_hours))

    @staticmethod
    def _needs_discovery(target) -> bool:
        """An auto-mode target that hasn't discovered its metric reports yet.

        Keeps metric-report discovery from waiting on the inventory refresh
        window, so a freshly-inventoried fleet still gets its reports enumerated
        on the next pass.
        """
        return getattr(target, "metric_discovery_mode", "auto") == "auto" and not getattr(
            target, "discovered_reports", None
        )

    async def _enrich_one(self, target) -> None:
        async with self._sem:
            try:
                client = self._build_client(target)
                async with client:
                    ok, _ = await client.test_connection()
                    if not ok:
                        return  # unreachable now — retried next pass
                    inv = await collect_inventory(
                        client,
                        self._loc_cfg.default_unit_type,
                        self._loc_cfg.gpus_per_system,
                    )
                    # Discover metric reports in the same connected pass (only for
                    # auto-mode targets); independent of inventory so a telemetry-
                    # only BMC still gets its report list.
                    await self._discover_reports(target, client)
                if inv is not None:
                    await apply_inventory(self._repo, target, inv, self._loc_cfg)
            except Exception as e:  # noqa: BLE001 — never let one system break the batch
                logger.debug(
                    "Inventory enrich failed for %s: %s: %s", target.name, type(e).__name__, e
                )

    async def _discover_reports(self, target, client) -> None:
        """Auto-enumerate the target's MetricReports and cache them (best-effort)."""
        if getattr(target, "metric_discovery_mode", "auto") != "auto":
            return
        try:
            reports = await discover_metric_reports(
                client, exclude_aggregates=self._cfg.discovery_exclude_aggregate_reports
            )
        except Exception as e:  # noqa: BLE001 — discovery must never break enrichment
            logger.debug(
                "Metric-report discovery failed for %s: %s: %s",
                target.name,
                type(e).__name__,
                e,
            )
            return
        if reports:
            await self._repo.set_discovered_reports(target.id, reports)
            logger.debug("Discovered %d metric report(s) for %s", len(reports), target.name)

    def _build_client(self, target) -> RedfishClient:
        """Build a short-lived client for a one-off inventory read."""
        return RedfishClient(
            base_url=target.base_url,
            username=target.username,
            password=self._repo.decrypt_password(target),
            token=self._repo.decrypt_token(target),
            timeout=self._cfg.request_timeout,
            verify_ssl=target.verify_ssl,
        )
