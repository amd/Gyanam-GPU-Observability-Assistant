# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Targeted coverage for the small remaining gaps:

* inventory/collector.py   — _get_json error guards, _first_member / _count_gpus
                             skip/None branches, system-level health fallback.
* parser/redfish_log_parser.py — file read error, URI/separator handling in the
                             JSON-block extractor, empty-block guards.
* api/routes/schemas.py    — the POST /schemas/api/reload endpoint.
* exporters/base.py        — BaseExporter abstract bodies + async context mgr.
"""

import json
from types import SimpleNamespace

from src.exporters.base import BaseExporter, Metric
from src.inventory import collect_inventory
from src.inventory import collector as invmod
from src.parser.redfish_log_parser import RedfishLogParser


# --------------------------------------------------------------------------- #
# inventory/collector.py
# --------------------------------------------------------------------------- #
def _resp(body):
    """Build a successful get_metric_report-shaped response for a JSON body."""
    return SimpleNamespace(success=True, content=json.dumps(body).encode())


class _MapClient:
    """Maps URIs to JSON bodies; unknown URIs return a failed response."""

    def __init__(self, responses: dict):
        self._responses = responses

    async def get_metric_report(self, uri: str):
        body = self._responses.get(uri)
        if body is None:
            return SimpleNamespace(success=False, content=b"")
        return _resp(body)


async def test_get_json_invalid_json_returns_none():
    """Malformed JSON body hits the (JSONDecodeError, ...) guard -> None."""

    class _Bad:
        async def get_metric_report(self, uri):
            return SimpleNamespace(success=True, content=b"{not valid json")

    assert await invmod._get_json(_Bad(), "/redfish/v1/Chassis") is None


async def test_get_json_generic_exception_returns_none():
    """A non-decoding error (RuntimeError) hits the broad best-effort guard."""

    class _Raiser:
        async def get_metric_report(self, uri):
            raise RuntimeError("transport boom")

    assert await invmod._get_json(_Raiser(), "/redfish/v1/Chassis") is None


async def test_first_member_skips_memberless_and_unresolvable():
    """A member with no @odata.id is skipped; when none resolve, returns None."""
    client = _MapClient(
        {
            "/coll": {"Members": [{"nope": 1}, {"@odata.id": "/m1"}]},
            # "/m1" is absent -> failed response -> resource None.
        }
    )
    assert await invmod._first_member(client, "/coll") is None


async def test_count_gpus_missing_collection_returns_none_triple():
    """processors_uri that doesn't resolve -> (None, None, None)."""
    assert await invmod._count_gpus(_MapClient({}), "/missing") == (None, None, None)


async def test_count_gpus_skips_memberless_and_unresolvable_members():
    """Member w/o @odata.id is skipped; a member whose GET fails is skipped."""
    client = _MapClient(
        {
            "/procs": {"Members": [{"nope": 1}, {"@odata.id": "/p1"}]},
            # "/p1" absent -> proc None -> skipped. No GPU counted.
        }
    )
    count, model, memory = await invmod._count_gpus(client, "/procs")
    assert count is None and model is None and memory is None


async def test_system_health_fallback_when_chassis_has_no_status():
    """inv.health falls back to the system's Status.Health when chassis omits it."""
    responses = {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {"Model": "node-x"},  # no Status
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
        "/redfish/v1/Systems/1": {"Status": {"Health": "Warning"}},
    }
    inv = await collect_inventory(_MapClient(responses))
    assert inv is not None and inv.health == "Warning"


# --------------------------------------------------------------------------- #
# parser/redfish_log_parser.py
# --------------------------------------------------------------------------- #
def test_parse_file_missing_returns_none(tmp_path):
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_file(tmp_path / "nope.log") is None


def test_parse_file_unicode_error_returns_none(tmp_path):
    """A file with invalid UTF-8 bytes hits the UnicodeDecodeError guard."""
    bad = tmp_path / "redfish-tree.log"
    bad.write_bytes(b"\xff\xfe\x00\x01not utf-8\xff")
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_file(bad) is None


def test_parse_file_reads_and_parses(tmp_path):
    """Happy path through parse_file -> parse_content."""
    good = tmp_path / "redfish-tree.log"
    good.write_text('GET redfish/v1/X\n{"ok": true}\n', encoding="utf-8")
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_file(good) == {"ok": True}


def test_parse_content_target_on_last_line_returns_none():
    """Target URL is the final line -> no following block (start_idx out of range)."""
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_content("GET redfish/v1/X") is None


def test_parse_content_skips_separators_and_uri_lines():
    """Separator '=' lines and a leading 'URI:' line are skipped; a trailing
    'URI:' line after JSON terminates the block."""
    content = (
        "GET redfish/v1/X\n"
        "======\n"  # separator before JSON -> continue
        "URI: /something\n"  # URI line before brace -> continue
        '{"a": 1}\n'  # JSON block
        "URI: /next\n"  # URI line after brace -> break
        '{"b": 2}\n'
    )
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_content(content) == {"a": 1}


def test_parse_content_no_json_block_returns_none():
    """Only separator/URI lines follow the target -> empty block -> None."""
    content = "GET redfish/v1/X\n======\nURI: /foo\n"
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_content(content) is None


# --------------------------------------------------------------------------- #
# api/routes/schemas.py — POST /schemas/api/reload
# --------------------------------------------------------------------------- #
async def test_reload_schemas_endpoint(client):
    r = await client.post("/schemas/api/reload")
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "Schemas reloaded successfully"
    assert body["count"] >= 1


# --------------------------------------------------------------------------- #
# exporters/base.py — abstract bodies + async context manager
# --------------------------------------------------------------------------- #
class _ConcreteExporter(BaseExporter):
    """Minimal concrete exporter that defers to the abstract bodies via super(),
    so the base class's (otherwise unexecuted) method bodies are covered."""

    def __init__(self):
        self.events: list[str] = []

    async def connect(self) -> None:
        await super().connect()  # runs the abstract 'pass' body
        self.events.append("connect")

    async def close(self) -> None:
        await super().close()
        self.events.append("close")

    async def write(self, metrics):
        return await super().write(metrics)

    async def health_check(self):
        base = await super().health_check()
        return base or (True, "ok")

    @property
    def is_connected(self) -> bool:
        _ = super().is_connected  # exercises the abstract property getter
        return True


async def test_base_exporter_context_manager_runs_connect_close():
    exp = _ConcreteExporter()
    async with exp as e:
        assert e is exp
    assert exp.events == ["connect", "close"]


async def test_base_exporter_abstract_bodies():
    exp = _ConcreteExporter()
    assert await exp.write([]) is None
    assert await exp.health_check() == (True, "ok")
    assert exp.is_connected is True


def test_metric_to_dict_roundtrip():
    from datetime import UTC, datetime

    m = Metric(
        name="gpu_temp",
        value=42.5,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        tags={"host": "h1"},
        metric_type="gauge",
        unit="C",
    )
    d = m.to_dict()
    assert d["name"] == "gpu_temp" and d["value"] == 42.5
    assert d["tags"] == {"host": "h1"} and d["type"] == "gauge" and d["unit"] == "C"
