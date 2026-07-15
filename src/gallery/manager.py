"""Spawn, supervise, and reap one `marimo run` subprocess per notebook."""

from __future__ import annotations

import asyncio
import collections
import enum
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

import httpx

from gallery.config import Settings
from gallery.registry import NotebookMeta
from gallery_shared import storage

logger = logging.getLogger(__name__)


class AppState(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


class ManagedApp:
    def __init__(self, meta: NotebookMeta) -> None:
        self.meta = meta
        self.state = AppState.STOPPED
        self.port: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.last_activity = time.monotonic()
        self.active_websockets = 0
        self.start_lock = asyncio.Lock()
        self.start_error: str | None = None
        self.recent_output: collections.deque[str] = collections.deque(maxlen=30)
        self._tasks: set[asyncio.Task] = set()

    @property
    def slug(self) -> str:
        return self.meta.slug

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class ProcessManager:
    def __init__(self, settings: Settings, repo_root: Path) -> None:
        self.settings = settings
        self.repo_root = repo_root
        self.apps: dict[str, ManagedApp] = {}
        self._reaper_task: asyncio.Task | None = None
        self._closing = False
        self._http = httpx.AsyncClient(timeout=2.0)

    # -- registry sync ------------------------------------------------------

    def sync(self, notebooks: dict[str, NotebookMeta]) -> None:
        """Reconcile managed apps with a fresh registry scan."""
        for slug in list(self.apps):
            if slug not in notebooks:
                logger.info("[%s] removed from registry; stopping", slug)
                asyncio.get_running_loop().create_task(self.stop(slug))
                del self.apps[slug]
        for slug, meta in notebooks.items():
            existing = self.apps.get(slug)
            if existing is None:
                self.apps[slug] = ManagedApp(meta)
            else:
                existing.meta = meta
        # NOTE: a changed app.py takes effect on the next (re)start; idle
        # reaping recycles processes naturally.

    def get(self, slug: str) -> ManagedApp | None:
        return self.apps.get(slug)

    # -- backend resolution --------------------------------------------------
    # The proxy resolves backends only through this method. To scale out on
    # Kubernetes with one Deployment per notebook, replace its body with a
    # lookup of the notebook's Service DNS (e.g. http://nb-<slug>:2718) and
    # skip spawning entirely — the rest of the gateway is unchanged.

    def base_url(self, slug: str) -> str:
        app = self.apps[slug]
        return f"http://127.0.0.1:{app.port}"

    # -- lifecycle -----------------------------------------------------------

    async def ensure_running(self, slug: str) -> ManagedApp:
        app = self.apps[slug]
        async with app.start_lock:
            if app.state == AppState.RUNNING or self._closing:
                return app
            await self._start(app)
            return app

    async def _start(self, app: ManagedApp) -> None:
        app.state = AppState.STARTING
        app.start_error = None
        app.recent_output.clear()
        for attempt in range(3):
            port = self._allocate_port()
            proc = await self._spawn(app, port)
            app.port, app.proc = port, proc
            self._pump_output(app, proc)
            if await self._wait_ready(app, proc, port):
                app.state = AppState.RUNNING
                app.touch()
                self._supervise(app, proc)
                logger.info("[%s] running on port %d (pid %d)", app.slug, port, proc.pid)
                return
            if proc.returncode is None:
                # Alive but never became healthy: give up rather than retry.
                await self._kill(proc)
                break
            logger.warning(
                "[%s] exited during startup (attempt %d/3, code %s)",
                app.slug,
                attempt + 1,
                proc.returncode,
            )
        app.state = AppState.FAILED
        app.port, app.proc = None, None
        app.start_error = app.start_error or "\n".join(app.recent_output) or "startup failed"
        logger.error("[%s] failed to start: %s", app.slug, app.start_error)

    async def _spawn(self, app: ManagedApp, port: int) -> asyncio.subprocess.Process:
        meta = app.meta
        session_ttl = meta.session_ttl or self.settings.marimo_session_ttl
        argv = [
            sys.executable,
            "-m",
            "marimo",
            "run",
            str(meta.app_path.resolve()),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--headless",
            "--base-url",
            f"/apps/{app.slug}",
            "--session-ttl",
            str(session_ttl),
        ]
        if meta.sandbox:
            argv.append("--sandbox")
        if meta.include_code:
            argv.append("--include-code")
        env = os.environ.copy()
        env["MARIMO_GALLERY_APP"] = app.slug
        env["GALLERY_STORAGE_ROOT"] = str(self.settings.storage_root.resolve())
        if self.settings.redis_url:
            env["REDIS_URL"] = self.settings.redis_url
        # PoC mechanism so sandboxed notebooks can import gallery_shared; in
        # production ship gallery_shared as a wheel in the PEP 723 deps.
        src = str(self.repo_root / "src")
        env["PYTHONPATH"] = src + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src
        logger.info("[%s] starting: %s", app.slug, " ".join(argv[2:]))
        return await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=meta.app_path.parent,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    def _allocate_port(self) -> int:
        taken = {a.port for a in self.apps.values() if a.port is not None}
        for port in range(self.settings.port_range_start, self.settings.port_range_end + 1):
            if port in taken:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise RuntimeError("no free ports in configured range")

    async def _wait_ready(
        self, app: ManagedApp, proc: asyncio.subprocess.Process, port: int
    ) -> bool:
        timeout = (
            self.settings.sandbox_startup_timeout_seconds
            if app.meta.sandbox
            else self.settings.startup_timeout_seconds
        )
        # marimo serves /health under --base-url (verified against 0.23).
        url = f"http://127.0.0.1:{port}/apps/{app.slug}/health"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                return False
            try:
                resp = await self._http.get(url)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        app.start_error = f"not healthy after {timeout}s"
        return False

    def _pump_output(self, app: ManagedApp, proc: asyncio.subprocess.Process) -> None:
        async def pump() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                if text:
                    app.recent_output.append(text)
                    logger.info("[%s] %s", app.slug, text)

        task = asyncio.get_running_loop().create_task(pump())
        app._tasks.add(task)
        task.add_done_callback(app._tasks.discard)

    def _supervise(self, app: ManagedApp, proc: asyncio.subprocess.Process) -> None:
        async def watch() -> None:
            code = await proc.wait()
            if app.proc is proc and app.state == AppState.RUNNING:
                logger.warning("[%s] exited unexpectedly (code %s)", app.slug, code)
                app.state = AppState.STOPPED
                app.port, app.proc = None, None

        task = asyncio.get_running_loop().create_task(watch())
        app._tasks.add(task)
        task.add_done_callback(app._tasks.discard)

    async def stop(self, slug: str) -> None:
        app = self.apps.get(slug)
        if app is None or app.proc is None:
            return
        proc = app.proc
        app.state = AppState.STOPPED
        app.port, app.proc = None, None
        await self._kill(proc)
        storage.cleanup_scratch(slug)
        logger.info("[%s] stopped", slug)

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        # killpg: under --sandbox the direct child is a uv wrapper, and the
        # real marimo server lives deeper in the process group.
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), self.settings.stop_grace_seconds)
        except asyncio.TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass

    # -- idle reaping ---------------------------------------------------------

    def start_reaper(self) -> None:
        self._reaper_task = asyncio.get_running_loop().create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.reaper_interval_seconds)
            now = time.monotonic()
            for app in list(self.apps.values()):
                idle = now - self.last_activity_of(app)
                if (
                    app.state == AppState.RUNNING
                    and app.active_websockets == 0
                    and idle > self.settings.idle_ttl_seconds
                ):
                    logger.info("[%s] idle for %.0fs; reaping", app.slug, idle)
                    await self.stop(app.slug)

    @staticmethod
    def last_activity_of(app: ManagedApp) -> float:
        return app.last_activity

    async def shutdown(self) -> None:
        self._closing = True
        if self._reaper_task:
            self._reaper_task.cancel()

        async def stop_when_settled(app: ManagedApp) -> None:
            # Wait out any in-flight start so it can't resurrect a process
            # after we've stopped everything.
            async with app.start_lock:
                await self.stop(app.slug)

        await asyncio.gather(*(stop_when_settled(a) for a in list(self.apps.values())))
        await self._http.aclose()
