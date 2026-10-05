# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for collector_main._sync_extract_metrics (the GET fast-path + guards)."""

import asyncio
import gzip
import json
from datetime import UTC, datetime

import pytest
from src.collector_main import _sync_extract_metrics, process_poll_result, result_processor_task
from src.parser.discovery import MetricDiscovery
from src.parser.extractor import MetricExtractor
from src.parser.unpacker import BlobUnpacker
from src.redfish.poller import PollResult


@pytest.fixture
def parts(schema_loader, tmp_path):
    return (
        BlobUnpacker(temp_dir=str(tmp_path / "x")),
        MetricExtractor(schema_loader),
        MetricDiscovery(schema_loader),
    )


def _result(**kw):
    base = {
        "target_id": 1,
        "target_name": "sys1",
        "target_host": "h1",
        "success": True,
        "content": b"",
        "content_type": "application/json",
        "error_message": None,
        "poll_time": datetime.now(UTC),
        "duration_ms": 1.0,
    }
    base.update(kw)
    return PollResult(**base)


def test_fast_path_extracts_and_discovers(parts, sample_metric_report):
    unpacker, extractor, discovery = parts
    result = _result(
        data=[("comprehensive", sample_metric_report)],
        target_tags={"site": "lab"},
    )
    metrics, cleanup = _sync_extract_metrics(result, unpacker, extractor, discovery)
    assert cleanup == []  # GET fast-path has no files to clean
    assert isinstance(metrics, list) and len(metrics) >= 1
    # target_name tag is threaded onto every metric.
    assert all(m.tags.get("target_name") == "sys1" for m in metrics)
    assert any(m.tags.get("site") == "lab" for m in metrics)


def test_fast_path_dedupes_repeated_property(parts):
    unpacker, extractor, discovery = parts
    mv = {"MetricProperty": "/redfish/v1/Chassis/1#DUP", "MetricValue": "5"}
    result = _result(
        data=[
            ("r1", {"MetricValues": [mv]}),
            ("r2", {"MetricValues": [mv, {"MetricProperty": "/x#OTHER", "MetricValue": "9"}]}),
        ]
    )
    metrics, _ = _sync_extract_metrics(result, unpacker, extractor, discovery)
    # DUP is claimed once (r1); r2's DUP is deduped. Both reports processed w/o error.
    assert isinstance(metrics, list)


def test_fast_path_skips_malformed_report_keeps_good_one(parts, sample_metric_report):
    """C1 guard: one non-dict / garbage report must not drop the whole target's
    cycle. The good report's metrics still come through even when malformed reports
    precede it (previously the first bad report raised and dropped everything while
    the poll was still recorded a success)."""
    unpacker, extractor, discovery = parts
    result = _result(
        data=[
            ("bad_list", []),  # not a dict -> would AttributeError
            ("bad_mv_null", {"MetricValues": None}),  # null -> would TypeError
            ("bad_entries", {"MetricValues": ["oops", 42, None]}),  # non-dict entries
            ("good", sample_metric_report),  # must still extract
        ]
    )
    metrics, _ = _sync_extract_metrics(result, unpacker, extractor, discovery)
    assert isinstance(metrics, list) and len(metrics) >= 1
    assert all(m.tags.get("target_name") == "sys1" for m in metrics)


def test_empty_content_slow_path_returns_nothing(parts):
    unpacker, extractor, discovery = parts
    result = _result(data=None, content=b"")  # no GET data, no blob
    assert _sync_extract_metrics(result, unpacker, extractor, discovery) == ([], [])


# ---- process_poll_result ----


class _FakeExporter:
    def __init__(self):
        self.written = []

    async def write(self, metrics):
        self.written.extend(metrics)

    @property
    def is_connected(self):
        return True


async def test_process_poll_result_extracts_and_exports(parts, sample_metric_report):
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    result = _result(data=[("comprehensive", sample_metric_report)])
    n = await process_poll_result(result, unpacker, extractor, discovery, exporter)
    assert n >= 1 and len(exporter.written) == n


async def test_process_poll_result_skips_failed_result():
    exporter = _FakeExporter()
    # Early-return before touching the (None) extractor/unpacker/discovery.
    result = _result(success=False, data=None, content=b"")
    assert await process_poll_result(result, None, None, None, exporter) == 0
    assert exporter.written == []


async def test_process_poll_result_no_metrics(parts):
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    result = _result(data=[])  # empty -> nothing extracted
    assert await process_poll_result(result, unpacker, extractor, discovery, exporter) == 0


async def test_process_poll_result_slow_path_blob(parts):
    # No GET data -> the slow (blob unpack) path runs: gzip -> JSON -> extract.
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    blob = gzip.compress(
        json.dumps(
            {
                "Id": "All",
                "MetricValues": [
                    {"MetricProperty": "/redfish/v1/Chassis/1#GPU_TEMP", "MetricValue": "42.5"}
                ],
            }
        ).encode()
    )
    result = _result(data=None, content=blob, content_type="application/gzip")
    # The slow (unpack) path now tags each file with a derived report_type and
    # extracts/discovers metrics (previously it raised AttributeError and
    # silently dropped them).
    n = await process_poll_result(result, unpacker, extractor, discovery, exporter)
    assert n >= 1 and len(exporter.written) == n


async def test_result_processor_task_processes_then_cancels(parts, sample_metric_report):
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    state = {"n": 0}

    class _FakePoller:
        async def get_results(self, timeout):
            state["n"] += 1
            if state["n"] == 1:
                return [_result(data=[("comprehensive", sample_metric_report)])]
            raise asyncio.CancelledError()  # second call stops the loop

    await result_processor_task(
        _FakePoller(), unpacker, extractor, discovery, exporter, max_workers=2
    )
    assert len(exporter.written) >= 1
