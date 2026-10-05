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
"""Redfish API client for GPU UBB8 telemetry collection.

Implements the Task-based asynchronous workflow for collecting diagnostic data:
1. POST to initiate data collection (returns Task ID)
2. Poll task status until complete
3. Download attachment blob from completed task
4. Delete completed task (optional cleanup)
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum

import httpx

from .http_client import make_bmc_client

logger = logging.getLogger(__name__)


class TaskState(Enum):
    """Redfish Task states."""

    NEW = "New"
    STARTING = "Starting"
    RUNNING = "Running"
    SUSPENDED = "Suspended"
    INTERRUPTED = "Interrupted"
    PENDING = "Pending"
    STOPPING = "Stopping"
    COMPLETED = "Completed"
    KILLED = "Killed"
    EXCEPTION = "Exception"
    SERVICE = "Service"
    CANCELLING = "Cancelling"
    CANCELLED = "Cancelled"


@dataclass
class TaskStatus:
    """Status of a Redfish task."""

    task_id: str
    task_uri: str
    state: TaskState
    percent_complete: int
    message: str
    # Location of the result (used for downloading attachment)
    result_location: str | None = None


@dataclass
class RedfishResponse:
    """Response from a Redfish API call."""

    success: bool
    status_code: int
    content: bytes
    content_type: str
    error_message: str | None = None


class RedfishClient:
    """Async client for Redfish API communication.

    Implements GPU UBB8 Task-based telemetry collection workflow:
    1. POST to initiate diagnostic data collection
    2. Poll task until complete
    3. Download blob from task attachment
    """

    # Default endpoint for GPU UBB8 diagnostic data collection
    DEFAULT_COLLECT_ENDPOINT = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )

    # Default request body for collecting all logs
    DEFAULT_COLLECT_BODY = {"DiagnosticDataType": "OEM", "OEMDiagnosticDataType": "AllLogs"}

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        token: str | None = None,
        timeout: int = 30,
        verify_ssl: bool = False,
        task_poll_interval: int = 5,
        task_timeout: int = 300,
        download_timeout: int = 300,
        cleanup_task_on_success: bool = True,
    ):
        """Initialize the Redfish client.

        Args:
            base_url: Base URL of the Redfish service (e.g., https://bmc.example.com)
            username: Authentication username
            password: Authentication password
            token: Optional authentication token (overrides username/password)
            timeout: HTTP request timeout in seconds
            verify_ssl: Whether to verify SSL certificates
            task_poll_interval: Seconds between task status polls
            task_timeout: Maximum seconds to wait for task completion
            download_timeout: Timeout for large file downloads in seconds
            cleanup_task_on_success: Delete task after successful download (default: True)
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.task_poll_interval = task_poll_interval
        self.task_timeout = task_timeout
        self.download_timeout = download_timeout
        self.cleanup_task_on_success = cleanup_task_on_success

        self._session_token: str | None = None
        self._client: httpx.AsyncClient | None = None
        # Status/message of the most recent CollectDiagnosticData initiation,
        # so callers can distinguish "action not found" (404 -> rediscover the
        # endpoint) from other failures without re-parsing logs.
        self._last_initiate_status: int = 0
        self._last_initiate_error: str | None = None

    async def __aenter__(self) -> "RedfishClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Establish connection and authenticate."""
        # Bound the per-client connection pool. One cached client exists per
        # target (hundreds at fleet scale); with httpx's default pool (up to 100
        # connections each) the aggregate socket count can exhaust the process
        # file-descriptor limit. A target needs only a handful of parallel GETs
        # (the 6 metric reports), so a small cap is ample.
        self._client = make_bmc_client(
            verify_ssl=self.verify_ssl,
            timeout=self.timeout,
        )

        # If we have a pre-configured token, use it
        if self.token:
            self._session_token = self.token
            return

        # Otherwise, create a session
        await self._create_session()

    async def _create_session(self) -> None:
        """Create a Redfish session for authentication."""
        if not self._is_connected():
            raise RuntimeError("Client not connected")

        session_url = f"{self.base_url}/redfish/v1/SessionService/Sessions"

        try:
            response = await self._http_post(
                session_url,
                headers={"Content-Type": "application/json"},
                json_body={"UserName": self.username, "Password": self.password},
            )

            if response.status_code in (200, 201):
                # Extract session token from header
                self._session_token = response.headers.get("X-Auth-Token")
                if self._session_token:
                    logger.info("Redfish session created successfully")
                else:
                    logger.warning("No X-Auth-Token in session response, using basic auth")
            else:
                logger.warning(
                    f"Session creation failed with status {response.status_code}, "
                    "falling back to basic auth"
                )

        except (httpx.RequestError, OSError, TimeoutError) as e:
            logger.warning(f"Session creation failed: {e}, falling back to basic auth")

    def _get_auth_headers(self) -> dict[str, str]:
        """Get authentication headers for requests."""
        headers = {
            "Accept": "application/json, application/octet-stream, */*",
            "Content-Type": "application/json",
        }

        if self._session_token:
            headers["X-Auth-Token"] = self._session_token

        return headers

    def _get_auth(self) -> httpx.BasicAuth | None:
        """Get basic auth if no session token."""
        if not self._session_token:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def _is_connected(self) -> bool:
        """Check if the HTTP client is connected."""
        return self._client is not None

    async def _http_get(
        self, url: str, headers: dict = None, timeout: float = None, binary: bool = False
    ):
        """HTTP GET request. ``binary`` is accepted for signature compatibility."""
        return await self._client.get(
            url,
            headers=headers or self._get_auth_headers(),
            auth=self._get_auth(),
            # When no per-request timeout is given, fall back to the client's
            # configured timeout (set in make_bmc_client). Passing None here would
            # instead DISABLE all timeouts (httpx treats None as "no timeout"),
            # letting a wedged BMC hang a poll forever and permanently strand the
            # target in poller._inflight.
            timeout=httpx.Timeout(timeout) if timeout else httpx.USE_CLIENT_DEFAULT,
        )

    async def _http_post(self, url: str, headers: dict = None, json_body: dict = None):
        """HTTP POST request."""
        return await self._client.post(
            url,
            json=json_body,
            headers=headers or {},
            auth=self._get_auth(),
        )

    async def _http_delete(self, url: str, headers: dict = None):
        """HTTP DELETE request."""
        return await self._client.delete(url, headers=headers or {}, auth=self._get_auth())

    async def collect_diagnostic_data(
        self, collect_endpoint: str | None = None, collect_body: dict | None = None
    ) -> RedfishResponse:
        """Collect diagnostic data using Task-based workflow.

        This is the main method for GPU UBB8 telemetry collection:
        1. POST to initiate collection (creates a task)
        2. Poll task until complete
        3. Download the attachment blob

        Args:
            collect_endpoint: Override the default collection endpoint
            collect_body: Override the default request body

        Returns:
            RedfishResponse with the downloaded blob content
        """
        if not self._is_connected():
            raise RuntimeError("Client not connected. Call connect() first.")

        endpoint = collect_endpoint or self.DEFAULT_COLLECT_ENDPOINT
        body = collect_body or self.DEFAULT_COLLECT_BODY

        # Step 1: Initiate diagnostic data collection
        logger.info(f"Initiating diagnostic data collection at {endpoint}")
        task_uri = await self._initiate_collection(endpoint, body)

        if not task_uri:
            # A 202 with no parseable task URI means the BMC *did* create a task
            # we can't address to delete — it will leak in TaskService. We can't
            # clean it up without the URI, but surface it loudly so operators can
            # GC (a BMC whose task list fills up rejects new collections).
            if self._last_initiate_status == 202:
                logger.error(
                    "CollectDiagnosticData returned 202 but no task URI (Location "
                    "header and body both unusable) at %s — the created task cannot "
                    "be addressed for deletion and may leak in TaskService.",
                    endpoint,
                )
            return RedfishResponse(
                success=False,
                status_code=self._last_initiate_status,
                content=b"",
                content_type="",
                error_message=(
                    self._last_initiate_error or "Failed to initiate data collection task"
                ),
            )

        logger.info(f"Task created: {task_uri}")

        # Once the task exists on the BMC it MUST be deleted on every exit path,
        # not just on success. A timed-out or failed task that is never deleted
        # stays registered in the BMC's TaskService; because each poll creates a
        # new task, a repeatedly-failing target leaks tasks until the BMC rejects
        # new CollectDiagnosticData requests — stalling collection fleet-wide.
        try:
            # Step 2: Poll task until complete
            task_status = await self._wait_for_task(task_uri)

            if not task_status:
                return RedfishResponse(
                    success=False,
                    status_code=0,
                    content=b"",
                    content_type="",
                    error_message="Task polling failed or timed out",
                )

            if task_status.state != TaskState.COMPLETED:
                return RedfishResponse(
                    success=False,
                    status_code=0,
                    content=b"",
                    content_type="",
                    error_message=f"Task failed with state: {task_status.state.value} - {task_status.message}",
                )

            logger.info(f"Task completed: {task_status.task_id}")

            # Step 3: Download the attachment
            attachment_uri = self._get_attachment_uri(task_status)

            if not attachment_uri:
                return RedfishResponse(
                    success=False,
                    status_code=0,
                    content=b"",
                    content_type="",
                    error_message="Could not determine attachment URI from task",
                )

            logger.info(f"Downloading attachment from {attachment_uri}")
            response = await self._download_attachment(attachment_uri)
            return response
        finally:
            # Step 4: Always clean up the task (success, failure, or timeout).
            if self.cleanup_task_on_success:
                try:
                    await self._delete_task(task_uri)
                except Exception as e:  # noqa: BLE001 — cleanup must not mask the result
                    logger.warning(f"Failed to delete task {task_uri}: {e}")

    async def _get_json_resource(self, uri: str) -> dict | None:
        """GET a Redfish resource and return parsed JSON, or None on any error.

        Best-effort helper for discovery walks: a missing resource, non-200
        status, or non-JSON body all degrade to None rather than raising.
        """
        url = f"{self.base_url}{uri}" if uri.startswith("/") else uri
        try:
            resp = await self._http_get(url)
            if resp.status_code != 200:
                return None
            return resp.json()  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001 — discovery must never raise
            return None

    async def discover_collect_endpoint(self) -> str | None:
        """Discover this target's CollectDiagnosticData action URI.

        The action lives on a LogService, but *where* varies by hardware and
        firmware (e.g. ``Systems/UBB/LogServices/DiagLogs`` vs
        ``Managers/Instinct_AMC/LogServices/Dump``). Rather than assume a
        hardcoded path, walk the LogServices under both ``Systems`` and
        ``Managers`` and return the first member advertising
        ``#LogService.CollectDiagnosticData``.

        Returns the action ``target`` URI, or None if none is found. Entirely
        best-effort — every fetch degrades to a skip.
        """
        action_key = "#LogService.CollectDiagnosticData"
        for root in ("/redfish/v1/Systems", "/redfish/v1/Managers"):
            root_doc = await self._get_json_resource(root)
            if not root_doc:
                continue
            for member in root_doc.get("Members", []):
                member_id = member.get("@odata.id")
                if not member_id:
                    continue
                node = await self._get_json_resource(member_id)
                log_services = (node or {}).get("LogServices", {}).get("@odata.id")
                if not log_services:
                    continue
                ls_doc = await self._get_json_resource(log_services)
                if not ls_doc:
                    continue
                for ls_member in ls_doc.get("Members", []):
                    ls_id = ls_member.get("@odata.id")
                    if not ls_id:
                        continue
                    svc = await self._get_json_resource(ls_id)
                    target = (svc or {}).get("Actions", {}).get(action_key, {}).get("target")
                    if target:
                        logger.info(f"Discovered CollectDiagnosticData action at {target}")
                        return target  # type: ignore[no-any-return]
        return None

    async def _initiate_collection(self, endpoint: str, body: dict) -> str | None:
        """Initiate diagnostic data collection.

        Args:
            endpoint: The collection action endpoint
            body: Request body with collection parameters

        Returns:
            Task URI if successful, None otherwise
        """
        url = f"{self.base_url}{endpoint}"
        self._last_initiate_status = 0
        self._last_initiate_error = None

        try:
            response = await self._http_post(
                url,
                headers=self._get_auth_headers(),
                json_body=body,
            )
            self._last_initiate_status = response.status_code

            # 202 Accepted indicates task was created
            if response.status_code == 202:
                # Task URI can be in Location header or response body
                task_uri = response.headers.get("Location")

                if not task_uri:
                    # Try to get from response body
                    try:
                        data = response.json()
                        task_uri = data.get("@odata.id") or data.get("Id")
                        if task_uri and not task_uri.startswith("/"):
                            task_uri = f"/redfish/v1/TaskService/Tasks/{task_uri}"
                    except Exception:
                        # Body isn't JSON or doesn't contain the expected
                        # fields — leave task_uri as None; the caller treats
                        # None as failure.
                        pass

                return task_uri  # type: ignore[no-any-return]

            elif response.status_code in (200, 201):
                # Some implementations might return success directly
                # Try to get task info from response
                try:
                    data = response.json()
                    return data.get("@odata.id")  # type: ignore[no-any-return]
                except Exception:
                    # Body isn't JSON — accept the 200/201 as a no-task-id
                    # response and let the caller treat it as completion.
                    pass
                return None

            else:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_body = response.json()
                    if "error" in error_body:
                        error_msg = error_body["error"].get("message", error_msg)
                except Exception:
                    # Body isn't JSON — keep the status-code-only error_msg.
                    pass
                self._last_initiate_error = error_msg
                logger.error(f"Failed to initiate collection: {error_msg}")
                return None

        except (httpx.RequestError, OSError, TimeoutError) as e:
            logger.error(f"Request error initiating collection: {e}")
            return None

    async def _wait_for_task(self, task_uri: str) -> TaskStatus | None:
        """Poll task status until complete or timeout.

        Retries on transient network errors to avoid abandoning long-running
        tasks due to momentary connectivity issues.

        Args:
            task_uri: URI of the task to monitor

        Returns:
            Final TaskStatus if successful, None on error
        """
        import time

        url = f"{self.base_url}{task_uri}" if task_uri.startswith("/") else task_uri
        start_time = time.monotonic()
        max_consecutive_errors = 3
        consecutive_errors = 0

        while (time.monotonic() - start_time) < self.task_timeout:
            try:
                response = await self._http_get(
                    url,
                    headers=self._get_auth_headers(),
                )

                if response.status_code != 200:
                    logger.error(f"Task status check failed: HTTP {response.status_code}")
                    return None

                # Reset error counter on successful request
                consecutive_errors = 0

                data = response.json()

                # Parse task status
                task_state_str = data.get("TaskState", "Unknown")
                try:
                    task_state = TaskState(task_state_str)
                except ValueError:
                    logger.warning(f"Unknown task state: {task_state_str}")
                    task_state = TaskState.RUNNING

                percent_complete = data.get("PercentComplete", 0)

                # Get message from TaskStatus or Messages array
                message = ""
                if "TaskStatus" in data:
                    message = data["TaskStatus"]
                elif "Messages" in data and len(data["Messages"]) > 0:
                    message = data["Messages"][0].get("Message", "")

                # Get result location - check multiple possible fields
                result_location = None

                # Check Payload.HttpHeaders for Location
                payload = data.get("Payload", {})
                http_headers = payload.get("HttpHeaders", [])
                for header in http_headers:
                    if isinstance(header, str) and header.lower().startswith("location:"):
                        result_location = header.split(":", 1)[1].strip()
                        break
                    elif isinstance(header, dict) and "Location" in header:
                        result_location = header["Location"]
                        break

                # Check TaskMonitor or direct location
                if not result_location:
                    result_location = data.get("TaskMonitor") or data.get("@odata.id")

                task_status = TaskStatus(
                    task_id=data.get("Id", ""),
                    task_uri=task_uri,
                    state=task_state,
                    percent_complete=percent_complete,
                    message=message,
                    result_location=result_location,
                )

                logger.debug(
                    f"Task {task_status.task_id}: {task_state.value} "
                    f"({percent_complete}%) - {message}"
                )

                # Check if task is in a terminal state
                if task_state in (
                    TaskState.COMPLETED,
                    TaskState.KILLED,
                    TaskState.EXCEPTION,
                    TaskState.CANCELLED,
                ):
                    return task_status

                # Wait before next poll
                await asyncio.sleep(self.task_poll_interval)

            except (httpx.RequestError, OSError, TimeoutError) as e:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"Task polling failed after {max_consecutive_errors} "
                        f"consecutive errors, last: {e}"
                    )
                    return None
                logger.warning(
                    f"Transient error polling task status ({consecutive_errors}/"
                    f"{max_consecutive_errors}): {e}, retrying..."
                )
                await asyncio.sleep(self.task_poll_interval)
            except Exception as e:
                logger.error(f"Error parsing task status: {e}")
                return None

        logger.error(f"Task timed out after {self.task_timeout} seconds")
        return None

    def _get_attachment_uri(self, task_status: TaskStatus) -> str | None:
        """Determine the attachment download URI from task status.

        Args:
            task_status: Completed task status

        Returns:
            URI to download the attachment
        """
        # Try result_location + /attachment
        if task_status.result_location:
            location = task_status.result_location
            if not location.endswith("/attachment"):
                location = f"{location}/attachment"
            return location

        # Fall back to task_uri + /attachment
        return f"{task_status.task_uri}/attachment"

    async def _download_attachment(self, attachment_uri: str) -> RedfishResponse:
        """Download the attachment blob.

        Args:
            attachment_uri: URI to the attachment

        Returns:
            RedfishResponse with the blob content
        """
        url = (
            f"{self.base_url}{attachment_uri}" if attachment_uri.startswith("/") else attachment_uri
        )

        try:
            download_headers = {
                "Accept": "application/octet-stream, application/zip, application/gzip, */*",
            }
            if self._session_token:
                download_headers["X-Auth-Token"] = self._session_token

            response = await self._http_get(
                url,
                headers=download_headers,
                timeout=float(self.download_timeout),
                binary=False,
            )

            content_type = response.headers.get("Content-Type", "application/octet-stream")

            if response.status_code == 200:
                logger.info(f"Downloaded {len(response.content)} bytes")
                return RedfishResponse(
                    success=True,
                    status_code=response.status_code,
                    content=response.content,
                    content_type=content_type,
                )
            else:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_body = response.json()
                    if "error" in error_body:
                        error_msg = error_body["error"].get("message", error_msg)
                except Exception:
                    error_msg = response.text[:200] if response.text else error_msg

                return RedfishResponse(
                    success=False,
                    status_code=response.status_code,
                    content=b"",
                    content_type=content_type,
                    error_message=error_msg,
                )

        except (httpx.TimeoutException, TimeoutError):
            return RedfishResponse(
                success=False,
                status_code=0,
                content=b"",
                content_type="",
                error_message="Attachment download timed out",
            )
        except (httpx.RequestError, OSError) as e:
            return RedfishResponse(
                success=False,
                status_code=0,
                content=b"",
                content_type="",
                error_message=f"Attachment download failed: {e}",
            )

    async def get_metric_report(self, report_uri: str) -> RedfishResponse:
        """Fetch a metric report directly via GET.

        This is the fast path: a single authenticated GET that returns JSON
        immediately, with no task/blob/unpack overhead.

        Args:
            report_uri: URI path, e.g. /redfish/v1/TelemetryService/MetricReports/All

        Returns:
            RedfishResponse with JSON content on success, or success=False on any error.
        """
        if not self._is_connected():
            return RedfishResponse(
                success=False,
                status_code=0,
                content=b"",
                content_type="",
                error_message="Client not connected",
            )

        url = f"{self.base_url}{report_uri}" if report_uri.startswith("/") else report_uri

        try:
            response = await self._http_get(
                url,
                headers=self._get_auth_headers(),
            )

            content_type = response.headers.get("Content-Type", "")

            if response.status_code == 200 and "json" in content_type.lower():
                return RedfishResponse(
                    success=True,
                    status_code=200,
                    content=response.content,
                    content_type=content_type,
                )

            error_msg = f"HTTP {response.status_code}"
            if response.status_code != 200:
                try:
                    body = response.json()
                    if "error" in body:
                        error_msg = body["error"].get("message", error_msg)
                except Exception:
                    # Body isn't JSON — keep the status-code-only error_msg.
                    pass
            else:
                error_msg = f"Unexpected content-type: {content_type}"

            return RedfishResponse(
                success=False,
                status_code=response.status_code,
                content=b"",
                content_type=content_type,
                error_message=error_msg,
            )

        except (httpx.TimeoutException, TimeoutError):
            return RedfishResponse(
                success=False,
                status_code=0,
                content=b"",
                content_type="",
                error_message="Metric report GET timed out",
            )
        except (httpx.RequestError, OSError) as e:
            return RedfishResponse(
                success=False,
                status_code=0,
                content=b"",
                content_type="",
                error_message=f"Metric report GET failed: {e}",
            )

    # Legacy method for backward compatibility
    async def get_telemetry(self, endpoint: str) -> RedfishResponse:
        """Fetch telemetry data - now uses Task-based workflow.

        For GPU UBB8, this calls collect_diagnostic_data().
        The endpoint parameter is used as the collection action endpoint.

        Args:
            endpoint: Redfish endpoint path for the collection action

        Returns:
            RedfishResponse with the blob content
        """
        return await self.collect_diagnostic_data(collect_endpoint=endpoint)

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to the Redfish service.

        Returns:
            Tuple of (success, message). Failure messages contain only the
            exception class name — the full exception is logged separately
            so the message can be safely surfaced to API responses without
            leaking stack-trace details.
        """
        if not self._client:
            try:
                await self.connect()
            except Exception as e:
                logger.warning(f"Test-connection connect failed: {e}", exc_info=True)
                return False, f"Connection failed ({type(e).__name__})"

        if not self._client:
            return False, "Failed to establish connection"

        try:
            response = await self._client.get(
                f"{self.base_url}/redfish/v1/",
                headers=self._get_auth_headers(),
                auth=self._get_auth(),
            )

            if response.status_code == 200:
                data = response.json()
                product = data.get("Product", "Unknown")
                vendor = data.get("Vendor", "Unknown")
                version = data.get("RedfishVersion", "Unknown")
                return True, f"Connected to {vendor} {product} (Redfish v{version})"
            else:
                return False, f"Connection failed: HTTP {response.status_code}"

        except httpx.RequestError as e:
            logger.warning(f"Test-connection request failed: {e}", exc_info=True)
            return False, f"Connection failed ({type(e).__name__})"

    async def _delete_task(self, task_uri: str) -> bool:
        """Delete a completed task to clean up task history.

        Args:
            task_uri: URI of the task to delete

        Returns:
            True if deletion successful or not needed, False on error
        """
        if not self._is_connected():
            return False

        url = f"{self.base_url}{task_uri}" if task_uri.startswith("/") else task_uri

        try:
            response = await self._http_delete(
                url,
                headers=self._get_auth_headers(),
            )

            if response.status_code in (200, 202, 204):
                logger.info(f"Task deleted successfully: {task_uri}")
                return True
            elif response.status_code == 404:
                # Task already deleted or doesn't exist - not an error
                logger.debug(f"Task not found (already deleted): {task_uri}")
                return True
            elif response.status_code == 405:
                # Method not allowed - some Redfish implementations don't support DELETE
                logger.warning("Task deletion not supported by this Redfish implementation")
                return True
            else:
                logger.warning(f"Failed to delete task {task_uri}: HTTP {response.status_code}")
                return False

        except (httpx.RequestError, OSError, TimeoutError) as e:
            logger.warning(f"Error deleting task {task_uri}: {e}")
            return False

    async def close(self) -> None:
        """Close the client connection."""
        if self._client:
            # Optionally delete the session
            if self._session_token and not self.token:
                # Best effort session cleanup
                with suppress(Exception):
                    await self._client.delete(
                        f"{self.base_url}/redfish/v1/SessionService/Sessions/Self",
                        headers={"X-Auth-Token": self._session_token},
                    )

            await self._client.aclose()
            self._client = None
            self._session_token = None
