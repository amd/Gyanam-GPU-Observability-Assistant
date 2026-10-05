# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Rendezvous (HRW) target allocation for dynamic sharding.

Pure, deterministic, DB-free. Every collector runs the identical ``allocate``
over the same ``(enabled target ids, live collector ids)`` and extracts its own
disjoint slice — so with an agreed membership view the fleet partitions with zero
overlap and no coordination. The repository layer then reconciles the shared lease
table to match each collector's computed slice.

Why HRW rather than greedy id-order claiming: a target's owner is a stable
function of its id and the live collector set, so adding/removing a collector
moves only ~1/K of the targets (not the whole fleet), and the load lands evenly
bounded by the fair share (see ``effective_cap``) — within ±1 when N ≫ K (the
deployment regime: hundreds of targets over a few collectors). For small N/K the
spread is wider and a collector can even get 0; the fair-share cap only bounds the
maximum, not the minimum.
"""

from __future__ import annotations

import hashlib
import math

# Number of hash bytes folded into the HRW score. 8 bytes (64 bits) makes a tie
# astronomically unlikely; ties are still broken deterministically by id.
_SCORE_BYTES = 8

# Field separator for the hash basis. An arbitrary collector id could otherwise
# collide with a target id across the boundary (e.g. tid=1, cid="23" vs tid=12,
# cid="3"); \x1f (ASCII unit separator) cannot appear in our ids. Mirrors the
# idempotency-key style in database/repository.py.
_SEP = "\x1f"


def score(target_id: int, collector_id: str) -> int:
    """Stable cross-process HRW weight for a (target, collector) pair.

    Uses sha256, NOT Python's builtin ``hash()`` — the latter is salted per
    process (``PYTHONHASHSEED``), so different collectors would compute different
    weights and never agree on an assignment.
    """
    basis = f"{target_id}{_SEP}{collector_id}".encode()
    return int.from_bytes(hashlib.sha256(basis).digest()[:_SCORE_BYTES], "big")


def effective_cap(n_targets: int, n_live: int, max_per_shard: int) -> int:
    """Per-collector target bound for one allocation pass.

    The *fair share* ``ceil(n_targets / n_live)`` is what produces ±1 balance;
    ``max_per_shard`` is only a hard safety ceiling (per-collector resource
    guard). Vanilla HRW capped at ``max_per_shard`` alone would leave the fair
    share far below the cap and so distribute unevenly.
    """
    if n_live <= 0:
        return max_per_shard
    return min(max_per_shard, math.ceil(n_targets / n_live))


def allocate(target_ids: list[int], live: list[str], cap: int) -> dict[str, list[int]]:
    """Assign each target to a live collector by HRW, bounded by ``cap``.

    Returns ``{collector_id: [target_id, ...]}`` for every collector in ``live``.
    A target is placed on its highest-scoring collector that is still under
    ``cap``; if every preferred collector is full it spills to the next. If no
    collector has room (``cap * len(live) < len(target_ids)``, i.e. the fleet is
    under-provisioned) the surplus targets are left unassigned — the caller
    surfaces that as a coverage warning.

    Deterministic: identical inputs yield identical output on every node,
    regardless of input ordering, so collectors agree without coordinating.
    """
    live = sorted(set(live))
    assigned: dict[str, list[int]] = {c: [] for c in live}
    if not live or cap <= 0:
        return assigned
    for tid in sorted(set(target_ids)):
        # Highest score first; collector_id breaks the (vanishingly rare) tie so
        # the ordering is total and identical everywhere.
        for cid in sorted(live, key=lambda c: (-score(tid, c), c)):
            if len(assigned[cid]) < cap:
                assigned[cid].append(tid)
                break
        # else: no room anywhere -> tid unassigned (under-provisioned fleet).
    return assigned
