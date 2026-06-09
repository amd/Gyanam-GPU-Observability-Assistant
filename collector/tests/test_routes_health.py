# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Route tests for health endpoints."""


async def test_health_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200


async def test_ready(client):
    r = await client.get("/ready")
    assert r.status_code in (200, 503)  # ready or not-ready, but a valid response
