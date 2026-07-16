from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gallery import schedule_routes
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

    registry = Registry(notebooks_dir)
    manager = ProcessManager(settings, REPO_ROOT)
    manager.sync(registry.scan())
    manager.start_reaper()

    db = Database(settings.storage_root)
    scheduler = Scheduler(settings, registry, db, Runner(settings, REPO_ROOT))
    scheduler.start()

    app.state.db = db
    app.state.known_dns = set()  # DNs already provisioned this process
    app.state.scheduler = scheduler
    app.state.settings = settings
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
# AUTH: identity is the client-certificate DN forwarded per-request by the
# TLS-terminating proxy in the GALLERY_DN_HEADER header (see gallery/auth.py
# for the trust model). Every route — index, APIs, the /apps/* proxy (HTTP
# and WS) — reads it through auth.current_user(); there are no sessions.
# /healthz stays identity-free so Kubernetes probes keep working.
# ---------------------------------------------------------------------------
if settings.dev_user_dn:
    logging.getLogger(__name__).warning(
        "GALLERY_DEV_USER_DN is set; requests without the %r header will act "
        "as %r — never enable this in production",
        settings.dn_header,
        settings.dev_user_dn,
    )

app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
app.include_router(schedule_routes.router)
app.include_router(router)
