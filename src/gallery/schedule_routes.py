"""Routes for notebook schedules, run-now, and run artifacts.

Schedules are private to their creator: users only see, edit, delete, and
run-now their own, and run history/reports are likewise owner-only (schedule
parameters and report contents may be sensitive). Everything here therefore
requires login, regardless of the notebook's requires_login flag.
"""

from __future__ import annotations

import json
import logging
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from gallery.auth import MISSING_IDENTITY_HINT, Identity, identify
from gallery.registry import NotebookMeta
from gallery.routes import _authorized
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


def _meta_or_none(request: Request, slug: str) -> NotebookMeta | None:
    return request.app.state.registry.notebooks.get(slug)


def _login_required() -> JSONResponse:
    return JSONResponse({"error": "login required"}, status_code=401)


def _gate(
    request: Request, slug: str
) -> tuple[NotebookMeta | None, Identity | None, JSONResponse | None]:
    """(meta, identity, error): scheduling a notebook requires the same group
    access as opening it — unknown and unauthorized slugs both 404."""
    meta = _meta_or_none(request, slug)
    if meta is None:
        return None, None, JSONResponse({"error": "unknown notebook"}, status_code=404)
    ident = identify(request)
    if ident is None:
        return meta, None, _login_required()
    if not _authorized(meta, ident):
        return meta, ident, JSONResponse({"error": "unknown notebook"}, status_code=404)
    return meta, ident, None


def _schedule_view(request: Request, schedule: dict) -> dict:
    return schedule | {"cadence_label": describe_cron(schedule["cron"])}


def _payload(request: Request, meta: NotebookMeta, ident: Identity) -> dict:
    """Filtered to what the user may see: their own schedules and runs, plus
    ones shared (view-only) with a group they belong to."""
    db = request.app.state.db
    runs = db.list_runs_visible(meta.slug, ident.dn)
    return {
        "slug": meta.slug,
        "title": meta.title,
        "parameters": [p.model_dump() for p in meta.parameters],
        "schedules": [
            _schedule_view(request, s) for s in db.list_schedules_visible(meta.slug, ident.dn)
        ],
        "runs": runs,
        "active": any(r["status"] in ("queued", "running") for r in runs),
        # Share targets: only groups the owner belongs to.
        "my_groups": [g for g in db.list_groups() if g["name"] in ident.groups],
    }


@router.get("/schedules/{slug}", response_class=HTMLResponse)
async def schedules_page(request: Request, slug: str):
    meta, ident, error = _gate(request, slug)
    if error is not None:
        if ident is None and meta is not None:
            return Response(MISSING_IDENTITY_HINT, status_code=401)
        return Response("unknown notebook", status_code=error.status_code)
    user = ident.dn
    return request.app.state.templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "meta": meta,
            "user": user,
            "user_name": ident.name,
            "tz_name": time.strftime("%Z"),
            "payload_json": json.dumps(_payload(request, meta, ident) | {"user": user}),
        },
    )


@router.get("/api/schedules/{slug}")
async def api_list(request: Request, slug: str):
    meta, ident, error = _gate(request, slug)
    if error is not None:
        return error
    return _payload(request, meta, ident)


@router.post("/api/schedules/{slug}")
async def api_create(request: Request, slug: str):
    meta, ident, error = _gate(request, slug)
    if error is not None:
        return error
    user = ident.dn
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


def _owned_schedule(request: Request, slug: str, schedule_id: int, user: str | None):
    """The schedule, only if it exists for this slug AND belongs to the user.
    Others' schedules 404 rather than 403 so their existence isn't leaked."""
    schedule = request.app.state.db.get_schedule(schedule_id)
    if schedule is None or schedule["slug"] != slug or schedule["created_by"] != user:
        return None
    return schedule


@router.patch("/api/schedules/{slug}/{schedule_id}")
async def api_update(request: Request, slug: str, schedule_id: int):
    _, ident, error = _gate(request, slug)
    if error is not None:
        return error
    db = request.app.state.db
    schedule = _owned_schedule(request, slug, schedule_id, ident.dn)
    if schedule is None:
        return JSONResponse({"error": "unknown schedule"}, status_code=404)
    body = await request.json()
    if "enabled" in body:
        enabled = bool(body["enabled"])
        db.set_enabled(schedule_id, enabled, next_run_utc(schedule["cron"]) if enabled else None)
    if "shared_group_id" in body:
        group_id = body["shared_group_id"]
        if group_id is not None:
            names = {g["id"]: g["name"] for g in db.list_groups()}
            if (
                isinstance(group_id, bool)
                or not isinstance(group_id, int)
                or names.get(group_id) not in ident.groups
            ):
                return JSONResponse(
                    {"error": "you can only share with a group you belong to"},
                    status_code=422,
                )
        db.set_shared_group(schedule_id, group_id)
        logger.info(
            "[%s] schedule %s sharing set to group %s by %s",
            slug,
            schedule_id,
            group_id,
            ident.dn,
        )
    updated = next(
        s for s in db.list_schedules_visible(slug, ident.dn) if s["id"] == schedule_id
    )
    return _schedule_view(request, updated)


@router.delete("/api/schedules/{slug}/{schedule_id}")
async def api_delete(request: Request, slug: str, schedule_id: int):
    _, ident, error = _gate(request, slug)
    if error is not None:
        return error
    schedule = _owned_schedule(request, slug, schedule_id, ident.dn)
    if schedule is None:
        return JSONResponse({"error": "unknown schedule"}, status_code=404)
    request.app.state.db.delete_schedule(schedule_id)
    logger.info("[%s] schedule %s deleted by %s", slug, schedule_id, ident.dn)
    return {"deleted": schedule_id}


@router.post("/api/schedules/{slug}/run")
async def api_run_now(request: Request, slug: str):
    meta, ident, error = _gate(request, slug)
    if error is not None:
        return error
    body = await request.json()
    try:
        params = validate_params(meta.parameters, body.get("params") or {})
    except ValidationFailure as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return request.app.state.scheduler.run_now(meta, params, ident.dn)


def _artifact_response(request: Request, slug: str, run_id: str, filename: str, media_type=None):
    _, ident, error = _gate(request, slug)
    if error is not None:
        return error if ident is None else Response(status_code=404)
    if not RUN_ID_RE.match(run_id):
        return Response(status_code=404)
    db = request.app.state.db
    run = db.get_run(run_id)
    # Run parameters and report contents may be sensitive: owner-only, unless
    # the run's schedule is currently shared with a group the user is in.
    if run is None or run["slug"] != slug or not db.run_accessible(run_id, ident.dn):
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
