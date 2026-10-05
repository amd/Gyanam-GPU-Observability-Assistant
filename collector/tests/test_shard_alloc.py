# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the pure Rendezvous (HRW) target allocator (src/shard_alloc.py)."""

import random

from src.shard_alloc import allocate, effective_cap, score

COLLECTORS_3 = ["collector-0", "collector-1", "collector-2"]


def _alloc_balanced(target_ids, live, max_per_shard=200):
    cap = effective_cap(len(target_ids), len(live), max_per_shard)
    return allocate(target_ids, live, cap), cap


# ---- balance & coverage ----------------------------------------------------


def test_balanced_within_one_and_full_coverage():
    ids = list(range(1, 303))  # 302 targets
    assigned, cap = _alloc_balanced(ids, COLLECTORS_3)
    assert cap == 101  # ceil(302/3)
    counts = sorted(len(v) for v in assigned.values())
    # Fair-share cap forces ±1 balance: [100, 101, 101].
    assert max(counts) - min(counts) <= 1
    assert max(counts) <= cap
    # Every target assigned exactly once (disjoint, full coverage).
    all_assigned = [t for v in assigned.values() for t in v]
    assert sorted(all_assigned) == ids
    assert len(all_assigned) == len(set(all_assigned)) == 302


def test_disjoint_across_collectors():
    ids = list(range(1, 251))
    assigned, _ = _alloc_balanced(ids, COLLECTORS_3)
    seen = set()
    for v in assigned.values():
        s = set(v)
        assert s.isdisjoint(seen)  # no target in two collectors
        seen |= s
    assert seen == set(ids)


# ---- determinism -----------------------------------------------------------


def test_deterministic_under_shuffled_inputs():
    ids = list(range(1, 200))
    live = list(COLLECTORS_3)
    base, cap = _alloc_balanced(ids, live)
    for _ in range(5):
        shuffled_ids = ids[:]
        random.shuffle(shuffled_ids)
        shuffled_live = live[:]
        random.shuffle(shuffled_live)
        other = allocate(shuffled_ids, shuffled_live, cap)
        # Identical assignment regardless of input ordering -> nodes agree.
        assert {k: sorted(v) for k, v in other.items()} == {k: sorted(v) for k, v in base.items()}


def test_score_is_stable_and_integer():
    # Same inputs -> same score across calls (sha256, not salted hash()).
    assert score(42, "collector-1") == score(42, "collector-1")
    assert isinstance(score(42, "collector-1"), int)
    # Different id or collector -> (almost surely) different score.
    assert score(42, "collector-1") != score(42, "collector-2")
    assert score(42, "collector-1") != score(43, "collector-1")


# ---- minimal movement on membership change ---------------------------------


def _owner_map(target_ids, live, max_per_shard=200):
    cap = effective_cap(len(target_ids), len(live), max_per_shard)
    assigned = allocate(target_ids, live, cap)
    return {t: c for c, ts in assigned.items() for t in ts}


def test_minimal_movement_on_scale_up():
    ids = list(range(1, 301))  # 300 targets
    before = _owner_map(ids, COLLECTORS_3)
    after = _owner_map(ids, COLLECTORS_3 + ["collector-3"])
    moved = sum(1 for t in ids if before[t] != after[t])
    # Adding a 4th collector should move ~N/4 targets, never reshuffle the fleet.
    assert 0 < moved < 0.5 * len(ids)


def test_minimal_movement_on_scale_down():
    ids = list(range(1, 301))
    before = _owner_map(ids, COLLECTORS_3)
    after = _owner_map(ids, ["collector-0", "collector-1"])  # lose collector-2
    moved = sum(1 for t in ids if before[t] != after[t])
    # The departed collector's ~N/3 targets must move; the fair-share cap tightens
    # from 100 to 150, which reshuffles some spill targets too — but it's never a
    # full reshuffle (the majority keep their owner). Pure-HRW minimal movement is
    # relaxed slightly as the price of ±1 balance.
    assert 0 < moved < 0.5 * len(ids)
    # Every target that stayed is on a surviving collector (no dangling owner).
    assert all(after[t] in ("collector-0", "collector-1") for t in ids)


# ---- cap / under-provisioning ----------------------------------------------


def test_effective_cap_is_fair_share_clamped_by_max():
    assert effective_cap(302, 3, 200) == 101  # fair share wins
    assert effective_cap(900, 3, 200) == 200  # safety cap wins
    assert effective_cap(10, 1, 200) == 10  # single collector
    assert effective_cap(0, 3, 200) == 0


def test_under_provisioned_leaves_surplus_unassigned():
    ids = list(range(1, 501))  # 500 targets
    live = ["collector-0", "collector-1"]
    cap = effective_cap(len(ids), len(live), 200)  # min(200, 250) = 200
    assert cap == 200
    assigned = allocate(ids, live, cap)
    total = sum(len(v) for v in assigned.values())
    assert total == 400  # 2 x 200 capacity
    assert all(len(v) <= 200 for v in assigned.values())
    # 100 targets cannot be placed -> surfaced to the caller as a coverage gap.
    assert len(ids) - total == 100


# ---- degenerate cases ------------------------------------------------------


def test_single_collector_takes_all_up_to_cap():
    ids = list(range(1, 11))
    assigned = allocate(ids, ["only"], effective_cap(len(ids), 1, 200))
    assert sorted(assigned["only"]) == ids


def test_empty_targets_and_empty_live():
    assert allocate([], COLLECTORS_3, 100) == {c: [] for c in COLLECTORS_3}
    assert allocate([1, 2, 3], [], 100) == {}
    assert allocate([1, 2, 3], COLLECTORS_3, 0) == {c: [] for c in COLLECTORS_3}


def test_duplicate_ids_and_collectors_are_deduped():
    assigned = allocate([1, 1, 2, 2, 3], ["a", "a", "b"], cap=10)
    all_assigned = sorted(t for v in assigned.values() for t in v)
    assert all_assigned == [1, 2, 3]  # each unique id placed once
    assert set(assigned.keys()) == {"a", "b"}
