# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Fetch and decode CPER (Common Platform Error Record) attachments.

Redfish LogEntries for hardware faults can reference a CPER binary via
``AdditionalDataURI`` (UEFI Spec Appendix N). We download that blob from the BMC
and decode it with OpenBMC ``libcper`` (the ``cper-convert`` CLI bundled in the
image), then distill a human-readable summary for the alert UI.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import tempfile

import httpx

from . import amd_cper_sections as amd_cper
from .http_client import make_bmc_client

logger = logging.getLogger(__name__)

DEFAULT_CPER_CONVERT = "/usr/local/bin/cper-convert"
# Cap on the refined_message length so a pathological CPER can't bloat the row.
_MAX_SUMMARY_CHARS = 2000


class CperGoneError(Exception):
    """Attachment no longer available on the BMC (404/410) — terminal."""


async def fetch_cper_attachment(
    *,
    base_url: str,
    uri: str,
    username: str,
    password: str,
    verify_ssl: bool,
    timeout: float = 30.0,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    """Download a CPER attachment from the BMC.

    Raises ``CperGoneError`` if the attachment is absent (404/410), ``ValueError`` if
    it exceeds ``max_bytes``, or the underlying httpx error for transient
    failures (so the caller can retry).
    """
    url = f"{base_url}{uri}" if uri.startswith("/") else uri
    auth = httpx.BasicAuth(username, password)
    async with make_bmc_client(auth=auth, verify_ssl=verify_ssl, timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "application/octet-stream, */*"})
        if resp.status_code in (404, 410):
            raise CperGoneError(f"attachment {uri} returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # Surface the BMC's error body (truncated) — a bare "HTTP 400" gives
            # operators nothing to act on. Some BMCs reject the attachment GET
            # (wrong Accept, needs a session token, redirect dropped auth, etc.);
            # logging the reason is the only way to diagnose per-fleet quirks.
            body_preview = ""
            with contextlib.suppress(Exception):
                body_preview = resp.text[:300]
            logger.warning(
                "CPER attachment %s returned HTTP %s: %s",
                uri,
                resp.status_code,
                body_preview,
            )
        resp.raise_for_status()
        data = resp.content
        if len(data) > max_bytes:
            raise ValueError(f"CPER attachment {len(data)} bytes exceeds cap {max_bytes}")
        return data  # type: ignore[no-any-return]


async def decode_cper(
    data: bytes,
    *,
    cper_convert_path: str = DEFAULT_CPER_CONVERT,
    timeout: float = 15.0,
) -> dict | None:
    """Decode a CPER binary to JSON via ``cper-convert to-json``.

    Returns the decoded dict, or None if the tool is missing, times out, exits
    non-zero, or emits unparseable output.
    """
    if not data:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".cper", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
    except OSError as e:
        logger.warning("CPER decode: could not write temp file: %s", e)
        return None

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                cper_convert_path,
                "to-json",
                tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("CPER decoder not found at %s; is libcper bundled?", cper_convert_path)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("CPER decode timed out after %.0fs", timeout)
            return None
        except BaseException:
            # Worker cancelled (or any error) mid-decode: kill the child so it is
            # not orphaned when its input temp file is unlinked below, then re-raise.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise

        if proc.returncode != 0:
            logger.warning(
                "cper-convert exited %s: %s",
                proc.returncode,
                stderr.decode("utf-8", "replace")[:200],
            )
            return None
        try:
            return json.loads(stdout)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("cper-convert produced invalid JSON: %s", e)
            return None
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


_ASCII_RUN = re.compile(rb"[\x20-\x7e]{4,}")


def _ascii_hints(b64: str | None, *, limit: int = 3) -> list[str]:
    """Extract a few printable ASCII strings from a base64 OEM payload.

    libcper leaves vendor/OEM section bodies as base64. Firmware/version strings
    embedded in them (e.g. "01.10.180862") are a useful breadcrumb, so surface a
    handful of printable runs.
    """
    if not b64:
        return []
    try:
        blob = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        return []
    seen: list[str] = []
    for m in _ASCII_RUN.findall(blob):
        s = m.decode("ascii", "replace").strip()
        if s and s not in seen:
            seen.append(s)
        if len(seen) >= limit:
            break
    return seen


def enrich_amd_sections(decoded: dict | None) -> dict | None:
    """Attach AMD vendor-section decode to libcper's ``Unknown`` sections.

    libcper leaves AMD OEM sections as an opaque base64 blob. For each section
    whose descriptor GUID is an AMD vendor GUID, decode the payload with
    ``amd_cper_sections`` and store the result under ``section["amd"]`` so it is
    available both to the summary and the detail view. Mutates and returns
    ``decoded``.
    """
    if not isinstance(decoded, dict):
        return decoded
    descriptors = decoded.get("sectionDescriptors") or []
    sections = decoded.get("sections") or []

    # Interpret ASCII-encoded identifier GUIDs in the libcper header/descriptors
    # (platformID/creatorID = board serial/creator; fruID = FRU part number), and
    # flag an unset/implausible record timestamp.
    header = decoded.get("header")
    if isinstance(header, dict):
        for key in ("platformID", "creatorID"):
            txt = amd_cper.guid_as_ascii(header.get(key))
            if txt:
                header[f"{key}_ascii"] = txt
        ts = str(header.get("timestamp") or "")
        if ts[:2] in ("15", "16", "17", "18", "19") or ts.startswith("0"):
            header["timestamp_note"] = "unset/implausible (BMC did not populate a valid time)"
    for desc in descriptors:
        if isinstance(desc, dict):
            txt = amd_cper.guid_as_ascii(desc.get("fruID"))
            if txt:
                desc["fruID_ascii"] = txt
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            continue
        desc = descriptors[i] if i < len(descriptors) else {}
        guid = ((desc.get("sectionType") or {}).get("data") or "").lower()
        if not amd_cper.is_amd_section(guid):
            continue
        # Find the raw section blob libcper emitted, skipping our own "amd" key
        # (present on re-enrichment) and "message" — otherwise a re-run would pick
        # the already-decoded "amd" dict (no raw "data") and skip re-decoding.
        payload = next((sec[k] for k in sec if k not in ("message", "amd")), None)
        b64 = payload.get("data") if isinstance(payload, dict) else None
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64, validate=False)
        except (ValueError, TypeError):
            continue
        amd_decoded = amd_cper.decode_amd_section(guid, raw)
        if amd_decoded:
            sec["amd"] = amd_decoded
    return decoded


