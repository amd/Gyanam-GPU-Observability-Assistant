# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Factory for per-target BMC httpx clients with a bounded connection pool.

One cached client can exist per target (hundreds at fleet scale). With httpx's
default pool (up to 100 connections each) the aggregate socket count can exhaust
the process file-descriptor limit. A BMC needs only a handful of parallel GETs,
so every BMC client's pool is capped here — the single place that bound lives.
"""

import httpx

# Fixed pool cap applied to every BMC client (see module docstring).
_BMC_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=4)

# Sentinel so `read_timeout=None` (an intentional unbounded read, e.g. for an
# SSE stream) is distinguishable from "caller did not specify a read timeout".
_UNSET: object = object()


def make_bmc_client(
    *,
    verify_ssl: bool,
    timeout: float,
    follow_redirects: bool = True,
    read_timeout: float | None = _UNSET,  # type: ignore[assignment]
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` for talking to a BMC, with a bounded pool.

    ``timeout`` sets the connect/write/pool phases (and the read phase too,
    unless ``read_timeout`` is given). Pass ``read_timeout=None`` to leave the
    read phase unbounded — used for long-lived SSE streams that must not time
    out between events. The connection-pool limits are fixed (max 10
    connections / 4 keepalive) to cap the per-target FD footprint at scale.
    """
    read = timeout if read_timeout is _UNSET else read_timeout
    client_timeout = httpx.Timeout(timeout, read=read)
    kwargs: dict = {
        "verify": verify_ssl,
        "timeout": client_timeout,
        "follow_redirects": follow_redirects,
        "limits": _BMC_LIMITS,
    }
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)
