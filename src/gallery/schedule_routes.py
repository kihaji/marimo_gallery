"""Routes for notebook schedules, run-now, and run artifacts.

Viewing follows the notebook's requires_login flag (same rule as the app
proxy); managing schedules always requires login.
"""

from __future__ import annotations

import json
import logging
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from gallery.auth import current_user
from gallery.registry import NotebookMeta
from gallery.scheduler import (
    ValidationFailure,
    compile_cadence,
    describe_cron,
    next_run_utc,
    validate_cron,
    validate_params,
)

logger = logging.getLogger(__name__)

router = APIRouter()

RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _authorized(meta: NotebookMeta, user: str | None) -> bool:
    return not meta.requires_login or user is not None


def _meta_or_none(request: Request, slug: str) -> NotebookMeta | None:
    return request.app.state.registry.notebooks.get(slug)


def _login_required() -> JSONResponse:
    return JSONResponse({"error": "login required"}, status_code=401)


def _schedule_view(request: Request, schedule: dict) -> dict:
    return schedule | {"cadence_label": describe_cron(schedule["cron"])}


def _payload(request: Request, meta: NotebookMeta) -> dict:
    db = request.app.state.db
    return {
        "slug": meta.slug,
        "title": meta.title,
        "parameters": [p.model_dump() for p in meta.parameters],
        "schedules": [_schedule_view(request, s) for s in db.list_schedules(meta.slug)],
        "runs": db.list_runs(meta.slug),
        "active": db.has_active_runs(meta.slug),
    }


@router.get("/schedules/{slug}", response_class=HTMLResponse)
async def schedules_page(request: Request, slug: str):
    meta = _meta_or_none(request, slug)
    if meta is None:
        return Response("unknown notebook", status_code=404)
    user = current_user(request)
    if not _authorized(meta, user):
        return RedirectResponse(f"/login?next=/schedules/{slug}", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "meta": meta,
            "user": user,
            "tz_name": time.strftime("%Z"),
            "payload_json": json.dumps(_payload(request, meta) | {"user": user}),
        },
    )


@router.get("/api/schedules/{slug}")
async def api_list(request: Request, slug: str):
    meta = _meta_or_none(request, slug)
    if meta is None:
        return JSONResponse({"error": "unknown notebook"}, status_code=404)
    if not _authorized(meta, current_user(request)):
        return _login_required()
    return _payload(request, meta)


@router.post("/api/schedules/{slug}")
async def api_create(request: Request, slug: str):
    meta = _meta_or_none(request, slug)
    if meta is None:
        return JSONResponse({"error": "unknown notebook"}, status_code=404)
    user = current_user(request)
    if user is None:
        return _login_required()
    body = await request.json()
    try:
        if body.get("cron"):
            cron = validate_cron(str(body["cron"]))
        else:
            cron = compile_cadence(body.get("cadence") or {})
        params = validate_params(meta.parameters, body.get("params") or {})
        name = str(body.get("name") or "").strip()[:100]
        if not name:
            raise ValidationFailure("schedule needs a name")
    except ValidationFailure as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    schedule = request.app.state.db.create_schedule(
        slug, name, cron, params, user, next_run_utc(cron)
    )
    logger.info("[%s] schedule %s (%s) created by %s", slug, schedule["id"], cron, user)
    return _schedule_view(request, schedule)


@router.patch("/api/schedules/{slug}/{schedule_id}")
async def api_update(request: Request, slug: str, schedule_id: int):
    meta = _meta_or_none(request, slug)
    db = request.app.state.db
    schedule = db.get_schedule(schedule_id)
    if meta is None or schedule is None or schedule["slug"] != slug:
        return JSONResponse({"error": "unknown schedule"}, status_code=404)
    if current_user(request) is None:
        return _login_required()
    body = await request.json()
    if "enabled" in body:
        enabled = bool(body["enabled"])
        db.set_enabled(schedule_id, enabled, next_run_utc(schedule["cron"]) if enabled else None)
    return _schedule_view(request, db.get_schedule(schedule_id))


@router.delete("/api/schedules/{slug}/{schedule_id}")
async def api_delete(request: Request, slug: str, schedule_id: int):
    meta = _meta_or_none(request, slug)
    db = request.app.state.db
    schedule = db.get_schedule(schedule_id)
    if meta is None or schedule is None or schedule["slug"] != slug:
        return JSONResponse({"error": "unknown schedule"}, status_code=404)
    user = current_user(request)
    if user is None:
        return _login_required()
    db.delete_schedule(schedule_id)
    logger.info("[%s] schedule %s deleted by %s", slug, schedule_id, user)
    return {"deleted": schedule_id}


@router.post("/api/schedules/{slug}/run")
async def api_run_now(request: Request, slug: str):
    meta = _meta_or_none(request, slug)
    if meta is None:
        return JSONResponse({"error": "unknown notebook"}, status_code=404)
    user = current_user(request)
    if user is None:
        return _login_required()
    body = await request.json()
    try:
        params = validate_params(meta.parameters, body.get("params") or {})
    except ValidationFailure as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return request.app.state.scheduler.run_now(meta, params, user)


def _artifact_response(request: Request, slug: str, run_id: str, filename: str, media_type=None):
    meta = _meta_or_none(request, slug)
    if meta is None or not RUN_ID_RE.match(run_id):
        return Response(status_code=404)
    if not _authorized(meta, current_user(request)):
        return _login_required()
    run = request.app.state.db.get_run(run_id)
    if run is None or run["slug"] != slug:
        return Response(status_code=404)
    path = request.app.state.scheduler.runner.run_dir(slug, run_id) / filename
    if not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type=media_type)


@router.get("/runs/{slug}/{run_id}/report")
async def run_report(request: Request, slug: str, run_id: str):
    return _artifact_response(request, slug, run_id, "report.html")


@router.get("/runs/{slug}/{run_id}/log")
async def run_log(request: Request, slug: str, run_id: str):
    return _artifact_response(request, slug, run_id, "run.log", media_type="text/plain")
