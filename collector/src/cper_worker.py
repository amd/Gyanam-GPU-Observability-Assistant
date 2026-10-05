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
"""Background worker that enriches CPER alerts into decoded, human-readable form.

Extracted from AlertManager so the enrichment pipeline is isolated and
independently testable. It owns its own asyncio task, metrics, and per-target
throttles; AlertManager just starts/stops it and folds its stats() into the
overall alert-manager stats.

Decode progress is persisted in PostgreSQL (cper_status/cper_attempts/
cper_decoded), so across restarts the worker resumes where it left off:
already-decoded rows are skipped and only outstanding work is picked up.
"""

import asyncio
import logging
import time
from contextlib import suppress

from .redfish import cper_decoder

logger = logging.getLogger(__name__)


class CperEnrichmentWorker:
    """Fetches CPER attachments from BMCs, decodes them, and stores the result."""

    def __init__(
        self,
        repository,
        *,
        enabled: bool = True,
        poll_interval_seconds: int = 30,
        batch_size: int = 20,
        max_attempts: int = 3,
        concurrency: int = 3,
        per_target_concurrency: int = 1,
        maintenance_every_cycles: int = 10,
        fetch_timeout: float = 60.0,
        decode_timeout: float = 15.0,
        max_bytes: int = 8 * 1024 * 1024,
        convert_path: str = "/usr/local/bin/cper-convert",
        retry_failed_interval_minutes: int = 360,
        attempt_cooldown_seconds: int = 60,
    ):
        self.repository = repository
        self.enabled = enabled
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.concurrency = concurrency
        self.per_target_concurrency = per_target_concurrency
        self.maintenance_every_cycles = maintenance_every_cycles
        self.fetch_timeout = fetch_timeout
        self.decode_timeout = decode_timeout
        self.max_bytes = max_bytes
        self.convert_path = convert_path
        self.retry_failed_interval_minutes = retry_failed_interval_minutes
        self.attempt_cooldown_seconds = attempt_cooldown_seconds

        self._running = False
        self._task: asyncio.Task | None = None
        self._decoded = 0
        self._failed = 0
        self._status_counts: dict[str, int] = {}
        # Per-target fetch throttles (lazy): one BMC gets at most
        # per_target_concurrency in-flight fetches (anti-hammer).
        self._target_sems: dict[int, asyncio.Semaphore] = {}

    async def start(self) -> None:
        """Start the enrichment loop (no-op if disabled or already running)."""
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the enrichment loop and await the task."""
        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def stats(self) -> dict:
        """Cumulative counters + cached backlog for alert-manager stats."""
        return {
            "cper_decoded": self._decoded,
            "cper_failed": self._failed,
            "cper_backlog": dict(self._status_counts),
        }

    def _refresh_backlog(self, counts: dict) -> None:
        """Cache CPER backlog counts for alert-manager stats."""
        self._status_counts = dict(counts)

    async def _loop(self) -> None:
        """Periodically decode CPER attachments for pending alerts."""
        interval = self.poll_interval_seconds
        sem = asyncio.Semaphore(max(1, self.concurrency))

        try:
            resume = await self.repository.count_cper_by_status()
            self._refresh_backlog(resume)
            if resume.get("pending") or resume.get("fetch_failed"):
                logger.info(
                    "CPER enrichment resuming: %d already decoded, %d pending, "
                    "%d failed (awaiting retry)",
                    resume.get("decoded", 0),
                    resume.get("pending", 0),
                    resume.get("fetch_failed", 0),
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("CPER resume-state count failed: %s", e)

        async def _guarded(row):
            # Per-target cap FIRST, then global — so a row only consumes a global
            # slot once it can actually run. Acquiring global first would let rows
            # for one busy BMC hold global slots while blocked on the per-target
            # lock, starving other (idle) BMCs' rows (head-of-line blocking).
            tsem = self._target_sems.get(row.target_id)
            if tsem is None:
                tsem = asyncio.Semaphore(max(1, self.per_target_concurrency))
                self._target_sems[row.target_id] = tsem
            async with tsem, sem:
                if self._running:
                    try:
                        return await self._enrich_one(row)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # Any UNEXPECTED failure (not already turned into an
                        # outcome by _enrich_one) must still advance the attempt
                        # counter — otherwise a poison row is re-selected every
                        # cycle forever and never finalizes.
                        self._failed += 1
                        attempts = row.attempts + 1
                        terminal = attempts >= self.max_attempts
                        logger.warning(
                            "CPER enrich crashed for alert %s (attempt %d/%d): %s",
                            row.id,
                            attempts,
                            self.max_attempts,
                            type(e).__name__,
                        )
                        return {
                            "id": row.id,
                            "status": "decode_failed" if terminal else "pending",
                            "increment_attempt": True,
                        }
            return None

        # Maintenance cadence is wall-clock based, NOT per-cycle: the adaptive
        # drain runs cycles every ~0.1s under backlog, so a cycle-count gate would
        # fire maintenance ~300x too often (and re-run the full backlog count)
        # exactly when the DB is busiest.
        maintenance_interval = max(1, self.maintenance_every_cycles) * max(1, interval)
        last_maintenance = 0.0

        while self._running:
            results: list[dict] = []
            try:
                now = time.monotonic()
                if now - last_maintenance >= maintenance_interval:
                    last_maintenance = now
                    if self.retry_failed_interval_minutes:
                        n = await self.repository.requeue_failed_cper(
                            self.retry_failed_interval_minutes, limit=self.batch_size
                        )
                        if n:
                            logger.info("Requeued %d failed CPER alert(s) for retry", n)
                    await self.repository.finalize_exhausted_cper(self.max_attempts)
                    with suppress(Exception):
                        self._refresh_backlog(await self.repository.count_cper_by_status())

                rows = await self.repository.get_pending_cper_alerts(
                    limit=self.batch_size,
                    max_attempts=self.max_attempts,
                    attempt_cooldown_seconds=self.attempt_cooldown_seconds,
                )
                if rows:
                    # Process the batch concurrently (globally + per-target bounded).
                    gathered = await asyncio.gather(
                        *(_guarded(r) for r in rows), return_exceptions=True
                    )
                    results = [g for g in gathered if isinstance(g, dict)]
                    # Persist all outcomes in one transaction (one commit/batch).
                    await self.repository.set_cper_results_batch(results)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Error in CPER enrichment loop: {type(e).__name__}: {e}", exc_info=True
                )

            # Adaptive pacing: if we just filled a batch there is likely more
            # work — loop again promptly so a backlog drains; only sleep the full
            # interval when the queue is (near) empty.
            try:
                if len(results) >= self.batch_size:
                    await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _enrich_one(self, row) -> dict | None:
        """Decode one alert's CPER and RETURN the outcome (persisted in batch).

        ``row`` is a lightweight PendingCper (id/target_id/uri/attempts/
        has_decoded). Returns a result dict for set_cper_results_batch, or None
        if nothing to persist. If the raw decoded record already exists (re-apply
        decoder logic after a code change), re-summarize locally without
        re-fetching — the BMC's Dump entries rotate, so re-fetching often fails.
        """
        if row.has_decoded:
            decoded = await self.repository.get_alert_cper(row.id)
            if decoded:
                cper_decoder.enrich_amd_sections(decoded)
                refined = cper_decoder.summarize_cper(decoded)
                self._decoded += 1
                return {
                    "id": row.id,
                    "status": "decoded",
                    "refined_message": refined,
                    "decoded": decoded,
                }
            # No blob after all — fall through to fetch.

        if not row.uri:
            return {"id": row.id, "status": "no_data"}

        target = await self.repository.get_target(row.target_id)
        if target is None:
            # Target removed; we can no longer authenticate to fetch the blob.
            return {"id": row.id, "status": "unavailable"}

        try:
            password = self.repository.decrypt_password(target)
            data = await cper_decoder.fetch_cper_attachment(
                base_url=target.base_url,
                uri=row.uri,
                username=target.username,
                password=password,
                verify_ssl=target.verify_ssl,
                timeout=self.fetch_timeout,
                max_bytes=self.max_bytes,
            )
        except cper_decoder.CperGoneError:
            return {"id": row.id, "status": "unavailable"}
        except Exception as e:
            # Transient (network/5xx/oversize): count the attempt and retry later
            # until max_attempts, then give up with a terminal status.
            self._failed += 1
            attempts = row.attempts + 1
            terminal = attempts >= self.max_attempts
            logger.warning(
                "CPER fetch failed for alert %d (attempt %d/%d): %s: %s",
                row.id,
                attempts,
                self.max_attempts,
                type(e).__name__,
                e,
            )
            return {
                "id": row.id,
                "status": "fetch_failed" if terminal else "pending",
                "increment_attempt": True,
            }

        decoded = await cper_decoder.decode_cper(
            data,
            cper_convert_path=self.convert_path,
            timeout=self.decode_timeout,
        )
        if decoded is None:
            self._failed += 1
            attempts = row.attempts + 1
            terminal = attempts >= self.max_attempts
            return {
                "id": row.id,
                "status": "decode_failed" if terminal else "pending",
                "increment_attempt": True,
            }

        # Decode AMD vendor sections libcper leaves opaque, then summarize.
        cper_decoder.enrich_amd_sections(decoded)
        refined = cper_decoder.summarize_cper(decoded)
        self._decoded += 1
        logger.debug("Decoded CPER for alert %d: %s", row.id, refined[:120])
        return {
            "id": row.id,
            "status": "decoded",
            "refined_message": refined,
            "decoded": decoded,
        }
