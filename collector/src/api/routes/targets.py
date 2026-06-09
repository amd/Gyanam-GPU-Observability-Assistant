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
"""Target management endpoints."""

import csv
import io
import ipaddress
import json
import logging
import re

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from ...redfish.client import RedfishClient
from ..auth import get_current_user
from ..csrf import generate_csrf_token, validate_csrf_token
from ..dependencies import get_repository

logger = logging.getLogger(__name__)

router = APIRouter()

# Collector service poll endpoint (internal docker network)
COLLECTOR_POLL_URL = "http://collector:8081/poll/{target_id}"

# Regex for valid hostname (RFC 1123)
_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63}(?<!-))*$"
)

# Blocked hosts that should not be polled
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
}

# Valid target name pattern: alphanumeric, spaces, hyphens, underscores, dots
_TARGET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,254}$")


def validate_name(name: str) -> str:
    """Validate a target display name.

    Args:
        name: The name to validate

    Returns:
        The validated and stripped name

    Raises:
        ValueError: If the name is invalid
    """
    if not name or not name.strip():
        raise ValueError("Name cannot be empty")

    name = name.strip()

    if len(name) > 255:
        raise ValueError("Name must be 255 characters or fewer")

    if not _TARGET_NAME_PATTERN.match(name):
        raise ValueError(
            "Name must start with a letter or digit and contain only "
            "letters, digits, spaces, hyphens, underscores, or dots"
        )

    return name


def validate_host(host: str) -> str:
    """Validate that the host is a valid hostname or IP address.

    Args:
        host: The host string to validate

    Returns:
        The validated host string

    Raises:
        ValueError: If the host is invalid
    """
    if not host or not host.strip():
        raise ValueError("Host cannot be empty")

    host = host.strip().lower()

    # Check blocked hosts
    if host in _BLOCKED_HOSTS:
        raise ValueError(f"Host '{host}' is not allowed (localhost/loopback addresses are blocked)")

    # Check if it's a valid IP address
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # Not an IP address, check hostname below
    else:
        # Allow private IPs (internal network) but block loopback
        if ip.is_loopback:
            raise ValueError("Loopback addresses are not allowed")
        return host

    # Validate hostname format
    if not _HOSTNAME_PATTERN.match(host):
        raise ValueError(
            f"Invalid hostname format: '{host}'. Must be a valid hostname or IP address."
        )

    return host


