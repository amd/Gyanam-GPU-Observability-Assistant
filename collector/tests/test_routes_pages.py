# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Smoke tests that the server-rendered HTML pages load for an authed user."""

import pytest


@pytest.mark.parametrize("path", ["/", "/targets", "/logs", "/schemas", "/alerts", "/status"])
async def test_pages_render(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


async def test_diagnostics_page_shows_json(client):
    r = await client.get("/status")
    assert r.status_code == 200
    # Health metrics are rendered as formatted JSON inside a <pre> block.
    assert "Diagnostics" in r.text
    assert "<pre" in r.text
    assert "collector_service" in r.text  # a key from the health JSON
