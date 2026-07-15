import httpx
import pytest

from gallery import auth
from gallery.config import settings
from gallery.main import app


def test_hash_and_verify_roundtrip():
    encoded = auth.hash_password("s3cret", iterations=1000)
    assert auth.verify_password("s3cret", encoded)
    assert not auth.verify_password("wrong", encoded)
    assert not auth.verify_password("s3cret", "garbage")
    assert not auth.verify_password("s3cret", "")


def test_load_users_tolerates_missing_and_bad_files(tmp_path):
    assert auth.load_users(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string")
    assert auth.load_users(bad) == {}
    good = tmp_path / "users.yaml"
    good.write_text("alice: somehash\n")
    assert auth.load_users(good) == {"alice": "somehash"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    users_file = tmp_path / "users.yaml"
    users_file.write_text(f"tester: {auth.hash_password('pw123', iterations=1000)}\n")
    monkeypatch.setattr(settings, "users_file", users_file)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def login(http, username="tester", password="pw123"):
    return await http.post(
        "/login", data={"username": username, "password": password, "next": "/"}
    )


async def test_index_hides_protected_notebooks_until_login(client):
    resp = await client.get("/")
    assert "sales-dashboard" in resp.text
    assert "csv-explorer" not in resp.text

    resp = await login(client)
    assert resp.status_code == 303

    resp = await client.get("/")
    assert "csv-explorer" in resp.text
    assert "tester" in resp.text


async def test_login_rejects_bad_credentials(client):
    resp = await login(client, password="nope")
    assert resp.status_code == 401
    resp = await login(client, username="ghost")
    assert resp.status_code == 401
    resp = await client.get("/")
    assert "csv-explorer" not in resp.text


async def test_protected_app_requires_login(client):
    # Anonymous page load redirects to the login form.
    resp = await client.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/apps/csv-explorer/"
    # Non-page requests get a bare 401.
    resp = await client.get("/apps/csv-explorer/some/asset.js")
    assert resp.status_code == 401
    resp = await client.get("/api/apps/csv-explorer/status")
    assert resp.status_code == 401
    resp = await client.get("/thumbnails/csv-explorer")
    assert resp.status_code == 401

    await login(client)
    resp = await client.get("/apps/csv-explorer/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Starting CSV Explorer" in resp.text
    resp = await client.get("/api/apps/csv-explorer/status")
    assert resp.status_code == 200


async def test_public_app_needs_no_login(client):
    resp = await client.get("/apps/sales-dashboard/", headers={"accept": "text/html"})
    assert resp.status_code == 200
    resp = await client.get("/api/apps/sales-dashboard/status")
    assert resp.status_code == 200


async def test_logout_clears_session(client):
    await login(client)
    resp = await client.post("/logout")
    assert resp.status_code == 303
    resp = await client.get("/")
    assert "csv-explorer" not in resp.text


async def test_login_next_blocks_open_redirects(client):
    resp = await client.post(
        "/login", data={"username": "tester", "password": "pw123", "next": "//evil.example"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
