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
"""Schema management and metric discovery endpoints."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..auth import get_current_user
from ..csrf import generate_csrf_token
from ..dependencies import get_schema_loader

router = APIRouter()


@router.get("/api", summary="List all schemas")
async def list_schemas_api(user: str = Depends(get_current_user)):
    """Get all configured metric schemas."""
    schema_loader = get_schema_loader()
    schemas = schema_loader.get_schemas()

    return [
        {
            "name": s.name,
            "description": s.description,
            "path_pattern": s.path_pattern,
            "fields": [
                {
                    "json_key": f.json_key,
                    "metric_name": f.metric_name,
                    "type": f.type,
                    "unit": f.unit,
                }
                for f in s.fields
            ],
            "tags": [{"json_key": t.json_key, "tag_name": t.tag_name} for t in s.tags_from],
        }
        for s in schemas
    ]


@router.get("/api/auto-discovery", summary="Get auto-discovery config")
async def get_auto_discovery_config(user: str = Depends(get_current_user)):
    """Get the auto-discovery configuration."""
    schema_loader = get_schema_loader()
    config = schema_loader.get_auto_discovery_config()

    return {
        "enabled": config.enabled,
        "include_patterns": config.include_patterns,
        "exclude_patterns": config.exclude_patterns,
        "default_type": config.default_type,
    }


@router.post("/api/reload", summary="Reload schemas from disk")
async def reload_schemas(user: str = Depends(get_current_user)):
    """Reload the metric schemas from the configuration file."""
    schema_loader = get_schema_loader()
    schema_loader.reload()
    schemas = schema_loader.get_schemas()

    return {"message": "Schemas reloaded successfully", "count": len(schemas)}


# HTML UI Endpoint


@router.get("", response_class=HTMLResponse)
async def schemas_page(request: Request, user: str = Depends(get_current_user)):
    """Render the schemas viewer page."""
    schema_loader = get_schema_loader()
    schemas = schema_loader.get_schemas()
    auto_discovery = schema_loader.get_auto_discovery_config()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="schemas.html",
        context={
            "schemas": schemas,
            "auto_discovery": auto_discovery,
            "user": user,
            "csrf_token": generate_csrf_token(),
        },
    )
