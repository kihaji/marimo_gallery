"""Header-based identity: the DN arrives per-request in GALLERY_DN_HEADER."""

import httpx
import pytest

from gallery.auth import cn_from_dn
from gallery.config import settings
from gallery.main import app

TESTER = "CN=Tester,OU=Engineering,O=Example Corp,C=US"


@pytest.fixture
async def gallery(tmp_path, monkeypatch):
    """Factory for clients acting as a given DN (None = anonymous)."""
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    def make(dn: str | None = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={settings.dn_header: dn} if dn else {},
        )
        clients.append(client)
        return client

    async with app.router.lifespan_context(app):
        yield make
        for client in clients:
            await client.aclose()


def test_cn_from_dn():
    assert cn_from_dn(TESTER) == "Tester"
    assert cn_from_dn("cn = Spaced Out , O=X") == "Spaced Out"
    assert cn_from_dn("O=No Common Name,C=US") == "O=No Common Name,C=US"


async def test_index_hides_protected_notebooks_without_identity(gallery):
    anon = gallery()
    resp = await anon.get("/")
    assert "sales-dashboard" in resp.text
    assert "csv-explorer" not in resp.text

    tester = gallery(TESTER)
    resp = await tester.get("/")
    assert "csv-explorer" in resp.text
    assert "Tester" in resp.text  # CN shown in the topbar


async def test_protected_app_requires_identity(gallery):
    anon = gallery()
    resp = await anon.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 401
    assert "GALLERY_DEV_USER_DN" in resp.text  # dev hint in the 401 body
    assert (await anon.get("/apps/csv-explorer/some/asset.js")).status_code == 401
    assert (await anon.get("/api/apps/csv-explorer/status")).status_code == 401
    assert (await anon.get("/thumbnails/csv-explorer")).status_code == 401

    tester = gallery(TESTER)
    resp = await tester.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Starting CSV Explorer" in resp.text
    assert (await tester.get("/api/apps/csv-explorer/status")).status_code == 200


async def test_public_app_needs_no_identity(gallery):
    anon = gallery()
    resp = await anon.get("/apps/sales-dashboard/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert (await anon.get("/api/apps/sales-dashboard/status")).status_code == 200


async def test_dev_user_dn_fallback(gallery, monkeypatch):
    monkeypatch.setattr(settings, "dev_user_dn", TESTER)
    anon = gallery()
    resp = await anon.get("/")
    assert "csv-explorer" in resp.text
    assert "Tester" in resp.text