def summarize_cper(decoded: dict | None) -> str:
    """Distill a decoded CPER (libcper JSON) into a one-line human summary.

    libcper emits a per-section ``message`` (e.g. "A Scrub Corrected Error Memory
    Error occurred at address 0x... at node 51722"); we combine the record
    severity, notification type, and each section's type/severity/message. OEM or
    vendor sections without a message degrade to their section-type name/GUID.
    """
    if not isinstance(decoded, dict):
        return ""
    header = decoded.get("header") or {}
    record_sev = (header.get("severity") or {}).get("name") or "Unknown"
    notif = (header.get("notificationType") or {}).get("type") or ""

    descriptors = decoded.get("sectionDescriptors") or []
    sections = decoded.get("sections") or []

    parts: list[str] = []
    for i, sec in enumerate(sections):
        desc = descriptors[i] if i < len(descriptors) else {}
        stype_obj = desc.get("sectionType") or {}
        stype = stype_obj.get("type") or "Unknown section"
        guid = stype_obj.get("data")
        ssev = (desc.get("severity") or {}).get("name") or ""
        fru = (desc.get("fruText") or "").strip()
        # AMD vendor section decoded by amd_cper_sections takes precedence over
        # both libcper's message and the generic OEM fallback.
        amd_info = sec.get("amd") if isinstance(sec, dict) else None
        if amd_info:
            msg = amd_cper.summarize_amd_section(amd_info)
        else:
            msg = sec.get("message") if isinstance(sec, dict) else ""
        if not msg and isinstance(sec, dict):
            # OEM/vendor section libcper couldn't decode: surface what we do have
            # — section-type GUID, byte length, and any embedded ASCII strings.
            payload = next((sec[k] for k in sec if k not in ("message", "amd")), None)
            bits = []
            if guid:
                bits.append(f"type {guid}")
            length = desc.get("sectionLength")
            if length:
                bits.append(f"{length} bytes")
            b64 = payload.get("data") if isinstance(payload, dict) else None
            hints = _ascii_hints(b64)
            if hints:
                bits.append("strings: " + ", ".join(hints))
            msg = "OEM section (" + "; ".join(bits) + ")" if bits else "undecoded OEM section"
        label = ("OEM" if stype == "Unknown section" else stype) + (f"/{ssev}" if ssev else "")
        if fru:
            label += f" {fru}"
        parts.append(f"[{label}] {msg}".strip())

    head = f"CPER {record_sev}"
    if notif and notif.lower() != "unknown":
        head += f" ({notif})"
    body = "; ".join(p for p in parts if p) or "no decodable sections"
    summary = f"{head} — {len(sections)} section(s): {body}"
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 1] + "…"
    return summary
