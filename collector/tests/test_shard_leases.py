# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for horizontal-sharding lease claim/reclaim/ownership logic."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text


async def _make_targets(repo, n):
    return [
        await repo.create_target(name=f"t{i}", host=f"10.0.0.{i}", username="u", password="p")
        for i in range(n)
    ]


async def _age_leases(repo, collector_id, seconds):
    """Backdate a collector's lease heartbeats to simulate a dead owner."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).replace(tzinfo=None)
    async with repo.session_factory() as s:
        await s.execute(
            text("UPDATE shard_leases SET updated_at=:t WHERE collector_id=:c"),
            {"t": old, "c": collector_id},
        )
        await s.commit()


# ---- claim / cap -----------------------------------------------------------


async def test_single_collector_claims_up_to_cap(repo):
    await _make_targets(repo, 5)
    owned = await repo.claim_shard_targets("A", max_targets=3, lease_ttl_seconds=60)
    assert owned == 3
    assert len(await repo.get_owned_targets("A", 60)) == 3


async def test_two_collectors_split_without_overlap(repo):
    await _make_targets(repo, 5)
    a = await repo.claim_shard_targets("A", max_targets=3, lease_ttl_seconds=60)
    b = await repo.claim_shard_targets("B", max_targets=3, lease_ttl_seconds=60)
    assert a == 3 and b == 2  # B only gets the 2 A left
    a_ids = {t.id for t in await repo.get_owned_targets("A", 60)}
    b_ids = {t.id for t in await repo.get_owned_targets("B", 60)}
    assert a_ids.isdisjoint(b_ids)
    assert len(a_ids | b_ids) == 5  # every target owned exactly once


async def test_fresh_leases_are_not_stolen(repo):
    await _make_targets(repo, 4)
    await repo.claim_shard_targets("A", max_targets=4, lease_ttl_seconds=60)
    # B tries to claim while A's leases are fresh -> gets nothing.
    b = await repo.claim_shard_targets("B", max_targets=4, lease_ttl_seconds=60)
    assert b == 0
    assert len(await repo.get_owned_targets("A", 60)) == 4


# ---- reclaim on dead owner -------------------------------------------------


async def test_stale_leases_reclaimed_by_peer(repo):
    await _make_targets(repo, 3)
    await repo.claim_shard_targets("A", max_targets=3, lease_ttl_seconds=60)
    await _age_leases(repo, "A", seconds=300)  # A "died"
    b = await repo.claim_shard_targets("B", max_targets=3, lease_ttl_seconds=75)
    assert b == 3  # B reclaims all of A's stale leases
    assert len(await repo.get_owned_targets("A", 75)) == 0
    assert len(await repo.get_owned_targets("B", 75)) == 3


async def test_renew_keeps_ownership(repo):
    await _make_targets(repo, 2)
    await repo.claim_shard_targets("A", max_targets=2, lease_ttl_seconds=60)
    # A heartbeats again -> still owns both, no duplicates.
    owned = await repo.claim_shard_targets("A", max_targets=2, lease_ttl_seconds=60)
    assert owned == 2


# ---- sweep / release -------------------------------------------------------


async def test_disabled_target_lease_swept(repo):
    targets = await _make_targets(repo, 2)
    await repo.claim_shard_targets("A", max_targets=2, lease_ttl_seconds=60)
    await repo.update_target(targets[0].id, enabled=False)
    owned = await repo.claim_shard_targets("A", max_targets=2, lease_ttl_seconds=60)
    assert owned == 1  # disabled target's lease swept
    assert {t.id for t in await repo.get_owned_targets("A", 60)} == {targets[1].id}


async def test_release_shard_leases_frees_everything(repo):
    await _make_targets(repo, 3)
    await repo.claim_shard_targets("A", max_targets=3, lease_ttl_seconds=60)
    await repo.release_shard_leases("A")
    assert len(await repo.get_owned_targets("A", 60)) == 0
    # a peer can immediately claim them
    assert await repo.claim_shard_targets("B", max_targets=3, lease_ttl_seconds=60) == 3


# ---- collector stats -------------------------------------------------------


# ---- ShardManager ----------------------------------------------------------


class _FakeShardRepo:
    def __init__(self):
        self.claims = 0
        self.released = False

    async def claim_shard_targets(self, cid, cap, ttl):
        self.claims += 1
        return 5

    async def release_shard_leases(self, cid):
        self.released = True


async def test_shard_manager_claim_once_delegates():
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    r = _FakeShardRepo()
    m = ShardManager(r, "A", ShardConfig(max_targets_per_shard=10, balance="static"))
    assert await m.claim_once() == 5
    assert m.owned_count == 5 and r.claims == 1


async def test_shard_manager_release_delegates():
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    r = _FakeShardRepo()
    await ShardManager(r, "A", ShardConfig()).release()
    assert r.released is True


async def test_shard_manager_run_claims_then_exits_on_cancel(monkeypatch):
    import asyncio

    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    r = _FakeShardRepo()
    m = ShardManager(r, "A", ShardConfig(balance="static"))
    calls = [0]

    async def sleep_then_cancel(_seconds):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", sleep_then_cancel)
    await m.run()  # loop: sleep ok -> claim -> sleep raises -> break
    assert r.claims >= 1


class _FakeDynamicRepo:
    def __init__(self, enabled_ids, live_ids, renew=True, renew_raises=False):
        self.enabled_ids = enabled_ids
        self.live_ids = live_ids
        self.renew = renew
        self.renew_raises = renew_raises
        self.released = False
        self.slot_released = False
        self.reconciled_with = None

    async def renew_collector_slot(self, token, ordinal, ttl):
        if self.renew_raises:
            raise RuntimeError("transient db blip")
        return self.renew

    async def get_collector_stats(self, ttl):
        return [{"collector_id": c} for c in self.live_ids]

    async def get_enabled_target_ids(self):
        return list(self.enabled_ids)

    async def reconcile_shard_leases(self, cid, mine, cap, ttl):
        self.reconciled_with = (cid, list(mine), cap)
        return len(mine)

    async def release_shard_leases(self, cid):
        self.released = True

    async def release_collector_slot(self, token):
        self.slot_released = True


async def test_shard_manager_dynamic_reconcile_and_release_with_slot():
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    r = _FakeDynamicRepo(
        enabled_ids=list(range(1, 31)), live_ids=["collector-0", "collector-1", "collector-2"]
    )
    cfg = ShardConfig(balance="dynamic", max_targets_per_shard=200)
    m = ShardManager(r, "collector-0", cfg, slot_token="hostX", slot_ordinal=0)
    owned = await m.claim_once()
    assert owned >= 1 and r.reconciled_with[0] == "collector-0"
    # cap passed to reconcile is the fair share ceil(30/3)=10, not the raw max.
    assert r.reconciled_with[2] == 10
    await m.release()
    assert r.released is True and r.slot_released is True


async def test_shard_manager_dynamic_transient_renew_error_continues():
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    # A transient slot-renew error is NOT identity loss -> held=True, pass continues.
    r = _FakeDynamicRepo(enabled_ids=[1, 2, 3], live_ids=["collector-0"], renew_raises=True)
    m = ShardManager(
        r, "collector-0", ShardConfig(balance="dynamic"), slot_token="hostX", slot_ordinal=0
    )
    assert await m.claim_once() == 3  # reconciled despite the renew blip
    assert r.reconciled_with is not None


async def test_shard_manager_dynamic_slot_loss_exits(monkeypatch):
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    # renew returns False (a peer took our ordinal) -> fatal os._exit for a clean
    # restart. Patch os._exit to raise instead of killing the test process.
    r = _FakeDynamicRepo(enabled_ids=[1, 2], live_ids=["collector-0"], renew=False)
    m = ShardManager(
        r, "collector-0", ShardConfig(balance="dynamic"), slot_token="hostX", slot_ordinal=0
    )

    def _fake_exit(code):
        raise SystemExit(code)

    import src.shard_manager as sm

    monkeypatch.setattr(sm.os, "_exit", _fake_exit)
    import pytest

    with pytest.raises(SystemExit):
        await m.claim_once()
    assert r.reconciled_with is None  # never reached reconcile


async def test_shard_manager_under_provision_warning_transition(caplog):
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    cfg = ShardConfig(balance="dynamic", max_targets_per_shard=100)
    # 300 targets, 1 live collector, cap 100 -> fair share 300 > 100 -> under-provisioned.
    r = _FakeDynamicRepo(enabled_ids=list(range(1, 301)), live_ids=["collector-0"])
    m = ShardManager(r, "collector-0", cfg)
    with caplog.at_level("WARNING"):
        await m.claim_once()
    assert "under-provisioned" in caplog.text
    # Now 4 collectors -> fair share 75 <= cap 100 -> recovery (info), flag clears.
    r.live_ids = ["collector-0", "collector-1", "collector-2", "collector-3"]
    with caplog.at_level("INFO"):
        await m.claim_once()
    assert "provisioning recovered" in caplog.text


async def test_collector_stats_roundtrip_and_staleness(repo):
    await repo.upsert_collector_stats("A", {"active_subscriptions": 7}, owned_targets=42)
    rows = await repo.get_collector_stats(max_age_seconds=120)
    assert len(rows) == 1
    assert rows[0]["collector_id"] == "A" and rows[0]["owned_targets"] == 42
    assert rows[0]["active_subscriptions"] == 7
    # stale rows drop out
    assert await repo.get_collector_stats(max_age_seconds=-1) == []


async def test_api_aggregates_collector_stats_across_shards(repo):
    from src.api.routes.alerts import _aggregate_collector_stats

    # Two shards, disjoint targets: subscriber lists concat, counters sum.
    await repo.upsert_collector_stats(
        "A",
        {
            "alerts_received": 10,
            "active_subscriptions": 2,
            "subscribers": [{"target_id": 1}, {"target_id": 2}],
            "permanently_failed_targets": [9],
        },
        owned_targets=2,
    )
    await repo.upsert_collector_stats(
        "B",
        {
            "alerts_received": 5,
            "active_subscriptions": 1,
            "subscribers": [{"target_id": 3}],
            "permanently_failed_targets": [],
        },
        owned_targets=1,
    )
    merged = await _aggregate_collector_stats(repo)
    assert merged["enabled"] is True and merged["collector_count"] == 2
    assert merged["alerts_received"] == 15 and merged["active_subscriptions"] == 3
    assert {s["target_id"] for s in merged["subscribers"]} == {1, 2, 3}
    assert merged["permanently_failed_targets"] == [9]


async def test_api_aggregate_none_when_no_collectors(repo):
    from src.api.routes.alerts import _aggregate_collector_stats

    assert await _aggregate_collector_stats(repo) is None  # falls back to HTTP proxy


async def test_api_aggregate_returns_none_on_db_error():
    from src.api.routes.alerts import _aggregate_collector_stats

    class _BoomRepo:
        async def get_collector_stats(self, *a, **k):
            raise RuntimeError("control DB unreachable")

    # A DB blip must degrade (return None -> caller falls back to the HTTP proxy /
    # empty state), NOT propagate a 500 to the alerts UI.
    assert await _aggregate_collector_stats(_BoomRepo()) is None


async def test_api_aggregate_dedups_target_during_handoff(repo):
    """During a lease handoff both the old and new owner report the same target.
    The aggregate must dedup by target_id (newest wins) and NOT double-count."""
    from src.api.routes.alerts import _aggregate_collector_stats

    # Old owner A still reporting target 1 (sse) + its own alert counters.
    await repo.upsert_collector_stats(
        "A",
        {
            "alerts_received": 10,
            "subscribers": [{"target_id": 1, "subscription_type": "sse"}],
            "permanently_failed_targets": [9],
        },
        owned_targets=1,
    )
    # New owner B now also reports target 1 (as webhook), plus target 2.
    await repo.upsert_collector_stats(
        "B",
        {
            "alerts_received": 5,
            "subscribers": [
                {"target_id": 1, "subscription_type": "webhook"},
                {"target_id": 2, "subscription_type": "sse"},
            ],
            "permanently_failed_targets": [9],
        },
        owned_targets=2,
    )
    merged = await _aggregate_collector_stats(repo)
    # target 1 counted once, not twice -> 2 distinct subscriptions (tids 1,2).
    assert merged["active_subscriptions"] == 2
    assert {s["target_id"] for s in merged["subscribers"]} == {1, 2}
    # permanently_failed_targets deduped to a single 9.
    assert merged["permanently_failed_targets"] == [9]
    assert merged["permanently_failed"] == 1
    # True cumulative counters still sum (each alert received once per collector).
    assert merged["alerts_received"] == 15


async def test_api_aggregate_carries_cper_and_baseline_counters(repo):
    from src.api.routes.alerts import _aggregate_collector_stats

    await repo.upsert_collector_stats(
        "A",
        {
            "alerts_received": 1,
            "cper_decoded": 3,
            "cper_failed": 1,
            "alerts_baseline_pulled": 5,
            "baseline_jobs_active": 1,
            "cper_backlog": {"pending": 2, "decoded": 3},
        },
        owned_targets=1,
    )
    await repo.upsert_collector_stats(
        "B",
        {
            "alerts_received": 1,
            "cper_decoded": 4,
            "cper_failed": 0,
            "alerts_baseline_pulled": 2,
            "baseline_jobs_active": 2,
            "cper_backlog": {"pending": 1, "fetch_failed": 1},
        },
        owned_targets=1,
    )
    merged = await _aggregate_collector_stats(repo)
    # CPER/baseline cumulative counters sum across shards...
    assert merged["cper_decoded"] == 7 and merged["cper_failed"] == 1
    assert merged["alerts_baseline_pulled"] == 7 and merged["baseline_jobs_active"] == 3
    # ...and the per-status backlog dict merges by summing each status.
    assert merged["cper_backlog"] == {"pending": 3, "decoded": 3, "fetch_failed": 1}


def test_shard_config_rejects_tight_ttl():
    import pytest
    from src.config import ShardConfig

    # ttl must be >= 2x claim interval; 30 < 2*20 -> rejected.
    with pytest.raises(ValueError, match="lease_ttl_seconds"):
        ShardConfig(claim_interval_seconds=20.0, lease_ttl_seconds=30.0)
    # A comfortable margin is accepted.
    ShardConfig(claim_interval_seconds=20.0, lease_ttl_seconds=75.0)


def test_shard_config_rejects_membership_ttl_above_lease_ttl():
    import pytest
    from src.config import ShardConfig

    # membership_ttl > lease_ttl would orphan a dead collector's targets (it stays
    # "live" after its leases go stale) -> rejected.
    with pytest.raises(ValueError, match="membership_ttl_seconds must be <="):
        ShardConfig(
            claim_interval_seconds=20.0, lease_ttl_seconds=75.0, membership_ttl_seconds=120.0
        )
    # membership_ttl <= lease_ttl is accepted.
    ShardConfig(claim_interval_seconds=20.0, lease_ttl_seconds=75.0, membership_ttl_seconds=75.0)


async def test_count_all_shard_leases(repo):
    await _make_targets(repo, 4)
    await repo.claim_shard_targets("A", max_targets=2, lease_ttl_seconds=60)
    await repo.claim_shard_targets("B", max_targets=2, lease_ttl_seconds=60)
    assert await repo.count_all_shard_leases(60) == 4


# ---- dynamic (HRW) reconcile ----------------------------------------------


async def _age_slots(repo, token, seconds):
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).replace(tzinfo=None)
    async with repo.session_factory() as s:
        await s.execute(
            text("UPDATE collector_slots SET updated_at=:t WHERE token=:k"),
            {"t": old, "k": token},
        )
        await s.commit()


async def test_reconcile_claims_exactly_my_slice(repo):
    targets = await _make_targets(repo, 6)
    ids = [t.id for t in targets]
    # cap == slice size -> no spare capacity for the orphan sweep (exact-slice test).
    a_owned = await repo.reconcile_shard_leases("A", ids[:3], 3, lease_ttl_seconds=60)
    b_owned = await repo.reconcile_shard_leases("B", ids[3:], 3, lease_ttl_seconds=60)
    assert a_owned == 3 and b_owned == 3
    a_ids = {t.id for t in await repo.get_owned_targets("A", 60)}
    b_ids = {t.id for t in await repo.get_owned_targets("B", 60)}
    assert a_ids == set(ids[:3])
    assert b_ids == set(ids[3:])
    assert a_ids.isdisjoint(b_ids)


async def test_reconcile_renew_only_my_slice_is_idempotent(repo):
    targets = await _make_targets(repo, 4)
    ids = [t.id for t in targets]
    assert await repo.reconcile_shard_leases("A", ids, 4, 60) == 4
    # Re-run with the same slice -> still 4, no duplicate rows.
    assert await repo.reconcile_shard_leases("A", ids, 4, 60) == 4
    assert await repo.count_all_shard_leases(60) == 4


async def test_reconcile_steal_guard_blocks_fresh_peer(repo):
    targets = await _make_targets(repo, 3)
    ids = [t.id for t in targets]
    await repo.reconcile_shard_leases("A", ids, 3, 60)
    # B disagrees and wants the same targets, but A's leases are fresh -> blocked
    # (and the sweep finds no unowned/stale orphans either).
    assert await repo.reconcile_shard_leases("B", ids, 3, 60) == 0
    assert len(await repo.get_owned_targets("A", 60)) == 3


async def test_reconcile_lazy_shed_is_gap_free_then_reclaimed(repo):
    targets = await _make_targets(repo, 4)
    ids = [t.id for t in targets]
    await repo.reconcile_shard_leases("A", ids, 4, 60)  # A owns all 4
    # A sheds the last 2 (drops them from its slice). Lazy shed = NOT renewed,
    # NOT deleted -> A still holds them (fresh) and keeps polling until they age.
    # cap=2 so the sweep has no spare capacity (A already holds 4 fresh).
    still = await repo.reconcile_shard_leases("A", ids[:2], 2, 60)
    assert still == 4  # continuous coverage: shed leases still fresh
    # B (new owner by HRW) cannot take them yet — still fresh.
    assert await repo.reconcile_shard_leases("B", ids[2:], 2, 60) == 0
    # Once A's leases go stale (A stopped renewing the shed two)...
    await _age_leases(repo, "A", seconds=300)
    assert await repo.reconcile_shard_leases("B", ids[2:], 2, 75) == 2  # B reclaims
    a_again = await repo.reconcile_shard_leases("A", ids[:2], 2, 75)  # A renews its own
    assert a_again == 2
    a_ids = {t.id for t in await repo.get_owned_targets("A", 75)}
    b_ids = {t.id for t in await repo.get_owned_targets("B", 75)}
    assert a_ids == set(ids[:2]) and b_ids == set(ids[2:])  # full coverage, disjoint


async def test_reconcile_sweeps_disabled(repo):
    targets = await _make_targets(repo, 3)
    ids = [t.id for t in targets]
    await repo.reconcile_shard_leases("A", ids, 3, 60)
    await repo.update_target(ids[0], enabled=False)
    owned = await repo.reconcile_shard_leases("A", ids, 3, 60)
    assert owned == 2  # disabled target's lease swept, not re-claimed


async def test_reconcile_orphan_safety_net_sweep(repo):
    """Dynamic mode has no 'claim anything unowned' fallback like the static path,
    so reconcile sweeps orphans (targets in nobody's HRW slice) up to cap — covering
    asymmetric-membership / cold-start / failing-peer gaps within one pass — but the
    sweep still never steals a peer's FRESH lease."""
    targets = await _make_targets(repo, 6)
    ids = [t.id for t in targets]
    await repo.reconcile_shard_leases("B", [ids[5]], 1, 60)  # B legitimately owns the last
    # A's HRW slice is only ids[:2]; ids[2:5] are unowned orphans. With spare
    # capacity (cap=6) A claims its slice AND adopts the 3 orphans, leaving B alone.
    owned = await repo.reconcile_shard_leases("A", ids[:2], 6, 60)
    assert owned == 5
    a_ids = {t.id for t in await repo.get_owned_targets("A", 60)}
    assert a_ids == set(ids[:5])
    assert ids[5] not in a_ids  # B's fresh lease untouched by the sweep
    assert len(await repo.get_owned_targets("B", 60)) == 1


# ---- stable-ordinal slot claim --------------------------------------------


async def test_slot_claim_assigns_lowest_free_and_renews(repo):
    assert await repo.claim_collector_slot("hostA", 60) == 0
    assert await repo.claim_collector_slot("hostB", 60) == 1
    assert await repo.claim_collector_slot("hostC", 60) == 2
    # Idempotent renew: same token keeps its ordinal.
    assert await repo.claim_collector_slot("hostA", 60) == 0
    # Release frees the ordinal; next new token takes the lowest free (1).
    await repo.release_collector_slot("hostB")
    assert await repo.claim_collector_slot("hostD", 60) == 1


async def test_slot_claim_reclaims_stale(repo):
    assert await repo.claim_collector_slot("hostA", 60) == 0
    await _age_slots(repo, "hostA", seconds=300)  # hostA "died"
    # A new process reaps the stale slot and takes ordinal 0.
    assert await repo.claim_collector_slot("hostB", 60) == 0


async def test_renew_slot_keeps_same_ordinal(repo):
    assert await repo.claim_collector_slot("hostA", 60) == 0
    # Renewal re-asserts the SAME ordinal (never drifts to a new one).
    assert await repo.renew_collector_slot("hostA", 0, 60) is True
    # Even if our slot was reaped while we stalled but nobody took the ordinal,
    # renewal simply re-inserts it (no drift, no identity loss).
    await _age_slots(repo, "hostA", seconds=300)
    assert await repo.renew_collector_slot("hostA", 0, 75) is True


async def test_renew_slot_false_when_ordinal_taken_by_peer(repo):
    assert await repo.claim_collector_slot("hostA", 60) == 0
    await _age_slots(repo, "hostA", seconds=300)  # hostA stalled past TTL
    assert await repo.claim_collector_slot("hostB", 60) == 0  # peer takes ordinal 0
    # hostA resuming must NOT reclaim 0 (hostB owns it now) — genuine identity loss.
    assert await repo.renew_collector_slot("hostA", 0, 75) is False


# ---- graceful release evicts membership -----------------------------------


async def test_release_shard_leases_evicts_collector_stats(repo):
    targets = await _make_targets(repo, 2)
    await repo.reconcile_shard_leases("A", [t.id for t in targets], 2, 60)
    await repo.upsert_collector_stats("A", {"alerts_received": 1}, owned_targets=2)
    await repo.release_shard_leases("A")
    # Both leases AND the membership/stats row are gone, so peers drop A from the
    # live set immediately on the next pass.
    assert len(await repo.get_owned_targets("A", 60)) == 0
    assert await repo.get_collector_stats(max_age_seconds=120) == []


async def test_get_active_targets_follows_dynamic_slice(repo):
    """The accessor EVERY collection consumer uses — poller, alert SSE/webhook
    subscriptions, diagnostic log collection, inventory enricher — returns exactly
    this collector's dynamically-reconciled slice, so they all shard together."""
    targets = await _make_targets(repo, 6)
    ids = [t.id for t in targets]
    repo.set_shard_context("collector-0", 75)
    await repo.reconcile_shard_leases("collector-0", ids[:4], 4, 75)
    assert {t.id for t in await repo.get_active_targets()} == set(ids[:4])
    # Lazy shed: dropping ids[2:4] keeps them active (fresh) until they age — so
    # SSE/webhook/log collection keep covering them through the handoff (gap-free).
    # cap=2 so A (still holding 4) has no spare capacity to re-sweep the shed two.
    await repo.reconcile_shard_leases("collector-0", ids[:2], 2, 75)
    assert {t.id for t in await repo.get_active_targets()} == set(ids[:4])
    await _age_leases(repo, "collector-0", 300)
    await repo.reconcile_shard_leases("collector-0", ids[:2], 2, 75)
    assert {t.id for t in await repo.get_active_targets()} == set(ids[:2])


# ---- ShardManager dynamic end-to-end --------------------------------------


async def test_shard_manager_dynamic_balances_across_three(repo):
    from src.config import ShardConfig
    from src.shard_manager import ShardManager

    await _make_targets(repo, 30)
    cfg = ShardConfig(enabled=True, balance="dynamic", max_targets_per_shard=200)
    ids = ["collector-0", "collector-1", "collector-2"]
    # Seed membership so each manager sees the full live set on its first pass.
    for cid in ids:
        await repo.upsert_collector_stats(cid, {}, 0)
    managers = [ShardManager(repo, cid, cfg) for cid in ids]
    for m in managers:
        await m.claim_once()
    owned = {m._id: m.owned_count for m in managers}
    # Balanced to within 1 (30/3 = 10 each) and full, disjoint coverage.
    assert max(owned.values()) - min(owned.values()) <= 1
    all_ids = set()
    for cid in ids:
        s = {t.id for t in await repo.get_owned_targets(cid, cfg.lease_ttl_seconds)}
        assert s.isdisjoint(all_ids)
        all_ids |= s
    assert len(all_ids) == 30
