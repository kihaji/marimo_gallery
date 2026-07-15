"""Gateway tests.

Fast tests exercise the API surface via ASGITransport against real notebook
dirs. The `slow`-marked test spawns a real marimo subprocess and verifies the
lazy-start -> proxy -> idle-reap lifecycle end to end:

    uv run pytest -m slow
"""

import asyncio

import httpx
import pytest

from gallery.config import Settings
from gallery.main import REPO_ROOT, app
from gallery.manager import AppState, ProcessManager
from gallery.registry import Registry


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            yield http


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["notebooks"] == 3
    assert resp.json()["running"] == 0


async def test_index_page_embeds_data(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "nb-data" in resp.text
    assert "sales-dashboard" in resp.text


async def test_unknown_slug_404(client):
    resp = await client.get("/apps/nope/", headers={"accept": "text/html"})
    assert resp.status_code == 404
    resp = await client.get("/api/apps/nope/status")
    assert resp.status_code == 404


async def test_no_slash_redirects(client):
    resp = await client.get("/apps/sales-dashboard")
    assert resp.status_code == 307
    assert resp.headers["location"] == "/apps/sales-dashboard/"


async def test_page_load_returns_starting_page(client):
    resp = await client.get("/apps/sales-dashboard/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Starting Sales Dashboard" in resp.text


@pytest.mark.slow
async def test_full_lifecycle(tmp_path, monkeypatch):
    """Lazy start a real marimo subprocess, proxy to it, then idle-reap it."""
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    settings = Settings(idle_ttl_seconds=3, reaper_interval_seconds=1)
    registry = Registry(REPO_ROOT / "notebooks")
    manager = ProcessManager(settings, REPO_ROOT)
    manager.sync(registry.scan())
    try:
        managed = await manager.ensure_running("sales-dashboard")
        assert managed.state == AppState.RUNNING
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{manager.base_url('sales-dashboard')}/apps/sales-dashboard/"
            )
            assert resp.status_code == 200

        manager.start_reaper()
        for _ in range(15):
            await asyncio.sleep(1)
            if managed.state != AppState.RUNNING:
                break
        assert managed.state == AppState.STOPPED
        assert managed.proc is None
    finally:
        await manager.shutdown()
