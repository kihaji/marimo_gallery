"""Username/password authentication with signed-cookie sessions.

Users live in a YAML file (``GALLERY_USERS_FILE``, default ``users.yaml``)
mapping usernames to PBKDF2 hashes:

    alice: pbkdf2_sha256$390000$<salt-hex>$<hash-hex>

Generate entries with ``uv run python -m gallery.passwd <username>``.

Sessions ride Starlette's SessionMiddleware (added in main.py), which also
covers WebSocket connections, so the /apps proxy can enforce login on both
transports.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

router = APIRouter()

PBKDF2_ITERATIONS = 390_000


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def load_users(path: Path) -> dict[str, str]:
    if not path.is_file():
        logger.warning("users file %s not found; all logins will fail", path)
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        users = {str(k): str(v) for k, v in raw.items()}
        logger.info("loaded %d user(s) from %s", len(users), path)
        return users
    except (yaml.YAMLError, AttributeError) as exc:
        logger.error("could not parse users file %s: %s", path, exc)
        return {}


def current_user(conn: Request | WebSocket) -> str | None:
    return conn.session.get("user")


def _safe_next(target: str | None) -> str:
    # Only same-site paths; blocks open redirects like //evil.example.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@router.get("/login")
async def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    target = _safe_next(str(form.get("next", "/")))
    encoded = request.app.state.users.get(username)
    # Hash even for unknown users so response timing doesn't leak usernames.
    valid = verify_password(password, encoded or hash_password("invalid"))
    if not encoded or not valid:
        logger.info("failed login for %r", username)
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": "Invalid username or password."},
            status_code=401,
        )
    request.session["user"] = username
    logger.info("user %r logged in", username)
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
