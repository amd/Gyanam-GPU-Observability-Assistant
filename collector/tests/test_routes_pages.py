# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Smoke tests that the server-rendered HTML pages load for an authed user."""

import pytest


@pytest.mark.parametrize(
    "path", ["/systems", "/targets", "/logs", "/schemas", "/alerts", "/status"]
)
async def test_pages_render(client, path):
    # /targets is kept as a backward-compatible alias of the canonical /systems.
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


async def test_root_redirects_to_systems(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/systems"


async def test_systems_page_shows_totals(client, repo):
    # The home (Systems) page always shows the system + GPU totals.
    await repo.create_target(name="n1", host="h1", username="u", password="p")
    r = await client.get("/systems")
    assert r.status_code == 200
    assert "Systems Under Monitoring" in r.text
    assert "GPUs" in r.text  # total GPU count rendered next to the heading


async def test_diagnostics_page_shows_json(client):
    r = await client.get("/status")
    assert r.status_code == 200
    # Health metrics are rendered as formatted JSON inside a <pre> block.
    assert "Diagnostics" in r.text
    assert "<pre" in r.text
    assert "collector_service" in r.text  # a key from the health JSON
