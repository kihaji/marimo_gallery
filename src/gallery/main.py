from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gallery.config import settings
from gallery.manager import ProcessManager
from gallery.proxy import make_http_client
from gallery.registry import Registry
from gallery.routes import router

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

    app.state.settings = settings
    app.state.registry = registry
    app.state.manager = manager
    app.state.http_client = make_http_client()
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.tasks = TaskPool()
    yield
    await manager.shutdown()
    await app.state.http_client.aclose()


app = FastAPI(title="marimo gallery", lifespan=lifespan)

# ---------------------------------------------------------------------------
# AUTH SLOT: every route — index, APIs, and the /apps/* proxy (HTTP and WS) —
# flows through this single ASGI app, so gateway middleware is the one place
# to enforce authentication. For SSO, add e.g. an OIDC middleware here:
#
#   app.add_middleware(OIDCMiddleware, exempt_paths=["/healthz"])
#
# Keep /healthz exempt so Kubernetes probes keep working.
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
app.include_router(router)
