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
"""On-demand diagnostic log collection service.

Downloads raw tar.gz bundles from BMC targets via the same Redfish
task-based workflow used by the poller, but stores the blob to disk
instead of unpacking it for metric extraction.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from .database.models import Target
from .database.repository import TargetRepository
from .redfish.client import RedfishClient

logger = logging.getLogger(__name__)

# Replace non-alphanumeric characters (except dot/hyphen) with underscore
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9.\-]")
_COLLAPSE_RE = re.compile(r"_+")


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    result = _SANITIZE_RE.sub("_", name)
    result = _COLLAPSE_RE.sub("_", result)
    return result.strip("_") or "target"


class LogCollector:
    """Collects diagnostic log bundles from Redfish targets."""

    def __init__(
        self,
        repository: TargetRepository,
        storage_dir: str,
        max_concurrent: int = 5,
        timeout: int = 30,
        task_poll_interval: int = 5,
        task_timeout: int = 600,
        download_timeout: int = 600,
        collect_endpoint: str = "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData",
        collect_body: dict | None = None,
    ):
        self.repository = repository
        self.storage_dir = Path(storage_dir)
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.task_poll_interval = task_poll_interval
        self.task_timeout = task_timeout
        self.download_timeout = download_timeout
        self.collect_endpoint = collect_endpoint
        self.collect_body = collect_body or {
            "DiagnosticDataType": "OEM",
            "OEMDiagnosticDataType": "AllLogs",
        }
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Per-target locks to prevent double collection of same target
        self._target_locks: dict[int, asyncio.Lock] = {}
        self._bulk_in_progress = False

        # Ensure storage directory exists
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_target_lock(self, target_id: int) -> asyncio.Lock:
        if target_id not in self._target_locks:
            self._target_locks[target_id] = asyncio.Lock()
        return self._target_locks[target_id]

    async def collect_single(self, target_id: int) -> dict:
        """Collect diagnostic logs from a single target.

        Returns:
            Dict with collection result info.
        """
        target = await self.repository.get_target(target_id)
        if not target:
            return {"success": False, "error": "Target not found"}

        lock = self._get_target_lock(target_id)
        if lock.locked():
            return {"success": False, "error": "Collection already in progress for this target"}

        async with lock:
            return await self._collect_from_target(target)

    async def collect_all(self) -> dict:
        """Collect logs from all enabled targets in parallel.

        Returns:
            Summary dict with per-target results.
        """
        if self._bulk_in_progress:
            return {"success": False, "error": "Bulk collection already in progress"}

        self._bulk_in_progress = True
        try:
            targets = await self.repository.get_all_targets(enabled_only=True)
            if not targets:
                return {"success": True, "message": "No enabled targets", "results": []}

            async def _collect_with_semaphore(t: Target) -> dict:
                async with self._semaphore:
                    lock = self._get_target_lock(t.id)
                    if lock.locked():
                        return {
                            "target_id": t.id,
                            "target_name": t.name,
                            "success": False,
                            "error": "Already in progress",
                        }
                    async with lock:
                        return await self._collect_from_target(t)

            results = await asyncio.gather(
                *[_collect_with_semaphore(t) for t in targets],
                return_exceptions=True,
            )

            summaries = []
            for target, result in zip(targets, results, strict=False):
                if isinstance(result, Exception):
                    # Log full exception; surface only the type name (matches
                    # _collect_from_target's failure-branch contract).
                    logger.error(
                        f"Bulk collect crashed for {target.name}: {result}",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    summaries.append(
                        {
                            "target_id": target.id,
                            "target_name": target.name,
                            "success": False,
                            "error": type(result).__name__,
                        }
                    )
                else:
                    summaries.append(result)  # type: ignore[arg-type]

            succeeded = sum(1 for r in summaries if r.get("success"))
            return {
                "success": True,
                "total": len(targets),
                "succeeded": succeeded,
                "failed": len(targets) - succeeded,
                "results": summaries,
            }
        finally:
            self._bulk_in_progress = False

    async def _collect_from_target(self, target: Target) -> dict:
        """Download the diagnostic log bundle from a single target."""
        start_time = datetime.now(UTC)
        timestamp_str = start_time.strftime("%Y%m%d_%H%M%S_%f")
        safe_name = sanitize_filename(target.name)
        filename = f"{safe_name}_all_logs_{timestamp_str}.tar.gz"
        file_path = self.storage_dir / filename

        # Create pending DB record
        log_record = await self.repository.create_collected_log(
            target_id=target.id,
            target_name=target.name,
            target_host=target.host,
            filename=filename,
            file_path=str(file_path),
            status="collecting",
        )

        try:
            # SSH proxy with custom tool: use hwdiag + SCP approach
            if (
                target.connection_mode == "ssh_proxy"
                and target.ssh_command_template
                and target.ssh_command_template.strip()
            ):
                await self._collect_via_ssh_tool(target, file_path)
                file_size = file_path.stat().st_size
            else:
                content = await self._collect_via_redfish(target)
                file_path.write_bytes(content)
                file_size = len(content)

            end_time = datetime.now(UTC)
            duration_ms = (end_time - start_time).total_seconds() * 1000

            await self.repository.update_collected_log(
                log_record.id,
                status="completed",
                file_size_bytes=file_size,
                duration_ms=duration_ms,
            )

            logger.info(
                f"Collected logs from {target.name}: {filename} "
                f"({file_size / 1024 / 1024:.1f} MB in {duration_ms:.0f}ms)"
            )

            return {
                "success": True,
                "target_id": target.id,
                "target_name": target.name,
                "log_id": log_record.id,
                "filename": filename,
                "file_size_bytes": file_size,
                "duration_ms": duration_ms,
            }

        except Exception as e:
            end_time = datetime.now(UTC)
            duration_ms = (end_time - start_time).total_seconds() * 1000
            # Persist the detailed error to the DB (admin-only audit) but
            # surface only the exception type to the upstream API result so
            # raw exception text never reaches user-facing HTTP responses.
            error_msg = str(e)
            error_type = type(e).__name__

            await self.repository.update_collected_log(
                log_record.id,
                status="failed",
                error_message=error_msg,
                duration_ms=duration_ms,
            )

            logger.error(f"Failed to collect logs from {target.name}: {error_msg}", exc_info=True)

            return {
                "success": False,
                "target_id": target.id,
                "target_name": target.name,
                "log_id": log_record.id,
                "error": error_type,
                "duration_ms": duration_ms,
            }

    async def _collect_via_redfish(self, target: Target) -> bytes:
        """Collect logs via the standard Redfish task-based workflow."""
        password = self.repository.decrypt_password(target)
        token = self.repository.decrypt_token(target)

        # Create SSH transport if target uses proxy mode (curl-based SSH)
        ssh_transport = None
        if target.connection_mode == "ssh_proxy":
            from .redfish.ssh_transport import SSHTransport

            ssh_key = self.repository.decrypt_ssh_key(target)
            ssh_password = self.repository.decrypt_ssh_password(target)
            ssh_transport = SSHTransport(
                proxy_host=target.ssh_proxy_host,
                proxy_port=target.ssh_proxy_port or 22,
                proxy_username=target.ssh_proxy_username or "root",
                ssh_key=ssh_key,
                ssh_password=ssh_password,
                command_timeout=self.download_timeout,
                verify_ssl=target.verify_ssl,
            )

        async with RedfishClient(
            base_url=target.base_url,
            username=target.username,
            password=password,
            token=token,
            timeout=self.timeout,
            verify_ssl=target.verify_ssl,
            task_poll_interval=self.task_poll_interval,
            task_timeout=self.task_timeout,
            download_timeout=self.download_timeout,
            cleanup_task_on_success=True,
            ssh_transport=ssh_transport,
        ) as client:
            endpoint = target.telemetry_endpoint or self.collect_endpoint
            response = await client.collect_diagnostic_data(
                collect_endpoint=endpoint,
                collect_body=self.collect_body,
            )

        if not response.success:
            raise RuntimeError(response.error_message or "Collection failed")

        return response.content

    async def _collect_via_ssh_tool(self, target: Target, local_path: Path) -> None:
        """Collect logs via SSH using hwdiag + SCP.

        For SSH proxy targets with a custom CLI tool:
        1. Run 'hwdiag gpu get_bmc_log all' on the proxy BMC
        2. Parse output for the log file path (/diag/log/...)
        3. SCP the file back to the collector's local storage
        """
        import asyncssh

        from .redfish.ssh_transport import SSHTransport

        ssh_key = self.repository.decrypt_ssh_key(target)
        ssh_password = self.repository.decrypt_ssh_password(target)
        transport = SSHTransport(
            proxy_host=target.ssh_proxy_host,
            proxy_port=target.ssh_proxy_port or 22,
            proxy_username=target.ssh_proxy_username or "root",
            ssh_key=ssh_key,
            ssh_password=ssh_password,
            command_timeout=self.task_timeout,
            verify_ssl=target.verify_ssl,
        )

        try:
            await transport.connect()

            # Step 1: Run hwdiag to collect logs (may take several minutes)
            logger.info(f"Running hwdiag log collection on {target.ssh_proxy_host}...")
            result = await transport._exec(
                "hwdiag gpu get_bmc_log all",
                timeout=self.task_timeout,
            )

            if result.exit_status != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                raise RuntimeError(f"hwdiag failed (exit {result.exit_status}): {stderr or stdout}")

            stdout = result.stdout or ""
            logger.debug(f"hwdiag output: {stdout[:500]}")

            # Step 2: Find the log file path in the output
            remote_path = None
            for line in stdout.splitlines():
                line = line.strip()
                # Look for path starting with /diag/log/ (exclude trailing punctuation)
                match = re.search(r"(/diag/log/[^\s,;:)\"\']+)", line)
                if match:
                    remote_path = match.group(1)
                    break

            if not remote_path:
                raise RuntimeError(
                    f"Could not find log file path in hwdiag output. Output: {stdout[:500]}"
                )

            logger.info(f"Log file on proxy: {remote_path}")

            # Step 3: SCP the file from the proxy BMC to local storage
            logger.info(f"SCP {target.ssh_proxy_host}:{remote_path} -> {local_path}")
            await asyncssh.scp(
                (transport._conn, remote_path),
                str(local_path),
            )

            file_size = local_path.stat().st_size
            logger.info(
                f"Transferred {file_size / 1024 / 1024:.1f} MB from "
                f"{target.ssh_proxy_host}:{remote_path}"
            )

        finally:
            await transport.close()

    def delete_file(self, file_path: str) -> bool:
        """Remove a collected log file from disk.

        Validates the path is within the storage directory to prevent
        path traversal attacks.
        """
        p = Path(file_path).resolve()
        storage_resolved = self.storage_dir.resolve()
        if not p.is_relative_to(storage_resolved):
            logger.warning(f"Refusing to delete file outside storage dir: {file_path}")
            return False
        if p.exists():
            p.unlink()
            return True
        return False
