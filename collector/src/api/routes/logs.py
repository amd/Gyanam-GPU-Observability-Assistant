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
"""Collected diagnostic logs endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..auth import get_current_user
from ..csrf import generate_csrf_token, validate_csrf_token
from ..dependencies import get_log_collector, get_repository

logger = logging.getLogger(__name__)

router = APIRouter()


# ---- JSON API ----


@router.get("/api", summary="List all collected logs")
async def list_logs_api(user: str = Depends(get_current_user)):
    """Get all collected log records."""
    repository = get_repository()
    logs = await repository.get_all_collected_logs()
    return [
        {
            "id": log.id,
            "target_id": log.target_id,
            "target_name": log.target_name,
            "target_host": log.target_host,
            "filename": log.filename,
            "file_size_bytes": log.file_size_bytes,
            "status": log.status,
            "error_message": log.error_message,
            "duration_ms": log.duration_ms,
            "collected_at": log.collected_at.isoformat() if log.collected_at else None,
        }
        for log in logs
    ]


@router.post("/api/{target_id}/collect", summary="Collect logs from a target")
async def collect_logs_api(
    target_id: int,
    user: str = Depends(get_current_user),
):
    """Trigger diagnostic log collection from a single target."""
    collector = get_log_collector()
    result = await collector.collect_single(target_id)
    if not result.get("success"):
        # collect_single() now returns only the exception-type name in
        # `result["error"]` (raw `str(e)` is logged server-side only —
        # see log_collector.py). Safe to surface, no stack trace leakage.
        logger.warning(
            "Log collection failed for target %d: %s",
            int(target_id),
            result.get("error", "Collection failed"),
        )
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Collection failed"),
        )
    # result is the dict from log_collector._collect_from_target which is
    # known-clean (no raw exception text in either branch — sanitised at the
    # source). Return it verbatim so API consumers see target_id, log_id,
    # filename, file_size_bytes, duration_ms as documented.
    return result


@router.post("/api/collect-all", summary="Collect logs from all targets")
async def collect_all_logs_api(user: str = Depends(get_current_user)):
    """Trigger diagnostic log collection from all enabled targets."""
    collector = get_log_collector()
    result = await collector.collect_all()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Bulk collection failed"))
    return result


@router.get("/api/{log_id}/download", summary="Download a collected log file")
async def download_log_api(
    log_id: int,
    user: str = Depends(get_current_user),
):
    """Download a collected log file."""
    repository = get_repository()
    log = await repository.get_collected_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    if log.status != "completed":
        raise HTTPException(status_code=400, detail=f"Log is not ready (status: {log.status})")

    file_path = Path(log.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=log.filename,
        media_type="application/gzip",
    )


@router.delete("/api/{log_id}", summary="Delete a collected log")
async def delete_log_api(
    log_id: int,
    user: str = Depends(get_current_user),
):
    """Delete a collected log record and its file."""
    repository = get_repository()
    collector = get_log_collector()

    log = await repository.delete_collected_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    collector.delete_file(log.file_path)
    return {"message": "Log deleted successfully"}


# ---- HTML UI ----


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, user: str = Depends(get_current_user)):
    """Render the collected logs page."""
    repository = get_repository()
    logs = await repository.get_all_collected_logs()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="collected_logs.html",
        context={
            "logs": logs,
            "user": user,
            "csrf_token": generate_csrf_token(),
        },
    )


@router.post("/{log_id}/delete")
async def delete_log_form(
    log_id: int,
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Handle delete log form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()
    collector = get_log_collector()

    log = await repository.delete_collected_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    collector.delete_file(log.file_path)
    return RedirectResponse(url="/logs", status_code=303)
