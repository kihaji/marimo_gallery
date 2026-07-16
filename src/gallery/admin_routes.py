"""Admin pages: manage groups, membership, and admin flags.

Users appear here automatically after their first request (identity is
auto-provisioned from the DN header). Every route 404s for non-admins so the
page's existence isn't advertised.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from gallery.auth import Identity, identify

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(request: Request) -> Identity | None:
    ident = identify(request)
    return ident if ident is not None and ident.is_admin else None


def _payload(request: Request) -> dict:
    db = request.app.state.db
    return {"users": db.list_users(), "groups": db.list_groups()}


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    ident = _admin(request)
    if ident is None:
        return Response("not found", status_code=404)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": ident.dn,
            "user_name": ident.name,
            "payload_json": json.dumps(_payload(request) | {"you": ident.dn}),
        },
    )


@router.get("/api/admin")
async def api_state(request: Request):
    ident = _admin(request)
    if ident is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _payload(request)


@router.post("/api/admin/groups")
async def api_create_group(request: Request):
    ident = _admin(request)
    if ident is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    name = str(body.get("name") or "").strip()[:100]
    if not name:
        return JSONResponse({"error": "group needs a name"}, status_code=422)
    group = request.app.state.db.create_group(name)
    if group is None:
        return JSONResponse({"error": f"group {name!r} already exists"}, status_code=422)
    logger.info("group %r created by %s", name, ident.dn)
    return group


@router.delete("/api/admin/groups/{group_id}")
async def api_delete_group(request: Request, group_id: int):
    ident = _admin(request)
    if ident is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    request.app.state.db.delete_group(group_id)
    logger.info("group %s deleted by %s", group_id, ident.dn)
    return {"deleted": group_id}


@router.post("/api/admin/membership")
async def api_membership(request: Request):
    ident = _admin(request)
    if ident is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    try:
        user_id, group_id = int(body["user_id"]), int(body["group_id"])
        member = bool(body["member"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "user_id, group_id, member required"}, status_code=422)
    request.app.state.db.set_membership(user_id, group_id, member)
    return _payload(request)


@router.post("/api/admin/users/{user_id}/admin")
async def api_set_admin(request: Request, user_id: int):
    ident = _admin(request)
    if ident is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    is_admin = bool(body.get("is_admin"))
    target = request.app.state.db.get_user(user_id)
    if target is None:
        return JSONResponse({"error": "unknown user"}, status_code=404)
    if target["dn"] == ident.dn and not is_admin:
        # Guard against locking everyone out of this page.
        return JSONResponse({"error": "you cannot remove your own admin"}, status_code=422)
    request.app.state.db.set_admin(user_id, is_admin)
    logger.info("user %s admin=%s by %s", user_id, is_admin, ident.dn)
    return _payload(request)
