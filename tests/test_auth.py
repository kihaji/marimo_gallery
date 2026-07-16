"""Header-based identity: the DN arrives per-request in GALLERY_DN_HEADER."""

import httpx
import pytest

from gallery.auth import cn_from_dn
from gallery.config import settings
from gallery.db import Database
from gallery.main import app

TESTER = "CN=Tester,OU=Engineering,O=Example Corp,C=US"
ADMIN = "CN=Boss,OU=IT,O=Example Corp,C=US"


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


def join_group(dn: str, group: str = "analytics") -> None:
    """Provision dn (if needed) and add it to a group, creating the group."""
    db = app.state.db
    user = db.upsert_user(dn, cn_from_dn(dn))
    existing = {g["name"]: g for g in db.list_groups()}
    grp = existing.get(group) or db.create_group(group)
    db.set_membership(user["id"], grp["id"], True)


def test_cn_from_dn():
    assert cn_from_dn(TESTER) == "Tester"
    assert cn_from_dn("cn = Spaced Out , O=X") == "Spaced Out"
    assert cn_from_dn("O=No Common Name,C=US") == "O=No Common Name,C=US"


def test_users_and_groups_db(tmp_path):
    db = Database(tmp_path)
    user = db.upsert_user(TESTER, "Tester")
    assert user["is_admin"] is False
    # Bootstrap grant applies on a later sighting; UI revocation possible after.
    assert db.upsert_user(TESTER, "Tester", make_admin=True)["is_admin"] is True
    db.set_admin(user["id"], False)
    assert db.get_user_by_dn(TESTER)["is_admin"] is False

    grp = db.create_group("analytics")
    assert db.create_group("analytics") is None  # duplicate name
    db.set_membership(user["id"], grp["id"], True)
    assert db.groups_of(TESTER) == {"analytics"}
    assert db.list_groups()[0]["member_count"] == 1
    assert db.list_users()[0]["groups"] == ["analytics"]
    db.set_membership(user["id"], grp["id"], False)
    assert db.groups_of(TESTER) == set()
    # Deleting a group cascades membership away.
    db.set_membership(user["id"], grp["id"], True)
    db.delete_group(grp["id"])
    assert db.groups_of(TESTER) == set()
    db.close()


async def test_first_request_provisions_user(gallery, monkeypatch):
    monkeypatch.setattr(settings, "admin_dns", f" {ADMIN} ; CN=Someone Else,O=X ")
    await gallery(TESTER).get("/")
    user = app.state.db.get_user_by_dn(TESTER)
    assert user["display_name"] == "Tester"
    assert user["is_admin"] is False
    assert user["last_seen_at"] is not None

    await gallery(ADMIN).get("/")
    assert app.state.db.get_user_by_dn(ADMIN)["is_admin"] is True


async def test_index_shows_group_gated_notebooks_to_members_only(gallery):
    anon = gallery()
    resp = await anon.get("/")
    assert "sales-dashboard" in resp.text
    assert "csv-explorer" not in resp.text

    tester = gallery(TESTER)
    resp = await tester.get("/")
    assert "csv-explorer" not in resp.text  # authenticated but not in analytics
    assert "Tester" in resp.text  # CN shown in the topbar

    join_group(TESTER, "analytics")
    assert "csv-explorer" in (await tester.get("/")).text


async def test_admins_see_all_notebooks(gallery, monkeypatch):
    monkeypatch.setattr(settings, "admin_dns", ADMIN)
    admin = gallery(ADMIN)
    assert "csv-explorer" in (await admin.get("/")).text


async def test_group_gated_app_access(gallery):
    anon = gallery()
    resp = await anon.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 401
    assert "GALLERY_DEV_USER_DN" in resp.text  # dev hint in the 401 body
    assert (await anon.get("/api/apps/csv-explorer/status")).status_code == 401
    assert (await anon.get("/thumbnails/csv-explorer")).status_code == 401

    # Authenticated non-members get 404, not 401: existence stays hidden.
    tester = gallery(TESTER)
    resp = await tester.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert (await tester.get("/api/apps/csv-explorer/status")).status_code == 404

    join_group(TESTER, "analytics")
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
    assert "Tester" in resp.text
    join_group(TESTER, "analytics")
    assert "csv-explorer" in (await anon.get("/")).text
