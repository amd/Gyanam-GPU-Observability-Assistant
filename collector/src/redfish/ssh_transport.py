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
"""SSH transport for Redfish API access via a proxy BMC host.

Connects to a proxy host via SSH and executes curl commands to reach
the target Redfish endpoint. Used when the target BMC is not directly
reachable from the collector.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field

import asyncssh

logger = logging.getLogger(__name__)


class CaseInsensitiveDict(dict):
    """Dict with case-insensitive key lookup, matching httpx.Headers behavior."""

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __delitem__(self, key):
        super().__delitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def pop(self, key, *args):
        return super().pop(key.lower(), *args)

    def setdefault(self, key, default=None):
        return super().setdefault(key.lower(), default)

    def update(self, other=None, **kwargs):
        if other:
            for k, v in other.items() if hasattr(other, "items") else other:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v


@dataclass
class SSHResponse:
    """Response from an SSH-proxied HTTP request.

    Mimics the subset of httpx.Response that RedfishClient uses, so
    RedfishClient can work with either transport transparently.
    Headers are case-insensitive to match httpx.Headers behavior.
    """

    status_code: int
    content: bytes
    headers: CaseInsensitiveDict = field(default_factory=CaseInsensitiveDict)
    text: str = ""

    def json(self) -> dict:
        return json.loads(self.content)  # type: ignore[no-any-return]


class SSHTransport:
    """Executes HTTP requests via SSH on a remote proxy host.

    Supports two modes:
    - **curl** (default): Builds full curl commands with auth, headers,
      and status code parsing. Used when command_template is not set.
    - **Custom tool** (e.g. redfishclient): When command_template is set,
      uses ``<tool> get <path>`` / ``<tool> post <path>`` syntax.
      The tool runs locally on the BMC, handles its own auth, and dumps
      JSON to stdout. Exit code 0 = success, non-zero = error.

    Lifecycle: create -> connect() -> get()/post()/delete() -> close()
    """

    MAX_RESPONSE_SIZE = 50 * 1024 * 1024  # 50MB

    def __init__(
        self,
        proxy_host: str,
        proxy_port: int = 22,
        proxy_username: str = "root",
        ssh_key: str | None = None,
        ssh_password: str | None = None,
        command_template: str | None = None,
        command_timeout: int = 300,
        verify_ssl: bool = False,
    ):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxy_username = proxy_username
        self.ssh_key = ssh_key
        self.ssh_password = ssh_password
        self.command_template = (
            command_template  # e.g. "redfishclient" — uses <tool> get/post <path>
        )
        self.command_timeout = command_timeout
        self.verify_ssl = verify_ssl
        self._conn: asyncssh.SSHClientConnection | None = None
        # Unique suffix for temp files to avoid race conditions when
        # multiple targets use the same SSH proxy host concurrently
        self._tmp_suffix = os.urandom(8).hex()

    async def connect(self) -> None:
        """Establish SSH connection to the proxy host."""
        connect_kwargs = {
            "host": self.proxy_host,
            "port": self.proxy_port,
            "username": self.proxy_username,
            "known_hosts": None,  # BMC internal network, no host key DB
        }

        if self.ssh_key:
            key = asyncssh.import_private_key(self.ssh_key)
            connect_kwargs["client_keys"] = [key]

        if self.ssh_password:
            connect_kwargs["password"] = self.ssh_password

        try:
            self._conn = await asyncssh.connect(**connect_kwargs)
            logger.info(f"SSH connected to {self.proxy_host}:{self.proxy_port}")
        except Exception as e:
            logger.error(f"SSH connection failed to {self.proxy_host}:{self.proxy_port}: {e}")
            raise

    async def close(self) -> None:
        """Close the SSH connection."""
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
            logger.debug(f"SSH connection closed to {self.proxy_host}")

    def _build_curl_command(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        json_body: dict | None = None,
        binary_output: bool = False,
        capture_headers: bool = False,
    ) -> str:
        """Build a shell-safe curl command.

        All user-supplied values are passed through shlex.quote() to
        prevent shell injection.

        Args:
            capture_headers: If True, dump response headers to
                /tmp/_gyanam_hdrs so the caller can read them.
                Used for POST responses that may include a Location header.
        """
        parts = ["curl", "-s"]  # silent
        if not self.verify_ssl:
            parts.append("-k")  # skip TLS verification

        if method.upper() != "GET":
            parts.extend(["-X", method.upper()])

        if capture_headers:
            parts.extend(["-D", f"/tmp/_gyanam_hdrs_{self._tmp_suffix}"])

        for key, value in (headers or {}).items():
            parts.extend(["-H", shlex.quote(f"{key}: {value}")])

        if auth:
            parts.extend(["-u", shlex.quote(f"{auth[0]}:{auth[1]}")])

        if json_body is not None:
            parts.extend(["-d", shlex.quote(json.dumps(json_body))])
            header_keys_lower = {k.lower() for k in (headers or {})}
            if "content-type" not in header_keys_lower:
                parts.extend(["-H", shlex.quote("Content-Type: application/json")])

        if not binary_output:
            # Append HTTP status code on last line, separated by newline
            parts.extend(["-o-", "-w", r"'\n%{http_code}'"])
        else:
            # Write body to temp file, status code to stdout, then base64 the body
            parts.extend(["-o", f"/tmp/_gyanam_resp_{self._tmp_suffix}", "-w", "'%{http_code}'"])

        parts.append(shlex.quote(url))

        cmd = " ".join(parts)

        if binary_output:
            resp_file = f"/tmp/_gyanam_resp_{self._tmp_suffix}"
            # Use subshell so cleanup runs even if curl/base64 fails
            cmd = f"({cmd} && echo '' && base64 {resp_file}); rm -f {resp_file}"

        return cmd

    @property
    def _use_custom_tool(self) -> bool:
        """Whether a custom CLI tool is configured instead of curl."""
        return bool(self.command_template and self.command_template.strip())

    @staticmethod
    def _extract_path(url: str) -> str:
        """Extract the path from a full URL for custom tool commands.

        Custom tools like redfishclient take paths, not full URLs:
          redfishclient get /redfish/v1/TelemetryService/MetricReports/All
        """
        # Handle full URLs: https://192.168.31.1/redfish/v1/...
        if "://" in url:
            # Split off scheme + host, keep path
            after_scheme = url.split("://", 1)[1]
            slash_pos = after_scheme.find("/")
            if slash_pos >= 0:
                return after_scheme[slash_pos:]
            return "/"
        return url

    def _build_custom_command(
        self,
        method: str,
        url: str,
        json_body: dict | None = None,
        binary_output: bool = False,
    ) -> str:
        """Build a command for a custom CLI tool (e.g. redfishclient).

        Format: <tool> <method> <path>
        The tool runs locally on the BMC, handles its own auth,
        and writes JSON output to stdout.
        """
        tool = shlex.quote(self.command_template.strip())
        path = shlex.quote(self._extract_path(url))
        verb = method.lower()

        parts = [tool, verb, path]

        if json_body is not None:
            parts.extend(["--data", shlex.quote(json.dumps(json_body))])

        cmd = " ".join(parts)

        if binary_output:
            # Pipe binary output through base64 for safe SSH transfer
            resp_file = f"/tmp/_gyanam_resp_{self._tmp_suffix}"
            cmd = f"({cmd} > {resp_file} && echo '200' && base64 {resp_file}); rm -f {resp_file}"

        return cmd

    def _parse_custom_response(self, result: asyncssh.SSHCompletedProcess) -> SSHResponse:
        """Parse output from a custom CLI tool.

        Custom tools dump raw output to stdout. Exit code determines success:
          0 = HTTP 200 equivalent, non-zero = error.
        """
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        success = result.exit_status == 0

        if not success:
            error_msg = (
                stderr.strip()
                or stdout.strip()
                or f"Command exited with status {result.exit_status}"
            )
            logger.warning(f"Custom tool error (exit {result.exit_status}): {error_msg[:200]}")
            return SSHResponse(
                status_code=500,
                content=error_msg.encode("utf-8"),
                headers=CaseInsensitiveDict({"content-type": "text/plain"}),
                text=error_msg,
            )

        # Detect JSON content robustly: try parsing, and if the raw output
        # has non-JSON prefix (status lines, ANSI codes, etc.), find the
        # JSON object/array within the output.
        ct = "application/octet-stream"
        json_text = stdout

        # First try: raw output is valid JSON
        try:
            json.loads(stdout)
            ct = "application/json"
        except (json.JSONDecodeError, ValueError):
            # Second try: scan for a valid JSON object/array in the output
            for i, ch in enumerate(stdout):
                if ch in ("{", "["):
                    candidate = stdout[i:]
                    try:
                        json.loads(candidate)
                        json_text = candidate
                        ct = "application/json"
                        if i > 0:
                            logger.debug(
                                f"Custom tool output had {i} bytes of non-JSON prefix, stripped"
                            )
                        break
                    except (json.JSONDecodeError, ValueError):
                        continue  # keep scanning for next { or [

        content = json_text.encode("utf-8")

        return SSHResponse(
            status_code=200,
            content=content,
            headers=CaseInsensitiveDict({"content-type": ct}),
            text=json_text,
        )

    async def _exec(self, cmd: str, timeout: int | None = None) -> asyncssh.SSHCompletedProcess:
        """Execute a command on the remote host."""
        if not self._conn:
            raise RuntimeError("SSH transport not connected")

        effective_timeout = timeout or self.command_timeout
        # Redact -u credentials from log output
        log_cmd = re.sub(r" -u \S+", " -u ***", cmd) if " -u " in cmd else cmd
        logger.debug(f"SSH exec: {log_cmd[:200]}...")
        try:
            result = await asyncio.wait_for(
                self._conn.run(cmd, check=False),
                timeout=effective_timeout,
            )
            return result
        except TimeoutError:
            raise TimeoutError(f"SSH command timed out after {effective_timeout}s")

    def _parse_text_response(self, stdout: str) -> SSHResponse:
        """Parse curl text output: body + status code on last line."""
        lines = stdout.rsplit("\n", 1)
        if len(lines) == 2:
            body = lines[0]
            try:
                status_code = int(lines[1].strip().strip("'"))
            except ValueError:
                body = stdout
                status_code = 0
        else:
            body = stdout
            status_code = 0

        content = body.encode("utf-8")
        ct = "application/octet-stream"
        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            ct = "application/json"

        return SSHResponse(
            status_code=status_code,
            content=content,
            headers=CaseInsensitiveDict({"content-type": ct}),
            text=body,
        )

    def _parse_binary_response(self, stdout: str) -> SSHResponse:
        """Parse curl binary output: status_code\\nbase64_body."""
        lines = stdout.split("\n", 1)
        try:
            status_code = int(lines[0].strip().strip("'"))
        except (ValueError, IndexError):
            status_code = 0

        content = b""
        if len(lines) > 1:
            b64_data = lines[1].strip()
            if b64_data:
                try:
                    content = base64.b64decode(b64_data)
                except Exception as e:
                    logger.error(f"Base64 decode failed: {e}")

        return SSHResponse(
            status_code=status_code,
            content=content,
            headers=CaseInsensitiveDict({"content-type": "application/octet-stream"}),
            text="",
        )

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        timeout: int | None = None,
        binary: bool = False,
    ) -> SSHResponse:
        """Execute an HTTP GET via SSH."""
        if self._use_custom_tool:
            cmd = self._build_custom_command(method="GET", url=url, binary_output=binary)
            result = await self._exec(cmd, timeout=timeout)
            # Check exit status first — custom tool errors should be reported clearly
            if result.exit_status != 0:
                return self._parse_custom_response(result)
            if binary:
                return self._parse_binary_response(result.stdout or "")
            return self._parse_custom_response(result)

        cmd = self._build_curl_command(
            method="GET",
            url=url,
            headers=headers,
            auth=auth,
            binary_output=binary,
        )
        result = await self._exec(cmd, timeout=timeout)
        if result.exit_status != 0 and result.stderr:
            logger.warning(f"curl stderr: {result.stderr.strip()}")
        if binary:
            return self._parse_binary_response(result.stdout or "")
        return self._parse_text_response(result.stdout or "")

    async def _read_captured_headers(self) -> dict[str, str]:
        """Read and parse response headers captured by curl -D."""
        parsed = {}
        try:
            result = await self._exec(
                f"cat /tmp/_gyanam_hdrs_{self._tmp_suffix} 2>/dev/null; rm -f /tmp/_gyanam_hdrs_{self._tmp_suffix}",
                timeout=5,
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    # Skip HTTP status line (e.g. "HTTP/1.1 202 Accepted")
                    if line.startswith("HTTP/"):
                        continue
                    if ":" in line:
                        key, _, value = line.partition(":")
                        parsed[key.strip()] = value.strip()
        except Exception as e:
            logger.debug(f"Failed to read captured headers: {e}")
        return parsed

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        json_body: dict | None = None,
        timeout: int | None = None,
    ) -> SSHResponse:
        """Execute an HTTP POST via SSH."""
        if self._use_custom_tool:
            cmd = self._build_custom_command(method="POST", url=url, json_body=json_body)
            result = await self._exec(cmd, timeout=timeout)
            return self._parse_custom_response(result)

        # curl path: capture response headers for Location header on 202
        cmd = self._build_curl_command(
            method="POST",
            url=url,
            headers=headers,
            auth=auth,
            json_body=json_body,
            capture_headers=True,
        )
        result = await self._exec(cmd, timeout=timeout)
        if result.exit_status != 0 and result.stderr:
            logger.warning(f"curl stderr: {result.stderr.strip()}")
        response = self._parse_text_response(result.stdout or "")
        captured = await self._read_captured_headers()
        response.headers.update(captured)
        return response

    async def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> SSHResponse:
        """Execute an HTTP DELETE via SSH."""
        if self._use_custom_tool:
            cmd = self._build_custom_command(method="DELETE", url=url)
            result = await self._exec(cmd)
            return self._parse_custom_response(result)

        cmd = self._build_curl_command(
            method="DELETE",
            url=url,
            headers=headers,
            auth=auth,
        )
        result = await self._exec(cmd)
        return self._parse_text_response(result.stdout or "")

    async def test_ssh_connection(self) -> tuple[bool, str]:
        """Test that the SSH connection itself works.

        Failure messages contain only the exception class name — the full
        exception is logged separately so the returned message is safe to
        surface in API responses without leaking stack-trace details.
        Matches the contract of RedfishClient.test_connection().
        """
        try:
            if not self._conn:
                await self.connect()
            result = await self._exec("echo ok", timeout=10)
            if result.stdout and result.stdout.strip() == "ok":
                return True, "SSH connection successful"
            # `result.stdout` is from the SSH peer (user-controlled), so we
            # report only the byte count rather than echoing the content.
            return False, (
                f"SSH echo test returned unexpected output " f"({len(result.stdout or '')} bytes)"
            )
        except Exception as e:
            logger.warning(f"SSH test connection failed: {e}", exc_info=True)
            return False, f"SSH connection failed ({type(e).__name__})"

    async def test_redfish_access(self, base_url: str) -> tuple[bool, str]:
        """Test Redfish reachability from the proxy host.

        Failure messages contain only sanitised information (HTTP status or
        exception class) — raw curl/tool stderr is logged but not returned.
        """
        try:
            if self._use_custom_tool:
                tool = shlex.quote(self.command_template.strip())
                cmd = f"{tool} get /redfish/v1/"
                result = await self._exec(cmd, timeout=15)
                if result.exit_status == 0 and result.stdout and result.stdout.strip():
                    return True, f"SSH proxy connected; {self.command_template} reached Redfish"
                # Log the raw err for the operator; return a generic note.
                err = (result.stderr or result.stdout or "").strip()[:200]
                logger.warning(
                    "SSH custom-tool Redfish probe failed (exit=%s): %s",
                    result.exit_status,
                    err,
                )
                return False, (
                    f"SSH proxy connected but {self.command_template} failed "
                    f"(exit={result.exit_status}); see server logs"
                )
            else:
                k_flag = "" if self.verify_ssl else "k"
                cmd = f"curl -s{k_flag} -o /dev/null -w '%{{http_code}}' {shlex.quote(base_url + '/redfish/v1/')}"
                result = await self._exec(cmd, timeout=15)
                status = (result.stdout or "").strip().strip("'")
                if status == "200":
                    return True, "SSH proxy connected; Redfish reachable (HTTP 200)"
                return False, f"SSH proxy connected but Redfish returned HTTP {status}"
        except Exception as e:
            logger.warning(f"SSH-proxied Redfish test failed: {e}", exc_info=True)
            return False, f"Redfish access test failed from proxy ({type(e).__name__})"
