from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import secrets

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from gallery import auth, schedule_routes
from gallery.config import settings
from gallery.db import Database
from gallery.manager import ProcessManager
from gallery.proxy import make_http_client
from gallery.registry import Registry
from gallery.routes import router
from gallery.scheduler import Runner, Scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]


class TaskPool:
    """Keeps references to fire-and-forget tasks so they aren't GC'd."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    notebooks_dir = settings.notebooks_dir
    if not notebooks_dir.is_absolute():
        notebooks_dir = REPO_ROOT / notebooks_dir
    users_file = settings.users_file
    if not users_file.is_absolute():
        users_file = REPO_ROOT / users_file

    registry = Registry(notebooks_dir)
    manager = ProcessManager(settings, REPO_ROOT)
    manager.sync(registry.scan())
    manager.start_reaper()

    db = Database(settings.storage_root)
    scheduler = Scheduler(settings, registry, db, Runner(settings, REPO_ROOT))
    scheduler.start()

    app.state.db = db
    app.state.scheduler = scheduler
    app.state.settings = settings
    app.state.users = auth.load_users(users_file)
    app.state.registry = registry
    app.state.manager = manager
    app.state.http_client = make_http_client()
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.tasks = TaskPool()
    yield
    await scheduler.shutdown()
    await manager.shutdown()
    await app.state.http_client.aclose()
    db.close()


app = FastAPI(title="marimo gallery", lifespan=lifespan)

# ---------------------------------------------------------------------------
# AUTH: every route — index, APIs, and the /apps/* proxy (HTTP and WS) —
# flows through this single ASGI app, so middleware here covers everything.
# SessionMiddleware signs the login cookie and also applies to WebSocket
# connections. To swap username/password for SSO later, replace the /login
# routes in gallery/auth.py with an OIDC flow that sets the same
# session["user"] key; the per-notebook checks in routes.py stay unchanged.
# Keep /healthz unauthenticated so Kubernetes probes keep working.
# ---------------------------------------------------------------------------
if settings.secret_key is None:
    logging.getLogger(__name__).warning(
        "GALLERY_SECRET_KEY not set; using a random key — logins will not "
        "survive gateway restarts"
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or secrets.token_hex(32),
    max_age=settings.session_max_age_seconds,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
app.include_router(auth.router)
app.include_router(schedule_routes.router)
app.include_router(router)
