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
"""API service - Web UI and user interactions.

This process runs independently from the collector and handles:
- Web UI (dashboard, configuration pages)
- API endpoints (targets, logs, alerts, schemas)
- User authentication and sessions
- On-demand log collections
- Health monitoring

The API shares the SQLite database (in WAL mode) with the collector process.
"""

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .api.dependencies import app_state
from .api.routes import (
    alerts_router,
    health_router,
    logs_router,
    schemas_router,
    targets_router,
)
from .config import get_config, get_settings
from .database.repository import TargetRepository
from .log_collector import LogCollector
from .parser.schema import SchemaLoader

logger = logging.getLogger(__name__)


class UTCFormatter(logging.Formatter):
    """Custom formatter that uses UTC time for all log timestamps."""

    converter = time.gmtime  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown."""
    config = get_config()
    settings = get_settings()

    logger.info("Starting GPU Metrics Collector API Service...")
    logger.info("This process handles web UI and user interactions")

    # Initialize database repository
    repository = TargetRepository(
        database_url=settings.database_url, encryption_key=settings.encryption_key
    )
    await repository.init_db()
    app_state["repository"] = repository
    logger.info("Database connection established")

    # Initialize schema loader
    schema_loader = SchemaLoader(settings.schema_path)
    try:
        schema_loader.load()
        logger.info(f"Schema loaded from {settings.schema_path}")
    except ValueError as e:
        logger.warning(
            f"Failed to load schema from {settings.schema_path}: {e}. "
            "Using default auto-discovery configuration."
        )
    app_state["schema_loader"] = schema_loader

    # Initialize log collector for on-demand diagnostic log downloads
    log_collector = LogCollector(
        repository=repository,
        storage_dir=config.collected_logs.storage_dir,
        max_concurrent=config.collected_logs.max_concurrent_collections,
        timeout=config.polling.timeout_seconds,
        task_poll_interval=config.polling.task_poll_interval,
        task_timeout=config.collected_logs.task_timeout,
        download_timeout=config.collected_logs.download_timeout,
        collect_endpoint=config.redfish.collect_endpoint,
        collect_body=config.redfish.collect_body,
    )
    app_state["log_collector"] = log_collector
    logger.info("Log collector initialized")

    # Start retention cleanup task for collected logs
    retention_task = None
    if config.collected_logs.retention_days > 0:

        async def _log_retention_loop():
            interval = config.collected_logs.cleanup_interval_hours * 3600
            while True:
                try:
                    await asyncio.sleep(interval)
                    expired = await repository.delete_expired_logs(
                        config.collected_logs.retention_days
                    )
                    for record in expired:
                        log_collector.delete_file(record.file_path)
                    if expired:
                        logger.info(f"Log retention: removed {len(expired)} expired log(s)")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in log retention task: {e}")

        retention_task = asyncio.create_task(_log_retention_loop())
        app_state["retention_task"] = retention_task
        logger.info(
            f"Log retention task started (cleanup every "
            f"{config.collected_logs.cleanup_interval_hours}h, "
            f"keep for {config.collected_logs.retention_days} days)"
        )

    logger.info("✅ API service startup complete - ready to serve requests")

    yield

    # Shutdown
    logger.info("API service shutting down...")

    if retention_task:
        retention_task.cancel()
        from contextlib import suppress

        with suppress(asyncio.CancelledError):
            # Discarding result intentional — we just need the task to
            # finish/raise CancelledError.
            _ = await retention_task

    await repository.close()

    logger.info("API service shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from fastapi import Depends, Form, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.templating import Jinja2Templates

    from .api.auth import (
        SESSION_COOKIE_NAME,
        SESSION_MAX_AGE,
        LoginRequiredError,
        create_session_cookie,
        get_current_user,
        verify_password,
    )
    from .api.csrf import generate_csrf_token, validate_csrf_token
    from .api.dependencies import get_repository

    app = FastAPI(
        title="GPU Metrics Collector",
        description="Web UI for managing GPU metric collection",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Mount routes
    app.include_router(targets_router, prefix="/targets", tags=["Targets"])
    app.include_router(logs_router, prefix="/logs", tags=["Logs"])
    app.include_router(alerts_router, prefix="/alerts", tags=["Alerts"])
    app.include_router(schemas_router, prefix="/schemas", tags=["Schemas"])
    app.include_router(health_router, tags=["health"])

    # Templates
    import os
    from pathlib import Path

    templates_dir = Path(__file__).parent / "api" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    app.state.templates = templates

    # Add external service links as template globals (available on every page)
    settings = get_settings()
    templates.env.globals["metrics_backend"] = settings.metrics_backend
    templates.env.globals["grafana_port"] = os.environ.get("GRAFANA_PORT", "3000")
    templates.env.globals["influxdb_port"] = os.environ.get("INFLUXDB_PORT", "8086")
    templates.env.globals["prometheus_port"] = os.environ.get("PROMETHEUS_PORT", "9090")

    # Login page
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": generate_csrf_token()},
        )

    @app.post("/login")
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        validate_csrf_token(csrf_token)

        config = get_config()

        # Check username and password
        import secrets

        username_valid = secrets.compare_digest(username, config.ui.auth.username)
        password_valid = verify_password(password, config.ui.auth.password_hash)

        if not (username_valid and password_valid):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "error": "Invalid username or password",
                    "csrf_token": generate_csrf_token(),
                },
                status_code=401,
            )

        # Past this point, compare_digest has confirmed the form value matches
        # the configured admin name. Use the canonical name from config rather
        # than the form-supplied string — this both clarifies intent and stops
        # any user-controlled bytes from flowing into the session cookie.
        response = RedirectResponse(url="/", status_code=303)
        session_cookie = create_session_cookie(config.ui.auth.username)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_cookie,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(...)):
        validate_csrf_token(csrf_token)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    # Main page
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, current_user: str = Depends(get_current_user)):
        repository = get_repository()
        targets = await repository.get_all_targets()
        return templates.TemplateResponse(
            request=request,
            name="targets.html",
            context={
                "targets": targets,
                "user": current_user,
                "csrf_token": generate_csrf_token(),
            },
        )

    # Exception handlers
    @app.exception_handler(LoginRequiredError)
    async def login_required_handler(request: Request, exc: LoginRequiredError):
        return RedirectResponse(url="/login", status_code=303)

    return app


def run():
    """Entry point for API service."""
    config = get_config()
    settings = get_settings()

    # Configure logging with UTC timestamps
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    formatter = UTCFormatter(config.logging.format)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Validate required settings
    if not settings.encryption_key:
        logger.error("ENCRYPTION_KEY environment variable is required")
        sys.exit(1)

    logger.info("Starting API server on http://%s:%d", config.ui.host, config.ui.port)

    # Create app and run with uvicorn
    app = create_app()

    uvicorn.run(
        app,
        host=config.ui.host,
        port=config.ui.port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    run()
