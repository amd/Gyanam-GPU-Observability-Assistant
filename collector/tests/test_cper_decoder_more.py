# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional coverage for CPER fetch and decode error/edge paths."""

import asyncio
import base64

import httpx
import pytest
from src.redfish import cper_decoder


async def test_decode_cper_kills_subprocess_on_cancel(monkeypatch):
    # If the worker is cancelled mid-decode, the child must be killed (not
    # orphaned) before CancelledError propagates.
    killed = {"v": False}

    class _Proc:
        returncode = None

        async def communicate(self):
            raise asyncio.CancelledError()

        def kill(self):
            killed["v"] = True

        async def wait(self):
            return 0

    async def _fake_exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(cper_decoder.asyncio, "create_subprocess_exec", _fake_exec)
    with pytest.raises(asyncio.CancelledError):
        await cper_decoder.decode_cper(b"data", cper_convert_path="x", timeout=5)
    assert killed["v"] is True


# ---- fetch_cper_attachment ---------------------------------------------


async def test_fetch_returns_bytes_absolute_uri(httpx_mock):
    # A non-"/" uri is treated as an absolute URL (base_url not prepended).
    httpx_mock.add_response(
        method="GET", url="https://host/full/attach", status_code=200, content=b"DATA"
    )
    data = await cper_decoder.fetch_cper_attachment(
        base_url="https://bmc",
        uri="https://host/full/attach",
        username="u",
        password="p",
        verify_ssl=False,
    )
    assert data == b"DATA"


async def test_fetch_410_raises_gone(httpx_mock):
    httpx_mock.add_response(method="GET", url="https://bmc/x", status_code=410)
    with pytest.raises(cper_decoder.CperGoneError):
        await cper_decoder.fetch_cper_attachment(
            base_url="https://bmc", uri="/x", username="u", password="p", verify_ssl=False
        )


async def test_fetch_500_logs_body_and_raises(httpx_mock):
    # >=400 (non-gone): the body is logged for diagnosis, then raise_for_status
    # surfaces an httpx error the caller can retry on.
    httpx_mock.add_response(
        method="GET", url="https://bmc/x", status_code=500, text="internal boom"
    )
    with pytest.raises(httpx.HTTPStatusError):
        await cper_decoder.fetch_cper_attachment(
            base_url="https://bmc", uri="/x", username="u", password="p", verify_ssl=False
        )


# ---- decode_cper (subprocess monkeypatched) -----------------------------


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_exec(monkeypatch, proc=None, exc=None):
    async def fake_exec(*args, **kwargs):
        if exc is not None:
            raise exc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


async def test_decode_empty_data_returns_none():
    assert await cper_decoder.decode_cper(b"") is None


async def test_decode_tool_missing_returns_none(monkeypatch):
    _patch_exec(monkeypatch, exc=FileNotFoundError())
    assert await cper_decoder.decode_cper(b"\x00\x01") is None


async def test_decode_nonzero_exit_returns_none(monkeypatch):
    _patch_exec(monkeypatch, proc=_FakeProc(returncode=3, stderr=b"bad record"))
    assert await cper_decoder.decode_cper(b"\x00\x01") is None


async def test_decode_bad_json_returns_none(monkeypatch):
    _patch_exec(monkeypatch, proc=_FakeProc(returncode=0, stdout=b"{not json"))
    assert await cper_decoder.decode_cper(b"\x00\x01") is None


async def test_decode_timeout_returns_none(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc=proc)
    assert await cper_decoder.decode_cper(b"\x00\x01", timeout=0.05) is None
    assert proc.killed is True


async def test_decode_success_returns_dict(monkeypatch):
    _patch_exec(
        monkeypatch,
        proc=_FakeProc(returncode=0, stdout=b'{"sections": [], "header": {}}'),
    )
    out = await cper_decoder.decode_cper(b"\x00\x01")
    assert out == {"sections": [], "header": {}}


# ---- _ascii_hints -------------------------------------------------------


def test_ascii_hints_respects_limit():
    blob = b"\x00AAAA\x00BBBB\x00CCCC\x00DDDD"
    b64 = base64.b64encode(blob).decode()
    hints = cper_decoder._ascii_hints(b64, limit=2)
    assert hints == ["AAAA", "BBBB"]


def test_ascii_hints_dedupes_runs():
    blob = b"\x00REPEAT\x00REPEAT\x00OTHER1"
    b64 = base64.b64encode(blob).decode()
    hints = cper_decoder._ascii_hints(b64)
    assert hints == ["REPEAT", "OTHER1"]


def test_ascii_hints_empty_input_returns_empty():
    assert cper_decoder._ascii_hints(None) == []
    assert cper_decoder._ascii_hints("") == []