class TargetCreate(BaseModel):
    """Request model for creating a target."""

    name: str
    host: str
    port: int = 443
    use_ssl: bool = True
    verify_ssl: bool = False
    telemetry_endpoint: str = (
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    )
    username: str
    password: str
    token: str | None = None
    enabled: bool = True
    metric_reports_override: list | None = None
    connection_mode: str = "direct"
    sse_endpoint: str | None = None
    ssh_proxy_host: str | None = None
    ssh_proxy_port: int = 22
    ssh_proxy_username: str | None = None
    ssh_key: str | None = None
    ssh_password: str | None = None
    ssh_command_template: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name_field(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("host")
    @classmethod
    def validate_host_field(cls, v: str) -> str:
        return validate_host(v)

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v

    @field_validator("connection_mode")
    @classmethod
    def validate_connection_mode(cls, v: str) -> str:
        if v not in ("direct", "ssh_proxy", "sse"):
            raise ValueError("connection_mode must be 'direct', 'ssh_proxy', or 'sse'")
        return v

    @field_validator("ssh_proxy_host")
    @classmethod
    def validate_ssh_proxy_host(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            return validate_host(v)
        return v

    @field_validator("ssh_proxy_port")
    @classmethod
    def validate_ssh_proxy_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("SSH proxy port must be between 1 and 65535")
        return v


class TargetUpdate(BaseModel):
    """Request model for updating a target."""

    name: str | None = None
    host: str | None = None
    port: int | None = None
    use_ssl: bool | None = None
    verify_ssl: bool | None = None
    telemetry_endpoint: str | None = None
    username: str | None = None
    password: str | None = None
    token: str | None = None
    enabled: bool | None = None
    metric_reports_override: list | None = None
    connection_mode: str | None = None
    sse_endpoint: str | None = None
    ssh_proxy_host: str | None = None
    ssh_proxy_port: int | None = None
    ssh_proxy_username: str | None = None
    ssh_key: str | None = None
    ssh_password: str | None = None
    ssh_command_template: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name_field(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_name(v)
        return v

    @field_validator("host")
    @classmethod
    def validate_host_field(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_host(v)
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v

    @field_validator("connection_mode")
    @classmethod
    def validate_connection_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in ("direct", "ssh_proxy", "sse"):
            raise ValueError("connection_mode must be 'direct', 'ssh_proxy', or 'sse'")
        return v

    @field_validator("ssh_proxy_host")
    @classmethod
    def validate_ssh_proxy_host(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            return validate_host(v)
        return v

    @field_validator("ssh_proxy_port")
    @classmethod
    def validate_ssh_proxy_port(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("SSH proxy port must be between 1 and 65535")
        return v


# The 6 metric report types with their default URIs
_REPORT_TYPES = [
    ("processor", "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0"),
    ("memory", "/redfish/v1/TelemetryService/MetricReports/OAM_MemoryMetrics_0"),
    ("interconnect", "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorPortMetrics_0"),
    ("platform", "/redfish/v1/TelemetryService/MetricReports/PlatformSensorsMetrics_0"),
    ("health", "/redfish/v1/TelemetryService/MetricReports/HealthRollup"),
    ("comprehensive", "/redfish/v1/TelemetryService/MetricReports/All"),
]

# Dict version for quick lookups
_DEFAULT_REPORT_URIS = dict(_REPORT_TYPES)


def _build_metric_reports_override(**report_uris: str | None) -> list | None:
    """Build metric_reports_override list from individual URI form values.

    Compares each submitted URI against the global default.  Returns None
    when every URI matches its default (i.e. no override needed).  When at
    least one differs, returns the full list of 6 entries so the poller can
    use it as a complete replacement.

    Args:
        **report_uris: keyword args like metric_report_processor="/some/uri"

    Returns:
        Full list of 6 {"uri": ..., "report_type": ...} dicts, or None if
        all match the defaults.
    """
    entries = []
    any_changed = False
    for report_type, default_uri in _REPORT_TYPES:
        uri = (report_uris.get(f"metric_report_{report_type}") or "").strip()
        if not uri:
            uri = default_uri
        if uri != default_uri:
            any_changed = True
        entries.append({"uri": uri, "report_type": report_type})
    return entries if any_changed else None


def _build_metric_reports_map(target) -> dict:
    """Build a report_type→uri map for template pre-fill.

    Merges stored overrides on top of global defaults so every field always
    has a value.
    """
    result = dict(_DEFAULT_REPORT_URIS)
    if target and target.metric_reports_override:
        for entry in json.loads(target.metric_reports_override):
            result[entry["report_type"]] = entry["uri"]
    return result


# API Endpoints


@router.get("/api", summary="List all targets")
async def list_targets_api(user: str = Depends(get_current_user)):
    """Get all configured targets."""
    repository = get_repository()
    targets = await repository.get_all_targets()

    return [
        {
            "id": t.id,
            "name": t.name,
            "host": t.host,
            "port": t.port,
            "use_ssl": t.use_ssl,
            "telemetry_endpoint": t.telemetry_endpoint,
            "username": t.username,
            "enabled": t.enabled,
            "connection_mode": t.connection_mode,
            "ssh_proxy_host": t.ssh_proxy_host,
            "metric_reports_override": json.loads(t.metric_reports_override)
            if t.metric_reports_override
            else None,
            "last_poll_time": t.last_poll_time.isoformat() if t.last_poll_time else None,
            "last_poll_status": t.last_poll_status,
            "consecutive_failures": t.consecutive_failures,
        }
        for t in targets
    ]


@router.post("/api", summary="Create a new target")
async def create_target_api(target: TargetCreate, user: str = Depends(get_current_user)):
    """Create a new target configuration."""
    repository = get_repository()

    # Check for duplicate: use ssh_proxy_host for SSH proxy targets, host for direct
    if target.connection_mode == "ssh_proxy" and target.ssh_proxy_host:
        existing = await repository.get_target_by_ssh_proxy_host(target.ssh_proxy_host)
        if existing:
            raise HTTPException(
                status_code=400, detail="Target with this SSH proxy host already exists"
            )
    else:
        existing = await repository.get_target_by_host(target.host)
        if existing:
            raise HTTPException(status_code=400, detail="Target with this host already exists")

    new_target = await repository.create_target(
        name=target.name,
        host=target.host,
        port=target.port,
        use_ssl=target.use_ssl,
        verify_ssl=target.verify_ssl,
        telemetry_endpoint=target.telemetry_endpoint,
        username=target.username,
        password=target.password,
        token=target.token,
        enabled=target.enabled,
        metric_reports_override=target.metric_reports_override,
        connection_mode=target.connection_mode,
        sse_endpoint=target.sse_endpoint,
        ssh_proxy_host=target.ssh_proxy_host,
        ssh_proxy_port=target.ssh_proxy_port,
        ssh_proxy_username=target.ssh_proxy_username,
        ssh_key=target.ssh_key,
        ssh_password=target.ssh_password,
        ssh_command_template=target.ssh_command_template,
    )

    return {"id": new_target.id, "message": "Target created successfully"}


# CSV column definitions for export/import
_CSV_COLUMNS = [
    "name",
    "host",
    "port",
    "use_ssl",
    "verify_ssl",
    "telemetry_endpoint",
    "username",
    "password",
    "token",
    "enabled",
    "poll_interval_override",
    "tags",
    "connection_mode",
    "sse_endpoint",
    "ssh_proxy_host",
    "ssh_proxy_port",
    "ssh_proxy_username",
    "ssh_key",
    "ssh_password",
    "ssh_command_template",
]


def _csv_safe(value: str) -> str:
    """Prevent CSV formula injection by prefixing dangerous leading characters."""
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@router.get("/api/export", summary="Export targets as CSV")
async def export_targets_csv(user: str = Depends(get_current_user)):
    """Export all targets as a CSV spreadsheet.

    Credentials (password, token, ssh_key, ssh_password) are left blank
    for security — they must be re-entered on import.
    """
    repository = get_repository()
    targets = await repository.get_all_targets()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS)
    writer.writeheader()

    for t in targets:
        row = {
            "name": _csv_safe(t.name),
            "host": _csv_safe(t.host),
            "port": t.port,
            "use_ssl": t.use_ssl,
            "verify_ssl": t.verify_ssl,
            "telemetry_endpoint": _csv_safe(t.telemetry_endpoint),
            "username": _csv_safe(t.username),
            "password": "",
            "token": "",
            "enabled": t.enabled,
            "poll_interval_override": t.poll_interval_override or "",
            "tags": t.tags or "",
            "connection_mode": t.connection_mode,
            "sse_endpoint": t.sse_endpoint or "",
            "ssh_proxy_host": _csv_safe(t.ssh_proxy_host or ""),
            "ssh_proxy_port": t.ssh_proxy_port,
            "ssh_proxy_username": _csv_safe(t.ssh_proxy_username or ""),
            "ssh_key": "",
            "ssh_password": "",
            "ssh_command_template": _csv_safe(t.ssh_command_template or ""),
        }
        writer.writerow(row)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=targets_export.csv"},
    )


@router.post("/api/import", summary="Import targets from CSV")
async def import_targets_csv(
    file: UploadFile = File(...),
    user: str = Depends(get_current_user),
):
    """Import targets from a CSV spreadsheet.

    - Skips rows where the host already exists (no duplicates).
    - Password is required for new targets.
    - Returns a summary of created, skipped, and failed rows.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    # Read and decode CSV content (limit to 5MB to prevent memory exhaustion)
    max_csv_size = 5 * 1024 * 1024
    try:
        raw = await file.read()
        if len(raw) > max_csv_size:
            raise HTTPException(
                status_code=400, detail=f"CSV file too large (max {max_csv_size // 1024 // 1024}MB)"
            )
        content = raw.decode("utf-8-sig")  # utf-8-sig handles BOM from Excel
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(content))

    # Validate that required columns exist
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no header row")

    missing = {"name", "host", "username"} - set(reader.fieldnames)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {', '.join(sorted(missing))}",
        )

    repository = get_repository()

    # Pre-fetch existing identifiers for dedup
    # Direct targets dedup by host; SSH proxy targets dedup by ssh_proxy_host
    existing_targets = await repository.get_all_targets()
    existing_hosts = {t.host.lower() for t in existing_targets if t.connection_mode != "ssh_proxy"}
    existing_ssh_proxy_hosts = {
        t.ssh_proxy_host.lower()
        for t in existing_targets
        if t.connection_mode == "ssh_proxy" and t.ssh_proxy_host
    }

    created = []
    skipped = []
    errors = []

    for row_num, row in enumerate(reader, start=2):  # start=2 because row 1 is header
        row_name = row.get("name", "").strip()
        row_host = row.get("host", "").strip()
        row_conn_mode = row.get("connection_mode", "direct").strip() or "direct"
        row_ssh_host = row.get("ssh_proxy_host", "").strip()

        # Skip empty rows
        if not row_name and not row_host:
            continue

        # Check for duplicate: ssh_proxy_host for SSH proxy targets, host for direct
        if row_conn_mode == "ssh_proxy" and row_ssh_host:
            if row_ssh_host.lower() in existing_ssh_proxy_hosts:
                skipped.append(
                    {
                        "row": row_num,
                        "name": row_name,
                        "host": row_ssh_host,
                        "reason": "SSH proxy host already exists",
                    }
                )
                continue
        else:
            if row_host.lower() in existing_hosts:
                skipped.append(
                    {
                        "row": row_num,
                        "name": row_name,
                        "host": row_host,
                        "reason": "Host already exists",
                    }
                )
                continue

        try:
            # Validate and parse fields
            validated_name = validate_name(row_name)
            validated_host = validate_host(row_host)

            port = int(row.get("port", "443").strip() or "443")
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535")

            use_ssl = row.get("use_ssl", "True").strip().lower() in ("true", "1", "yes")
            verify_ssl = row.get("verify_ssl", "False").strip().lower() in ("true", "1", "yes")
            enabled = row.get("enabled", "True").strip().lower() in ("true", "1", "yes")

            telemetry_endpoint = (
                row.get("telemetry_endpoint", "").strip()
                or "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
            )

            # Connection mode (parse early — affects credential requirements)
            connection_mode = row.get("connection_mode", "direct").strip() or "direct"
            if connection_mode not in ("direct", "ssh_proxy", "sse"):
                raise ValueError(f"Invalid connection_mode: {connection_mode}")

            username = row.get("username", "").strip()
            password = row.get("password", "").strip()

            # Direct mode requires Redfish credentials; SSH proxy does not
            if connection_mode != "ssh_proxy":
                if not username:
                    raise ValueError("Username is required for direct connection mode")
                if not password:
                    raise ValueError("Password is required for direct connection mode")

            token = row.get("token", "").strip() or None

            # Optional fields
            poll_interval_raw = row.get("poll_interval_override", "").strip()
            poll_interval_override = int(poll_interval_raw) if poll_interval_raw else None

            tags_raw = row.get("tags", "").strip()
            tags = json.loads(tags_raw) if tags_raw else None

            # SSE fields
            sse_endpoint = row.get("sse_endpoint", "").strip() or None

            # SSH proxy fields
            ssh_proxy_host = row.get("ssh_proxy_host", "").strip() or None
            ssh_proxy_port = int(row.get("ssh_proxy_port", "22").strip() or "22")
            if not 1 <= ssh_proxy_port <= 65535:
                raise ValueError("SSH proxy port must be between 1 and 65535")
            ssh_proxy_username = row.get("ssh_proxy_username", "").strip() or None
            ssh_key = row.get("ssh_key", "").strip() or None
            ssh_password = row.get("ssh_password", "").strip() or None
            ssh_command_template = row.get("ssh_command_template", "").strip() or None

            if connection_mode == "ssh_proxy":
                if not ssh_proxy_host:
                    raise ValueError("SSH proxy host is required for ssh_proxy mode")
                ssh_proxy_host = validate_host(ssh_proxy_host)
                if not ssh_proxy_username:
                    raise ValueError("SSH proxy username is required for ssh_proxy mode")
                if not ssh_key and not ssh_password:
                    raise ValueError("SSH key or password is required for ssh_proxy mode")

            await repository.create_target(
                name=validated_name,
                host=validated_host,
                port=port,
                use_ssl=use_ssl,
                verify_ssl=verify_ssl,
                telemetry_endpoint=telemetry_endpoint,
                username=username or "",
                password=password or "",
                token=token,
                enabled=enabled,
                poll_interval_override=poll_interval_override,
                tags=tags,
                connection_mode=connection_mode,
                sse_endpoint=sse_endpoint,
                ssh_proxy_host=ssh_proxy_host,
                ssh_proxy_port=ssh_proxy_port,
                ssh_proxy_username=ssh_proxy_username,
                ssh_key=ssh_key,
                ssh_password=ssh_password,
                ssh_command_template=ssh_command_template,
            )

            created.append({"row": row_num, "name": validated_name, "host": validated_host})
            # Track for within-file dedup
            if connection_mode == "ssh_proxy" and ssh_proxy_host:
                existing_ssh_proxy_hosts.add(ssh_proxy_host.lower())
            else:
                existing_hosts.add(validated_host.lower())

        except Exception as e:
            # Log full exception with stack trace server-side. Surface a
            # bounded form to the UI: type name + first line of message,
            # truncated. Bulk-import errors are almost always validation
            # messages ("invalid IP", "duplicate host") that operators
            # need to see to fix their CSV — type-name alone would force
            # them to dig through server logs for every row.
            logger.warning(f"Bulk-create row {row_num} failed: {e}", exc_info=True)
            # Take first line only and cap length; strip control chars
            # so the message can't break the JSON or log lines.
            first_line = str(e).split("\n", 1)[0]
            safe_msg = "".join(c for c in first_line if c.isprintable())[:200]
            errors.append(
                {
                    "row": row_num,
                    "name": row_name,
                    "host": row_host,
                    "error": f"{type(e).__name__}: {safe_msg}",
                }
            )

    return {
        "created": len(created),
        "skipped": len(skipped),
        "errors": len(errors),
        "details": {
            "created": created,
            "skipped": skipped,
            "errors": errors,
        },
    }


@router.get("/api/{target_id}", summary="Get a specific target")
async def get_target_api(target_id: int, user: str = Depends(get_current_user)):
    """Get a specific target by ID."""
    repository = get_repository()
    target = await repository.get_target(target_id)

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    return {
        "id": target.id,
        "name": target.name,
        "host": target.host,
        "port": target.port,
        "use_ssl": target.use_ssl,
        "telemetry_endpoint": target.telemetry_endpoint,
        "username": target.username,
        "enabled": target.enabled,
        "connection_mode": target.connection_mode,
        "ssh_proxy_host": target.ssh_proxy_host,
        "ssh_proxy_port": target.ssh_proxy_port,
        "ssh_proxy_username": target.ssh_proxy_username,
        "ssh_command_template": target.ssh_command_template,
        "metric_reports_override": json.loads(target.metric_reports_override)
        if target.metric_reports_override
        else None,
        "last_poll_time": target.last_poll_time.isoformat() if target.last_poll_time else None,
        "last_poll_status": target.last_poll_status,
        "last_error_message": target.last_error_message,
        "consecutive_failures": target.consecutive_failures,
    }


@router.put("/api/{target_id}", summary="Update a target")
async def update_target_api(
    target_id: int, update: TargetUpdate, user: str = Depends(get_current_user)
):
    """Update a target configuration."""
    repository = get_repository()

    update_data = update.model_dump(exclude_unset=True)
    target = await repository.update_target(target_id, **update_data)

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    return {"message": "Target updated successfully"}


@router.delete("/api/{target_id}", summary="Delete a target")
async def delete_target_api(target_id: int, user: str = Depends(get_current_user)):
    """Delete a target configuration."""
    repository = get_repository()
    deleted = await repository.delete_target(target_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Target not found")

    return {"message": "Target deleted successfully"}


@router.post("/api/{target_id}/test", summary="Test connection to a target")
async def test_target_connection(target_id: int, user: str = Depends(get_current_user)):
    """Test the connection to a target."""
    repository = get_repository()
    target = await repository.get_target(target_id)

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    password = repository.decrypt_password(target)
    token = repository.decrypt_token(target)

    ssh_transport = None
    if target.connection_mode == "ssh_proxy":
        from ...redfish.ssh_transport import SSHTransport

        ssh_key = repository.decrypt_ssh_key(target)
        ssh_password = repository.decrypt_ssh_password(target)
        ssh_transport = SSHTransport(
            proxy_host=target.ssh_proxy_host,
            proxy_port=target.ssh_proxy_port or 22,
            proxy_username=target.ssh_proxy_username or "root",
            ssh_key=ssh_key,
            ssh_password=ssh_password,
            verify_ssl=target.verify_ssl,
            command_template=target.ssh_command_template,
        )

    async with RedfishClient(
        base_url=target.base_url,
        username=target.username,
        password=password,
        token=token,
        timeout=30,
        verify_ssl=target.verify_ssl,
        ssh_transport=ssh_transport,
    ) as client:
        success, message = await client.test_connection()

    # Sanitise: strip newlines from any embedded exception text so it can't
    # corrupt the JSON response or log lines on the client side.
    safe_message = message.replace("\n", " ").replace("\r", " ")[:500]
    return {"success": success, "message": safe_message}


@router.post("/api/{target_id}/poll", summary="Trigger immediate poll")
async def trigger_poll(target_id: int, user: str = Depends(get_current_user)):
    """Trigger an immediate poll of a target by forwarding to collector service."""
    try:
        # Forward the poll request to the collector service
        async with httpx.AsyncClient(timeout=60.0) as client:  # Longer timeout for polling
            # target_id is an int (FastAPI path-param coercion), so the
            # formatted URL cannot be hijacked — the int is the only value
            # that flows into the path segment.
            url = COLLECTOR_POLL_URL.format(target_id=int(target_id))
            response = await client.post(url)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Target not found")
            elif response.status_code == 503:
                raise HTTPException(status_code=503, detail="Collector service not ready")
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Collector returned error: {response.text}",
                )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504, detail="Poll request timed out - target may be unreachable"
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach collector service: {str(e)}")


# HTML UI Endpoints


@router.get("", response_class=HTMLResponse)
async def targets_page(request: Request, user: str = Depends(get_current_user)):
    """Render the targets management page."""
    repository = get_repository()
    targets = await repository.get_all_targets()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="targets.html",
        context={"targets": targets, "user": user, "csrf_token": generate_csrf_token()},
    )


@router.get("/add", response_class=HTMLResponse)
async def add_target_page(request: Request, user: str = Depends(get_current_user)):
    """Render the add target form."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="add_target.html",
        context={
            "target": None,
            "user": user,
            "csrf_token": generate_csrf_token(),
            "metric_reports_map": dict(_DEFAULT_REPORT_URIS),
        },
    )


@router.get("/{target_id}/edit", response_class=HTMLResponse)
async def edit_target_page(request: Request, target_id: int, user: str = Depends(get_current_user)):
    """Render the edit target form."""
    repository = get_repository()
    target = await repository.get_target(target_id)

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    metric_reports_map = _build_metric_reports_map(target)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="add_target.html",
        context={
            "target": target,
            "user": user,
            "csrf_token": generate_csrf_token(),
            "metric_reports_map": metric_reports_map,
        },
    )


@router.post("/add", response_class=HTMLResponse)
async def add_target_form(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(443),
    use_ssl: str = Form("true"),
    verify_ssl: str | None = Form(None),
    telemetry_endpoint: str = Form(
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    ),
    username: str | None = Form(None),
    password: str | None = Form(None),
    token: str | None = Form(None),
    enabled: str | None = Form(None),
    connection_mode: str = Form("direct"),
    sse_endpoint: str | None = Form(None),
    ssh_proxy_host: str | None = Form(None),
    ssh_proxy_port: int = Form(22),
    ssh_proxy_username: str | None = Form(None),
    ssh_key: str | None = Form(None),
    ssh_password: str | None = Form(None),
    ssh_command_template: str | None = Form(None),
    metric_report_processor: str | None = Form(None),
    metric_report_memory: str | None = Form(None),
    metric_report_interconnect: str | None = Form(None),
    metric_report_platform: str | None = Form(None),
    metric_report_health: str | None = Form(None),
    metric_report_comprehensive: str | None = Form(None),
    enable_alert_subscription: str | None = Form(None),
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Handle add target form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()

    try:
        # Validate inputs
        validated_name = validate_name(name)
        validated_host = validate_host(host)

        # Validate port
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")

        # Convert form values to proper types
        use_ssl_bool = use_ssl.lower() == "true"
        verify_ssl_bool = verify_ssl is not None
        enabled_bool = enabled is not None
        enable_alert_subscription_bool = enable_alert_subscription is not None

        # Validate mode-specific fields
        validated_ssh_host = None
        if connection_mode == "ssh_proxy":
            if not ssh_proxy_host or not ssh_proxy_host.strip():
                raise ValueError("SSH proxy host is required for SSH Proxy mode")
            validated_ssh_host = validate_host(ssh_proxy_host)
            if not ssh_proxy_username or not ssh_proxy_username.strip():
                raise ValueError("SSH proxy username is required for SSH Proxy mode")
            if not ssh_key and not ssh_password:
                raise ValueError(
                    "Either SSH private key or SSH password is required for SSH Proxy mode"
                )
        else:
            # Direct mode requires Redfish credentials
            if not username or not username.strip():
                raise ValueError("Username is required for Direct connection mode")
            if not password:
                raise ValueError("Password is required for Direct connection mode")

        reports_override = _build_metric_reports_override(
            metric_report_processor=metric_report_processor,
            metric_report_memory=metric_report_memory,
            metric_report_interconnect=metric_report_interconnect,
            metric_report_platform=metric_report_platform,
            metric_report_health=metric_report_health,
            metric_report_comprehensive=metric_report_comprehensive,
        )

        await repository.create_target(
            name=validated_name,
            host=validated_host,
            port=port,
            use_ssl=use_ssl_bool,
            verify_ssl=verify_ssl_bool,
            telemetry_endpoint=telemetry_endpoint,
            username=username.strip() if username else "",
            password=password or "",
            token=token if token else None,
            enabled=enabled_bool,
            enable_alert_subscription=enable_alert_subscription_bool,
            metric_reports_override=reports_override,
            connection_mode=connection_mode,
            sse_endpoint=sse_endpoint.strip() if sse_endpoint else None,
            ssh_proxy_host=validated_ssh_host,
            ssh_proxy_port=ssh_proxy_port,
            ssh_proxy_username=ssh_proxy_username.strip() if ssh_proxy_username else None,
            ssh_key=ssh_key.strip() if ssh_key else None,
            ssh_password=ssh_password if ssh_password else None,
            ssh_command_template=ssh_command_template.strip() if ssh_command_template else None,
        )
        return RedirectResponse(url="/targets", status_code=303)
    except Exception as e:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request=request,
            name="add_target.html",
            context={
                "target": None,
                "error": str(e),
                "user": user,
                "csrf_token": generate_csrf_token(),
                "metric_reports_map": dict(_DEFAULT_REPORT_URIS),
            },
        )


@router.post("/{target_id}/edit", response_class=HTMLResponse)
async def edit_target_form(
    request: Request,
    target_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(443),
    use_ssl: str = Form("true"),
    verify_ssl: str | None = Form(None),
    telemetry_endpoint: str = Form(
        "/redfish/v1/Systems/UBB/LogServices/DiagLogs/Actions/LogService.CollectDiagnosticData"
    ),
    username: str | None = Form(None),
    password: str | None = Form(None),
    token: str | None = Form(None),
    enabled: str | None = Form(None),
    connection_mode: str = Form("direct"),
    sse_endpoint: str | None = Form(None),
    ssh_proxy_host: str | None = Form(None),
    ssh_proxy_port: int = Form(22),
    ssh_proxy_username: str | None = Form(None),
    ssh_key: str | None = Form(None),
    ssh_password: str | None = Form(None),
    ssh_command_template: str | None = Form(None),
    metric_report_processor: str | None = Form(None),
    metric_report_memory: str | None = Form(None),
    metric_report_interconnect: str | None = Form(None),
    metric_report_platform: str | None = Form(None),
    metric_report_health: str | None = Form(None),
    metric_report_comprehensive: str | None = Form(None),
    enable_alert_subscription: str | None = Form(None),
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Handle edit target form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()

    try:
        # Validate inputs
        validated_name = validate_name(name)
        validated_host = validate_host(host)

        # Validate port
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")

        # Validate SSH proxy fields
        validated_ssh_host = None
        if connection_mode == "ssh_proxy":
            if not ssh_proxy_host or not ssh_proxy_host.strip():
                raise ValueError("SSH proxy host is required for SSH Proxy mode")
            validated_ssh_host = validate_host(ssh_proxy_host)
            if not ssh_proxy_username or not ssh_proxy_username.strip():
                raise ValueError("SSH proxy username is required for SSH Proxy mode")
            # On edit, key/password may already be stored — only require if neither is provided
            # and no existing credentials exist
            existing = await repository.get_target(target_id)
            has_existing_ssh_creds = existing and (
                existing.encrypted_ssh_key or existing.encrypted_ssh_password
            )
            if not ssh_key and not ssh_password and not has_existing_ssh_creds:
                raise ValueError(
                    "Either SSH private key or SSH password is required for SSH Proxy mode"
                )
        else:
            # Direct mode requires Redfish credentials
            if not username or not username.strip():
                raise ValueError("Username is required for Direct connection mode")
            # On edit, password may already be stored — only require if no real password exists
            if not password:
                existing = await repository.get_target(target_id)
                has_real_password = (
                    existing
                    and existing.encrypted_password
                    and repository.decrypt_password(existing)
                )
                if not has_real_password:
                    raise ValueError("Password is required for Direct connection mode")

        reports_override = _build_metric_reports_override(
            metric_report_processor=metric_report_processor,
            metric_report_memory=metric_report_memory,
            metric_report_interconnect=metric_report_interconnect,
            metric_report_platform=metric_report_platform,
            metric_report_health=metric_report_health,
            metric_report_comprehensive=metric_report_comprehensive,
        )

        # Build update data
        update_data = {
            "name": validated_name,
            "host": validated_host,
            "port": port,
            "use_ssl": use_ssl.lower() == "true",
            "verify_ssl": verify_ssl is not None,
            "telemetry_endpoint": telemetry_endpoint,
            "username": username.strip() if username else "",
            "enabled": enabled is not None,
            "enable_alert_subscription": enable_alert_subscription is not None,
            "metric_reports_override": reports_override,
            "connection_mode": connection_mode,
            "sse_endpoint": sse_endpoint.strip() if sse_endpoint else None,
            "ssh_proxy_host": validated_ssh_host,
            "ssh_proxy_port": ssh_proxy_port,
            "ssh_proxy_username": ssh_proxy_username.strip() if ssh_proxy_username else None,
            "ssh_command_template": ssh_command_template.strip() if ssh_command_template else None,
        }

        # Only update password if provided
        if password:
            update_data["password"] = password

        # Only update token if provided
        if token:
            update_data["token"] = token

        # SSH credentials: update if provided, or clear if switching to direct mode
        if connection_mode == "direct":
            # Clear stored SSH credentials when switching away from SSH proxy
            update_data["ssh_key"] = ""
            update_data["ssh_password"] = ""
        else:
            # Only update SSH credentials if provided (leave existing if blank)
            if ssh_key:
                update_data["ssh_key"] = ssh_key.strip()
            if ssh_password:
                update_data["ssh_password"] = ssh_password

        await repository.update_target(target_id, **update_data)
        return RedirectResponse(url="/targets", status_code=303)
    except Exception as e:
        target = await repository.get_target(target_id)
        metric_reports_map = _build_metric_reports_map(target)
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request=request,
            name="add_target.html",
            context={
                "target": target,
                "error": str(e),
                "user": user,
                "csrf_token": generate_csrf_token(),
                "metric_reports_map": metric_reports_map,
            },
        )


@router.post("/{target_id}/delete")
async def delete_target_form(
    target_id: int, csrf_token: str = Form(...), user: str = Depends(get_current_user)
):
    """Handle delete target form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()
    deleted = await repository.delete_target(target_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Target not found")
    return RedirectResponse(url="/targets", status_code=303)
