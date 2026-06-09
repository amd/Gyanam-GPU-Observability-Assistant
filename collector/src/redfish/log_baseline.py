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
"""One-shot baseline pull of existing Redfish LogService entries.

SSE streams and webhook subscriptions only capture events emitted *after* the
subscription is established. Standing/active conditions already present on a BMC
are therefore invisible until a new event fires — which is why an "active"
subscription can show zero alerts. This module fetches the existing event-log
entries once (and on a periodic re-pull) so the baseline is populated. Dedup at
the DB layer keeps re-pulls idempotent.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .alert_subscriber import (
    AlertEvent,
    normalize_severity,
    parse_redfish_timestamp,
    severity_allowed,
)

logger = logging.getLogger(__name__)

AlertCallback = Callable[[AlertEvent], None]

# Roots whose LogServices we traverse. Chassis logs are intentionally excluded
# to bound traversal cost; add here if a fleet needs them.
_LOG_ROOTS = ("/redfish/v1/Systems", "/redfish/v1/Managers")


def _abs(base_url: str, uri: str) -> str:
    """Resolve an @odata.id (usually an absolute path) against the base URL."""
    if not uri:
        return ""
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    return f"{base_url.rstrip('/')}/{uri.lstrip('/')}"


def _extract_origin(entry: dict) -> str | None:
    """OriginOfCondition can live at the top level or under Links, as dict/str."""
    origin = entry.get("OriginOfCondition")
    if origin is None:
        links = entry.get("Links")
        if isinstance(links, dict):
            origin = links.get("OriginOfCondition")
    if isinstance(origin, dict):
        return origin.get("@odata.id")  # type: ignore[no-any-return]
    if isinstance(origin, str):
        return origin
    return None


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """GET a Redfish resource, returning parsed JSON or None on any failure."""
    try:
        resp = await client.get(url)
    except Exception as e:  # noqa: BLE001 - best-effort discovery
        logger.debug("Baseline GET failed for %s: %s", url, type(e).__name__)
        return None
    if resp.status_code != 200:
        logger.debug("Baseline GET %s returned HTTP %d", url, resp.status_code)
        return None
    try:
        return resp.json()  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        return None


async def _discover_entry_collections(client: httpx.AsyncClient, base_url: str) -> list[str]:
    """Find LogService Entries collection URIs under Systems + Managers."""
    collections: list[str] = []
    seen: set[str] = set()

    for root in _LOG_ROOTS:
        root_doc = await _get_json(client, _abs(base_url, root))
        if not root_doc:
            continue
        for member in root_doc.get("Members", []):
            member_uri = member.get("@odata.id") if isinstance(member, dict) else None
            if not member_uri:
                continue
            member_doc = await _get_json(client, _abs(base_url, member_uri))
            if not member_doc:
                continue
            ls_ref = member_doc.get("LogServices", {})
            ls_uri = ls_ref.get("@odata.id") if isinstance(ls_ref, dict) else None
            if not ls_uri:
                continue
            ls_doc = await _get_json(client, _abs(base_url, ls_uri))
            if not ls_doc:
                continue
            for ls_member in ls_doc.get("Members", []):
                ls_member_uri = ls_member.get("@odata.id") if isinstance(ls_member, dict) else None
                if not ls_member_uri:
                    continue
                ls_detail = await _get_json(client, _abs(base_url, ls_member_uri))
                if not ls_detail:
                    continue
                entries_ref = ls_detail.get("Entries", {})
                entries_uri = (
                    entries_ref.get("@odata.id") if isinstance(entries_ref, dict) else None
                )
                if entries_uri and entries_uri not in seen:
                    seen.add(entries_uri)
                    collections.append(entries_uri)

    return collections


def order_members_newest_first(members: list, max_entries: int) -> list[dict]:
    """Return the newest ``max_entries`` log members, newest-first.

    Sorts by ``Created`` when present, else a numeric entry Id parsed from
    ``@odata.id``, else keeps original order — so the cap keeps the newest
    entries regardless of whether the BMC returns the collection oldest- or
    newest-first.
    """
    dicts = [m for m in members if isinstance(m, dict)]

    def _sort_key(m: dict):
        ts = parse_redfish_timestamp(m.get("Created"))
        if ts is not None:
            return (2, ts.timestamp())
        oid = m.get("@odata.id", "") or ""
        try:
            return (1, float(oid.rstrip("/").split("/")[-1]))
        except (ValueError, IndexError):
            return (0, 0.0)

    dicts.sort(key=_sort_key, reverse=True)
    if len(dicts) > max_entries:
        dicts = dicts[:max_entries]
    return dicts


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to naive UTC for comparison with stored cursors.

    ``parse_redfish_timestamp`` yields tz-aware datetimes, but cursors are
    persisted as naive UTC. Comparing the two raises ``TypeError`` ("can't
    compare offset-naive and offset-aware datetimes"), which previously killed
    every incremental re-pull. Normalizing both sides here prevents that.
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


# Cap on pages followed via @odata.nextLink per collection, so a huge log can't
# make one pull walk thousands of pages. Bounded but far beyond a single page.
_MAX_PAGES = 20


async def _collect_members(
    client: httpx.AsyncClient, base_url: str, entries_uri: str, max_entries: int
) -> tuple[list[dict], bool]:
    """Gather collection Members, following ``@odata.nextLink`` up to a budget.

    Returns ``(members, truncated)``. Without this, only the first page is ever
    considered — on BMCs that paginate (or return oldest-first) the genuinely
    newest entries live on later pages and are silently missed.
    """
    members: list[dict] = []
    seen_ids: set[str] = set()
    visited: set[str] = set()
    uri: str | None = entries_uri
    pages = 0
    truncated = False
    while uri:
        # Guard against a BMC returning a self-referential / cyclic nextLink.
        if uri in visited:
            break
        visited.add(uri)
        doc = await _get_json(client, _abs(base_url, uri))
        if not doc:
            break
        for m in doc.get("Members", []):
            if not isinstance(m, dict):
                continue
            # Dedup by @odata.id so a paginating/cyclic BMC can't inflate the set
            # with repeats (members without an id are kept as-is).
            mid = m.get("@odata.id")
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            members.append(m)
        pages += 1
        uri = doc.get("Members@odata.nextLink")
        # Stop once we have comfortably more than the cap (we sort+trim after) or
        # hit the page budget.
        if len(members) >= max_entries * 3 or pages >= _MAX_PAGES:
            truncated = bool(uri)
            break
    return members, truncated


async def _pull_collection(
    client: httpx.AsyncClient,
    base_url: str,
    entries_uri: str,
    *,
    target_id: int,
    target_name: str,
    target_bmc: str,
    severities: list[str] | None,
    max_entries: int,
    callback: AlertCallback,
    cursor: datetime | None = None,
    is_known: Callable | None = None,
) -> tuple[int, datetime | None]:
    """Fetch entries from one collection and emit AlertEvents via callback.

    Returns ``(emitted, newest_created_seen)``. When ``cursor`` is given, entries
    with ``Created <= cursor`` are skipped (incremental re-pull). ``is_known`` (an
    awaitable ``source_id -> bool``) lets us skip re-fetching timestamp-less
    entries already stored.
    """
    all_members, truncated = await _collect_members(client, base_url, entries_uri, max_entries)
    if not all_members:
        return 0, None
    if truncated:
        logger.info(
            "Baseline: %s hit page budget (%d) for %s; older entries not scanned this pull",
            target_name,
            _MAX_PAGES,
            entries_uri,
        )

    members = order_members_newest_first(all_members, max_entries)

    # Cursors are stored naive-UTC; normalize once so per-entry comparisons below
    # never mix tz-aware and tz-naive datetimes.
    cursor = _naive_utc(cursor)

    emitted = 0
    newest: datetime | None = None
    for member in members:
        # Cheap pre-filter using the listing's OWN Created, before any per-entry
        # GET: skip entries older than the cursor without fetching them. Cuts the
        # per-entry round-trips on re-pulls from "whole tail" to "new entries".
        member_ts = _naive_utc(parse_redfish_timestamp(member.get("Created")))
        if cursor is not None and member_ts is not None and member_ts < cursor:
            continue
        # Timestamp-less entries would otherwise be re-fetched and re-hashed every
        # pull. When we can, skip ones already stored (their key is derived from
        # the stable @odata.id alone, so it's computable without the full entry).
        member_ref = member.get("@odata.id")
        if is_known is not None and member_ts is None and member_ref:
            try:
                if await is_known(member_ref):
                    continue
            except Exception as e:  # noqa: BLE001
                logger.debug("is_known check failed for %s: %s", target_name, e)

        entry = member
        # Members may be returned partial (reference-only, or inline but missing
        # Created/Message). Fetch the full entry whenever a key field is absent so
        # we capture the creation time and complete data.
        if any(k not in entry for k in ("Message", "Severity", "Created")):
            ref = member_ref
            if ref:
                fetched = await _get_json(client, _abs(base_url, ref))
                if fetched:
                    entry = fetched
        # Skip entries with no usable content at all.
        if "Message" not in entry and "Severity" not in entry and "MessageSeverity" not in entry:
            continue

        # Normalize to naive-UTC to match the stored cursor (see _naive_utc).
        event_ts = _naive_utc(parse_redfish_timestamp(entry.get("Created")))
        # Incremental: skip entries strictly older than the cursor. Use "<" (not
        # "<=") so a distinct entry created in the same second as the boundary is
        # still processed (dedup is the backstop). No-timestamp entries always
        # process.
        if cursor is not None and event_ts is not None and event_ts < cursor:
            continue
        if event_ts is not None and (newest is None or event_ts > newest):
            newest = event_ts

        severity, sev_present = normalize_severity(entry)
        if not severity_allowed(severity, sev_present, severities):
            continue

        # Stable per-entry identity so periodic re-pulls dedup even when the
        # entry has no Created timestamp.
        source_id = entry.get("@odata.id") or member.get("@odata.id")

        alert = AlertEvent(
            target_id=target_id,
            target_name=target_name,
            target_bmc=target_bmc,
            severity=severity,
            message=entry.get("Message", "") or "",
            message_id=entry.get("MessageId"),
            event_type=entry.get("EntryType") or "Alert",
            origin_of_condition=_extract_origin(entry),
            event_timestamp=event_ts,
            received_at=datetime.now(UTC),
            source_id=source_id,
            raw=entry,
        )
        try:
            callback(alert)
            emitted += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("Baseline callback error for %s: %s", target_name, e)

    return emitted, newest


async def pull_baseline_alerts(
    *,
    target_id: int,
    target_name: str,
    target_bmc: str,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool,
    callback: AlertCallback,
    severities: list[str] | None = None,
    max_entries_per_log: int = 200,
    timeout: float = 30.0,
    get_cursor: Callable | None = None,
    set_cursor: Callable | None = None,
    is_known: Callable | None = None,
) -> int:
    """Pull existing log entries from a target and feed them to ``callback``.

    Best-effort: any per-collection or per-entry failure is logged at DEBUG and
    skipped. Returns the number of AlertEvents emitted (before DB dedup).

    ``get_cursor(entries_uri)`` / ``set_cursor(entries_uri, dt)`` (awaitables), if
    provided, enable incremental re-pull: only entries newer than the stored
    high-water mark are processed, and the mark is advanced afterwards.
    ``is_known(source_id)`` (awaitable -> bool), if provided, lets timestamp-less
    entries already stored be skipped without a per-entry fetch.
    """
    auth = httpx.BasicAuth(username, password)
    emitted = 0
    try:
        async with httpx.AsyncClient(
            auth=auth, verify=verify_ssl, timeout=timeout, follow_redirects=True
        ) as client:
            collections = await _discover_entry_collections(client, base_url)
            if not collections:
                logger.debug("No log-entry collections discovered for %s", target_name)
                return 0
            failures = 0
            last_error = ""
            for entries_uri in collections:
                try:
                    cursor = await get_cursor(entries_uri) if get_cursor else None
                    count, newest = await _pull_collection(
                        client,
                        base_url,
                        entries_uri,
                        target_id=target_id,
                        target_name=target_name,
                        target_bmc=target_bmc,
                        severities=severities,
                        max_entries=max_entries_per_log,
                        callback=callback,
                        cursor=cursor,
                        is_known=is_known,
                    )
                    emitted += count
                    if set_cursor and newest is not None:
                        await set_cursor(entries_uri, newest)
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    last_error = f"{type(e).__name__}: {e}"
                    logger.debug(
                        "Baseline pull failed for %s collection %s: %s",
                        target_name,
                        entries_uri,
                        last_error,
                    )
            # Every collection failing is a systemic defect (e.g. the tz-aware vs
            # naive cursor comparison that once silently disabled all re-pulls),
            # not a flaky endpoint. Surface it loudly instead of hiding at DEBUG.
            if failures and failures == len(collections):
                logger.warning(
                    "Baseline pull for %s: ALL %d log collections failed (last error: %s)",
                    target_name,
                    failures,
                    last_error,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Baseline alert pull failed for %s: %s: %s", target_name, type(e).__name__, e
        )

    if emitted:
        logger.info("Baseline pull for %s emitted %d log entries", target_name, emitted)
    return emitted
