import asyncio

import httpx
import pytest

from gallery import auth
from gallery.config import settings
from gallery.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    users_file = tmp_path / "users.yaml"
    users_file.write_text(f"tester: {auth.hash_password('pw123', iterations=1000)}\n")
    monkeypatch.setattr(settings, "users_file", users_file)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def login(http):
    resp = await http.post("/login", data={"username": "tester", "password": "pw123", "next": "/"})
    assert resp.status_code == 303


class StubRunner:
    def __init__(self, tmp_path, status="success"):
        self.tmp = tmp_path
        self.status = status

    def run_dir(self, slug, run_id):
        return self.tmp / "runs" / slug / run_id

    async def run(self, run_id, meta, params):
        return self.status, 0


def stub_runner(tmp_path):
    runner = StubRunner(tmp_path)
    app.state.scheduler.runner = runner
    return runner


# -- page + read access ---------------------------------------------------------


async def test_page_public_notebook_anonymous(client):
    resp = await client.get("/schedules/sales-dashboard")
    assert resp.status_code == 200
    assert "sched-data" in resp.text


async def test_page_protected_notebook_redirects(client):
    resp = await client.get("/schedules/csv-explorer")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/schedules/csv-explorer"
    resp = await client.get("/api/schedules/csv-explorer")
    assert resp.status_code == 401


async def test_unknown_slug(client):
    assert (await client.get("/schedules/nope")).status_code == 404
    assert (await client.get("/api/schedules/nope")).status_code == 404


# -- mutations require login ------------------------------------------------------


async def test_anonymous_mutations_blocked(client):
    assert (
        await client.post("/api/schedules/sales-dashboard", json={"name": "x"})
    ).status_code == 401
    assert (
        await client.post("/api/schedules/sales-dashboard/run", json={"params": {}})
    ).status_code == 401
    assert (
        await client.patch("/api/schedules/sales-dashboard/1", json={"enabled": False})
    ).status_code == 404  # no such schedule yet; 404 before auth is fine
    await login(client)
    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={"name": "s", "cadence": {"kind": "daily", "time": "02:00"}},
        )
    ).json()
    await client.post("/logout")
    sid = schedule["id"]
    assert (
        await client.patch(f"/api/schedules/sales-dashboard/{sid}", json={"enabled": False})
    ).status_code == 401
    assert (await client.delete(f"/api/schedules/sales-dashboard/{sid}")).status_code == 401


# -- schedule CRUD -----------------------------------------------------------------


async def test_create_with_preset_and_cron(client):
    await login(client)
    resp = await client.post(
        "/api/schedules/sales-dashboard",
        json={
            "name": "nightly",
            "cadence": {"kind": "daily", "time": "02:00"},
            "params": {"region": "West", "days": 30},
        },
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["cron"] == "0 2 * * *"
    assert row["cadence_label"] == "every day at 02:00"
    assert row["params"] == {"region": "West", "days": 30}
    assert row["next_run_at"] is not None

    resp = await client.post(
        "/api/schedules/sales-dashboard",
        json={"name": "custom", "cron": "*/15 * * * *"},
    )
    assert resp.status_code == 200
    assert resp.json()["cron"] == "*/15 * * * *"

    listing = (await client.get("/api/schedules/sales-dashboard")).json()
    assert [s["name"] for s in listing["schedules"]] == ["nightly", "custom"]


@pytest.mark.parametrize(
    "body,fragment",
    [
        ({"name": "x", "cron": "banana"}, "cron"),
        ({"name": "x", "cadence": {"kind": "hourly"}}, "cadence"),
        ({"cadence": {"kind": "daily", "time": "02:00"}}, "name"),
        (
            {"name": "x", "cadence": {"kind": "daily", "time": "02:00"}, "params": {"bogus": 1}},
            "unknown parameter",
        ),
        (
            {
                "name": "x",
                "cadence": {"kind": "daily", "time": "02:00"},
                "params": {"region": "Mars"},
            },
            "region",
        ),
    ],
)
async def test_create_validation_errors(client, body, fragment):
    await login(client)
    resp = await client.post("/api/schedules/sales-dashboard", json=body)
    assert resp.status_code == 422
    assert fragment in resp.json()["error"]


async def test_disable_enable_delete(client):
    await login(client)
    row = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={"name": "s", "cadence": {"kind": "hours", "every": 6}},
        )
    ).json()
    sid = row["id"]
    off = (
        await client.patch(f"/api/schedules/sales-dashboard/{sid}", json={"enabled": False})
    ).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    on = (
        await client.patch(f"/api/schedules/sales-dashboard/{sid}", json={"enabled": True})
    ).json()
    assert on["enabled"] is True and on["next_run_at"] is not None
    assert (await client.delete(f"/api/schedules/sales-dashboard/{sid}")).status_code == 200
    listing = (await client.get("/api/schedules/sales-dashboard")).json()
    assert listing["schedules"] == []


# -- run-now + artifacts -------------------------------------------------------------


async def test_run_now_and_artifacts(client, tmp_path):
    runner = stub_runner(tmp_path)
    await login(client)
    resp = await client.post(
        "/api/schedules/sales-dashboard/run", json={"params": {"region": "East"}}
    )
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "queued"
    assert run["created_by"] == "tester"

    for _ in range(50):
        listing = (await client.get("/api/schedules/sales-dashboard")).json()
        current = next(r for r in listing["runs"] if r["id"] == run["id"])
        if current["status"] != "queued" and current["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert current["status"] == "success"

    # Artifacts: 404 until files exist, then served with the right type.
    assert (await client.get(f"/runs/sales-dashboard/{run['id']}/report")).status_code == 404
    run_dir = runner.run_dir("sales-dashboard", run["id"])
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html>report</html>")
    (run_dir / "run.log").write_text("log line")
    report = await client.get(f"/runs/sales-dashboard/{run['id']}/report")
    assert report.status_code == 200 and "report" in report.text
    log = await client.get(f"/runs/sales-dashboard/{run['id']}/log")
    assert log.status_code == 200
    assert log.headers["content-type"].startswith("text/plain")

    # Traversal / identity checks.
    assert (await client.get("/runs/sales-dashboard/deadbeef/report")).status_code == 404
    assert (await client.get(f"/runs/cluster-lab/{run['id']}/report")).status_code == 404


async def test_protected_run_artifacts_require_login(client, tmp_path):
    runner = stub_runner(tmp_path)
    await login(client)
    run = (await client.post("/api/schedules/csv-explorer/run", json={"params": {}})).json()
    run_dir = runner.run_dir("csv-explorer", run["id"])
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html>secret</html>")
    assert (await client.get(f"/runs/csv-explorer/{run['id']}/report")).status_code == 200
    await client.post("/logout")
    assert (await client.get(f"/runs/csv-explorer/{run['id']}/report")).status_code == 401
