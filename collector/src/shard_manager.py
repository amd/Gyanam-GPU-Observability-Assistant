# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Horizontal-sharding claim loop for the collector.

When sharding is enabled, each collector process runs one of these. It
periodically renews the leases it holds (a heartbeat), reclaims targets whose
owner's heartbeat went stale, and claims unowned targets up to the per-shard
cap. Target ownership lives in the shared ``shard_leases`` table, so scaling the
collector replica count up or down rebalances the fleet automatically — a dead
or removed collector's targets are picked up by the survivors within one lease
TTL, and newly-onboarded targets are claimed by whichever shard has capacity.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import suppress

from .shard_alloc import allocate, effective_cap

logger = logging.getLogger(__name__)


class ShardManager:
    """Runs the claim/renew/reclaim loop for one collector shard.

    Two balancing strategies (``config.balance``):
      - ``static``  — greedy id-order claiming up to the cap (``claim_shard_targets``).
      - ``dynamic`` — Rendezvous (HRW) hashing: compute this collector's balanced
        slice from the live-collector set and reconcile the lease table to match
        (``reconcile_shard_leases``). ``slot_token`` (when set) is the process's
        stable-ordinal claim, renewed each pass and released on shutdown.
    """

    def __init__(
        self,
        repository,
        collector_id: str,
        config,
        slot_token: str | None = None,
        slot_ordinal: int | None = None,
    ):
        self._repo = repository
        self._id = collector_id
        self._cfg = config  # ShardConfig
        self._slot_token = slot_token
        self._slot_ordinal = slot_ordinal
        self._owned = 0
        self._under_provisioned = False  # transition flag for the warning

    @property
    def owned_count(self) -> int:
        return self._owned

    async def claim_once(self) -> int:
        """One claim/renew pass; returns the number of targets now owned.

        Called synchronously at startup so the poller/subscribers have their slice
        before their first pass, and then on the renew loop.
        """
        if self._cfg.balance == "dynamic":
            self._owned = await self._reconcile_dynamic()
        else:
            self._owned = await self._repo.claim_shard_targets(
                self._id,
                self._cfg.max_targets_per_shard,
                self._cfg.lease_ttl_seconds,
            )
        return self._owned

    async def _reconcile_dynamic(self) -> int:
        """HRW pass: renew our ordinal, compute our balanced slice, reconcile leases."""
        # Keep our stable ordinal fresh so the collector id (and thus every HRW
        # weight that keys on it) stays put across the fleet. renew_collector_slot
        # re-asserts our SPECIFIC ordinal (never drifts to a new one); it returns
        # False only if a peer has taken our ordinal while we were stalled past the
        # slot TTL — a genuine identity loss. Continuing would double-own targets
        # under a now-shared id, so exit and let the container restart cleanly.
        if self._slot_token is not None and self._slot_ordinal is not None:
            try:
                held = await self._repo.renew_collector_slot(
                    self._slot_token, self._slot_ordinal, self._cfg.lease_ttl_seconds
                )
            except Exception as e:  # noqa: BLE001 — a transient DB error is not identity loss
                logger.warning(
                    "Slot renewal failed transiently for ordinal %s: %s", self._slot_ordinal, e
                )
                held = True
            if not held:
                logger.critical(
                    "Lost stable slot ordinal %d to another process (stalled past the "
                    "slot TTL?). Exiting so the container restarts with a fresh identity "
                    "instead of double-owning targets under '%s'.",
                    self._slot_ordinal,
                    self._id,
                )
                os._exit(1)
        # Live membership = collectors with a fresh stats row, plus self. Biased to
        # UNDERcount (short window) on purpose: overcounting would shrink everyone's
        # fair share and orphan the phantom node's targets, whereas undercounting
        # only causes bounded overlap the reconcile steal-guard absorbs.
        rows = await self._repo.get_collector_stats(self._cfg.membership_ttl_seconds)
        live = {r["collector_id"] for r in rows} | {self._id}
        target_ids = await self._repo.get_enabled_target_ids()
        cap = effective_cap(len(target_ids), len(live), self._cfg.max_targets_per_shard)
        self._warn_if_under_provisioned(len(target_ids), len(live))
        # allocate() is pure/CPU-bound — run it OUTSIDE the DB write txn so the
        # sha256 work never holds SQLite's write lock.
        mine = allocate(target_ids, sorted(live), cap).get(self._id, [])
        return int(
            await self._repo.reconcile_shard_leases(
                self._id, mine, cap, self._cfg.lease_ttl_seconds
            )
        )

    def _warn_if_under_provisioned(self, n_targets: int, n_live: int) -> None:
        """Log once (on transition) when the fair share exceeds the safety cap, i.e.
        capacity (live x cap) < fleet so some targets can't be covered. Evaluated on
        the live set each pass, so it self-clears after a cold-start blip rather than
        firing spuriously forever (fixes the old startup one-shot)."""
        fair = math.ceil(n_targets / max(1, n_live))
        under = fair > self._cfg.max_targets_per_shard
        if under and not self._under_provisioned:
            logger.warning(
                "Fleet under-provisioned: fair share %d targets/collector exceeds the "
                "cap %d (%d live collectors, %d enabled targets). ~%d targets will be "
                "unpolled — add collector replicas or raise MAX_TARGETS_PER_SHARD.",
                fair,
                self._cfg.max_targets_per_shard,
                n_live,
                n_targets,
                n_targets - n_live * self._cfg.max_targets_per_shard,
            )
        elif not under and self._under_provisioned:
            logger.info(
                "Fleet provisioning recovered: fair share %d targets/collector is within "
                "the cap %d (%d live collectors).",
                fair,
                self._cfg.max_targets_per_shard,
                n_live,
            )
        self._under_provisioned = under

    async def run(self) -> None:
        """Renew/claim on an interval until cancelled."""
        logger.info(
            "Shard manager started (id=%s, cap=%d, interval=%ss, ttl=%ss)",
            self._id,
            self._cfg.max_targets_per_shard,
            self._cfg.claim_interval_seconds,
            self._cfg.lease_ttl_seconds,
        )
        while True:
            try:
                # Claim/renew BEFORE sleeping: run() is created at the end of startup
                # (after exporter connect, poller/SSE/alert start), so sleeping first
                # would leave this replica's just-claimed leases and slot un-renewed
                # for startup_duration + claim_interval — long enough on a slow start
                # to let them go stale and flap. Renewing first bounds the gap to the
                # startup duration alone.
                prev = self._owned
                await self.claim_once()
                if self._owned != prev:
                    logger.info("Shard %s now owns %d target(s)", self._id, self._owned)
                await asyncio.sleep(self._cfg.claim_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — a bad pass must not kill the loop
                logger.error("Shard claim pass failed: %s: %s", type(e).__name__, e)

    async def release(self) -> None:
        """Release this collector's leases (and stable-ordinal slot) so peers
        rebalance immediately on graceful shutdown."""
        try:
            await self._repo.release_shard_leases(self._id)
            logger.info("Released shard leases for %s", self._id)
        except Exception as e:  # noqa: BLE001 — shutdown best-effort, but make it visible
            logger.warning("Failed to release shard leases for %s: %s", self._id, e)
        if self._slot_token is not None:
            with suppress(Exception):
                await self._repo.release_collector_slot(self._slot_token)
