# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for RedfishPoller pure helpers (interval / circuit breaker / stats)."""

from types import SimpleNamespace

from src.redfish.poller import RedfishPoller


def _poller():
    return RedfishPoller(repository=None, poll_interval=300)


def _target(**kw):
    kw.setdefault("poll_interval_override", None)
    kw.setdefault("consecutive_failures", 0)
    return SimpleNamespace(**kw)


def test_interval_default():
    p = _poller()
    assert p._get_target_interval(_target(), poll_succeeded=True) == 300


def test_interval_override():
    p = _poller()
    assert p._get_target_interval(_target(poll_interval_override=60), poll_succeeded=True) == 60


def test_interval_circuit_breaker_backoff():
    p = _poller()
    # Reaching the failure threshold backs the target off by the recheck multiplier.
    t = _target(consecutive_failures=p.circuit_breaker_threshold - 1)
    backed_off = p._get_target_interval(t, poll_succeeded=False)
    assert backed_off == 300 * p.circuit_breaker_recheck_multiplier


def test_interval_resets_on_success():
    p = _poller()
    t = _target(consecutive_failures=10)
    # A successful poll resets the effective failure count -> normal interval.
    assert p._get_target_interval(t, poll_succeeded=True) == 300


def test_get_stats_shape():
    stats = _poller().get_stats()
    for key in (
        "polls_started",
        "inflight",
        "cached_clients",
        "result_queue_size",
        "seconds_since_last_poll",
        "making_progress",
    ):
        assert key in stats


def test_progress_startup_before_first_poll():
    p = _poller()
    p._running = True
    # No poll completed yet -> treated as progressing (startup), not stalled.
    assert p._last_completed_monotonic is None
    assert p.is_making_progress() is True
    assert p.get_stats()["seconds_since_last_poll"] is None


def test_progress_false_when_not_running():
    p = _poller()
    assert p._running is False
    assert p.is_making_progress() is False


def test_progress_false_when_stalled():
    import time

    p = _poller()  # poll_interval=300 -> stall window = max(600, 900) = 900s
    p._running = True
    # Last completion was long ago on the monotonic clock -> stalled.
    p._last_completed_monotonic = time.monotonic() - 10_000
    assert p.is_making_progress() is False


def test_progress_true_when_recent():
    import time

    p = _poller()
    p._running = True
    p._last_completed_monotonic = time.monotonic() - 1
    assert p.is_making_progress() is True
