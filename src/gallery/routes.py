from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from gallery.auth import current_user
from gallery.manager import AppState
from gallery.proxy import proxy_http, proxy_ws
from gallery.registry import NotebookMeta

logger = logging.getLogger(__name__)

router = APIRouter()

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _authorized(meta: NotebookMeta, user: str | None) -> bool:
    return not meta.requires_login or user is not None


def _visible_notebooks(request: Request, user: str | None) -> list[dict]:
    return [
        m.summary()
        for m in request.app.state.registry.notebooks.values()
        if _authorized(m, user)
    ]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = current_user(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"notebooks_json": json.dumps(_visible_notebooks(request, user)), "user": user},
    )


@router.get("/api/apps/{slug}/status")
async def api_app_status(request: Request, slug: str):
    app = request.app.state.manager.get(slug)
    if app is None:
        return JSONResponse({"error": "unknown notebook"}, status_code=404)
    if not _authorized(app.meta, current_user(request)):
        return JSONResponse({"error": "login required"}, status_code=401)
    return {"state": app.state.value, "error": app.start_error}


@router.get("/thumbnails/{slug}")
async def thumbnail(request: Request, slug: str):
    meta = request.app.state.registry.notebooks.get(slug)
    if meta is None or meta.thumbnail_path is None:
        return Response(status_code=404)
    if not _authorized(meta, current_user(request)):
        return Response(status_code=401)
    return FileResponse(meta.thumbnail_path)


@router.get("/healthz")
async def healthz(request: Request):
    manager = request.app.state.manager
    return {
        "status": "ok",
        "notebooks": len(manager.apps),
        "running": sum(1 for a in manager.apps.values() if a.state == AppState.RUNNING),
    }


@router.get("/apps/{slug}")
async def app_no_slash(slug: str):
    return RedirectResponse(f"/apps/{slug}/", status_code=307)


@router.api_route("/apps/{slug}/{path:path}", methods=PROXY_METHODS)
async def app_http(request: Request, slug: str, path: str):
    manager = request.app.state.manager
    app = manager.get(slug)
    if app is None:
        return Response("unknown notebook", status_code=404)

    is_page_load = (
        request.method == "GET"
        and path in ("", "/")
        and "text/html" in request.headers.get("accept", "")
    )
    if not _authorized(app.meta, current_user(request)):
        if is_page_load:
            return RedirectResponse(f"/login?next=/apps/{slug}/", status_code=303)
        return Response("login required", status_code=401)

    if app.state != AppState.RUNNING:
        if is_page_load:
            # Show a friendly starting page and boot the backend behind it.
            if not app.start_lock.locked():
                request.app.state.tasks.spawn(manager.ensure_running(slug))
            return request.app.state.templates.TemplateResponse(
                request,
                "starting.html",
                {"slug": slug, "title": app.meta.title, "sandbox": app.meta.sandbox},
            )
        await manager.ensure_running(slug)
        if app.state != AppState.RUNNING:
            return Response(f"notebook failed to start: {app.start_error}", status_code=502)

    return await proxy_http(
        request,
        request.app.state.http_client,
        manager,
        slug,
        path,
        request.app.state.settings.max_upload_bytes,
    )


@router.websocket("/apps/{slug}/{path:path}")
async def app_ws(websocket: WebSocket, slug: str, path: str):
    manager = websocket.app.state.manager
    app = manager.get(slug)
    if app is None:
        await websocket.close(code=4404)
        return
    if not _authorized(app.meta, current_user(websocket)):
        await websocket.close(code=4401)
        return
    if app.state != AppState.RUNNING:
        await manager.ensure_running(slug)
    if app.state != AppState.RUNNING:
        await websocket.close(code=1011)
        return
    await proxy_ws(websocket, manager, slug, path)
