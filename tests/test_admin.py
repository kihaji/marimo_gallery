"""Admin pages: group/membership management, hidden from non-admins."""

import httpx
import pytest

from gallery.config import settings
from gallery.main import app

ADMIN = "CN=Boss,OU=IT,O=Example Corp,C=US"
TESTER = "CN=Tester,OU=Engineering,O=Example Corp,C=US"


@pytest.fixture
async def gallery(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "admin_dns", ADMIN)
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


async def test_admin_routes_hidden_from_everyone_else(gallery):
    for actor in (gallery(), gallery(TESTER)):
        assert (await actor.get("/admin")).status_code == 404
        assert (await actor.get("/api/admin")).status_code == 404
        assert (await actor.post("/api/admin/groups", json={"name": "x"})).status_code == 404
        assert (
            await actor.post(
                "/api/admin/membership", json={"user_id": 1, "group_id": 1, "member": True}
            )
        ).status_code == 404
        assert (
            await actor.post("/api/admin/users/1/admin", json={"is_admin": True})
        ).status_code == 404
    # The admin link only renders for admins.
    assert '"/admin"' not in (await gallery(TESTER).get("/")).text
    assert '"/admin"' in (await gallery(ADMIN).get("/")).text


async def test_group_and_membership_management(gallery):
    admin = gallery(ADMIN)
    await gallery(TESTER).get("/")  # provision tester

    page = await admin.get("/admin")
    assert page.status_code == 200 and "admin-data" in page.text

    group = (await admin.post("/api/admin/groups", json={"name": "analytics"})).json()
    dup = await admin.post("/api/admin/groups", json={"name": "analytics"})
    assert dup.status_code == 422
    assert (await admin.post("/api/admin/groups", json={"name": "  "})).status_code == 422

    tester = app.state.db.get_user_by_dn(TESTER)
    state = (
        await admin.post(
            "/api/admin/membership",
            json={"user_id": tester["id"], "group_id": group["id"], "member": True},
        )
    ).json()
    tester_row = next(u for u in state["users"] if u["dn"] == TESTER)
    assert tester_row["groups"] == ["analytics"]
    # Membership grants notebook access immediately.
    assert "csv-explorer" in (await gallery(TESTER).get("/")).text

    state = (
        await admin.post(
            "/api/admin/membership",
            json={"user_id": tester["id"], "group_id": group["id"], "member": False},
        )
    ).json()
    assert next(u for u in state["users"] if u["dn"] == TESTER)["groups"] == []

    assert (await admin.delete(f"/api/admin/groups/{group['id']}")).status_code == 200
    assert (await admin.get("/api/admin")).json()["groups"] == []


async def test_admin_toggle_and_self_demotion_guard(gallery):
    admin = gallery(ADMIN)
    await gallery(TESTER).get("/")
    tester = app.state.db.get_user_by_dn(TESTER)

    state = (
        await admin.post(f"/api/admin/users/{tester['id']}/admin", json={"is_admin": True})
    ).json()
    assert next(u for u in state["users"] if u["dn"] == TESTER)["is_admin"] is True
    assert "csv-explorer" in (await gallery(TESTER).get("/")).text  # admins see everything

    me = app.state.db.get_user_by_dn(ADMIN)
    resp = await admin.post(f"/api/admin/users/{me['id']}/admin", json={"is_admin": False})
    assert resp.status_code == 422
    assert app.state.db.get_user_by_dn(ADMIN)["is_admin"] is True

    assert (
        await admin.post("/api/admin/users/9999/admin", json={"is_admin": True})
    ).status_code == 404
