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

from gallery.auth import MISSING_IDENTITY_HINT, Identity, identify
from gallery.manager import AppState
from gallery.proxy import proxy_http, proxy_ws
from gallery.registry import NotebookMeta

logger = logging.getLogger(__name__)

router = APIRouter()

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _authorized(meta: NotebookMeta, ident: Identity | None) -> bool:
    if meta.groups:
        return ident is not None and (ident.is_admin or bool(ident.groups.intersection(meta.groups)))
    return not meta.requires_login or ident is not None


def _denied(ident: Identity | None) -> Response:
    """401 with a hint when there is no identity at all; 404 when the user is
    authenticated but not in an allowed group (don't reveal the notebook)."""
    if ident is None:
        return Response(MISSING_IDENTITY_HINT, status_code=401)
    return Response("unknown notebook", status_code=404)


def _visible_notebooks(request: Request, ident: Identity | None) -> list[dict]:
    return [
        m.summary()
        for m in request.app.state.registry.notebooks.values()
        if _authorized(m, ident)
    ]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ident = identify(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "notebooks_json": json.dumps(_visible_notebooks(request, ident)),
            "user": ident.dn if ident else None,
            "user_name": ident.name if ident else None,
            "is_admin": ident.is_admin if ident else False,
        },
    )


@router.get("/api/apps/{slug}/status")
async def api_app_status(request: Request, slug: str):
    app = request.app.state.manager.get(slug)
    if app is None:
        return JSONResponse({"error": "unknown notebook"}, status_code=404)
    ident = identify(request)
    if not _authorized(app.meta, ident):
        code = 401 if ident is None else 404
        return JSONResponse({"error": "login required"}, status_code=code)
    return {"state": app.state.value, "error": app.start_error}


@router.get("/thumbnails/{slug}")
async def thumbnail(request: Request, slug: str):
    meta = request.app.state.registry.notebooks.get(slug)
    if meta is None or meta.thumbnail_path is None:
        return Response(status_code=404)
    ident = identify(request)
    if not _authorized(meta, ident):
        return Response(status_code=401 if ident is None else 404)
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
    ident = identify(request)
    if not _authorized(app.meta, ident):
        return _denied(ident)

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
    if not _authorized(app.meta, identify(websocket)):
        await websocket.close(code=4401)
        return
    if app.state != AppState.RUNNING:
        await manager.ensure_running(slug)
    if app.state != AppState.RUNNING:
        await websocket.close(code=1011)
        return
    await proxy_ws(websocket, manager, slug, path)
