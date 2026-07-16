import asyncio

import httpx
import pytest

from gallery.auth import cn_from_dn
from gallery.config import settings
from gallery.main import app

TESTER = "CN=Tester,OU=Engineering,O=Example Corp,C=US"
OTHER = "CN=Other,OU=Engineering,O=Example Corp,C=US"


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


@pytest.fixture
def client(gallery):
    return gallery(TESTER)


def join_group(dn: str, group: str) -> None:
    """Provision dn (if needed) and add it to a group, creating the group."""
    db = app.state.db
    user = db.upsert_user(dn, cn_from_dn(dn))
    existing = {g["name"]: g for g in db.list_groups()}
    grp = existing.get(group) or db.create_group(group)
    db.set_membership(user["id"], grp["id"], True)


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


async def test_page_requires_identity_even_for_public_notebooks(gallery, client):
    anon = gallery()
    assert (await anon.get("/schedules/sales-dashboard")).status_code == 401
    assert (await anon.get("/api/schedules/sales-dashboard")).status_code == 401
    resp = await client.get("/schedules/sales-dashboard")
    assert resp.status_code == 200
    assert "sched-data" in resp.text


async def test_unknown_slug(client):
    assert (await client.get("/schedules/nope")).status_code == 404
    assert (await client.get("/api/schedules/nope")).status_code == 404


# -- mutations require identity ---------------------------------------------------


async def test_anonymous_mutations_blocked(gallery, client):
    anon = gallery()
    assert (
        await anon.post("/api/schedules/sales-dashboard", json={"name": "x"})
    ).status_code == 401
    assert (
        await anon.post("/api/schedules/sales-dashboard/run", json={"params": {}})
    ).status_code == 401
    assert (
        await anon.patch("/api/schedules/sales-dashboard/1", json={"enabled": False})
    ).status_code == 401
    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={"name": "s", "cadence": {"kind": "daily", "time": "02:00"}},
        )
    ).json()
    sid = schedule["id"]
    assert (
        await anon.patch(f"/api/schedules/sales-dashboard/{sid}", json={"enabled": False})
    ).status_code == 401
    assert (await anon.delete(f"/api/schedules/sales-dashboard/{sid}")).status_code == 401


# -- schedule CRUD -----------------------------------------------------------------


async def test_create_with_preset_and_cron(client):
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
    resp = await client.post("/api/schedules/sales-dashboard", json=body)
    assert resp.status_code == 422
    assert fragment in resp.json()["error"]


async def test_disable_enable_delete(client):
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
    resp = await client.post(
        "/api/schedules/sales-dashboard/run", json={"params": {"region": "East"}}
    )
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "queued"
    assert run["created_by"] == TESTER

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


async def test_group_gated_notebook_schedules_hidden_from_non_members(gallery, client):
    # csv-explorer is restricted to the analytics group; tester is not in it.
    assert (await client.get("/schedules/csv-explorer")).status_code == 404
    assert (await client.get("/api/schedules/csv-explorer")).status_code == 404
    assert (
        await client.post("/api/schedules/csv-explorer/run", json={"params": {}})
    ).status_code == 404


async def test_run_artifacts_require_identity(gallery, client, tmp_path):
    runner = stub_runner(tmp_path)
    join_group(TESTER, "analytics")
    run = (await client.post("/api/schedules/csv-explorer/run", json={"params": {}})).json()
    run_dir = runner.run_dir("csv-explorer", run["id"])
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html>secret</html>")
    assert (await client.get(f"/runs/csv-explorer/{run['id']}/report")).status_code == 200
    anon = gallery()
    assert (await anon.get(f"/runs/csv-explorer/{run['id']}/report")).status_code == 401


# -- per-user isolation -----------------------------------------------------------


async def test_users_cannot_see_or_touch_each_others_schedules(gallery, client, tmp_path):
    runner = stub_runner(tmp_path)
    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={
                "name": "secret nightly",
                "cadence": {"kind": "daily", "time": "02:00"},
                "params": {"region": "West"},
            },
        )
    ).json()
    run = (
        await client.post(
            "/api/schedules/sales-dashboard/run", json={"params": {"region": "West"}}
        )
    ).json()
    run_dir = runner.run_dir("sales-dashboard", run["id"])
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html>sensitive</html>")

    # tester sees their own things
    mine = (await client.get("/api/schedules/sales-dashboard")).json()
    assert [s["name"] for s in mine["schedules"]] == ["secret nightly"]
    assert [r["id"] for r in mine["runs"]] == [run["id"]]

    other = gallery(OTHER)
    theirs = (await other.get("/api/schedules/sales-dashboard")).json()
    assert theirs["schedules"] == []
    assert theirs["runs"] == []
    assert "secret nightly" not in (await other.get("/schedules/sales-dashboard")).text

    sid = schedule["id"]
    patch = await other.patch(
        f"/api/schedules/sales-dashboard/{sid}", json={"enabled": False}
    )
    assert patch.status_code == 404
    assert (await other.delete(f"/api/schedules/sales-dashboard/{sid}")).status_code == 404
    assert (
        await other.get(f"/runs/sales-dashboard/{run['id']}/report")
    ).status_code == 404
    assert (await other.get(f"/runs/sales-dashboard/{run['id']}/log")).status_code == 404

    # tester's schedule is untouched
    mine = (await client.get("/api/schedules/sales-dashboard")).json()
    assert mine["schedules"][0]["enabled"] is True


async def test_schedule_sharing_is_view_only_for_group_members(gallery, client, tmp_path):
    runner = stub_runner(tmp_path)
    join_group(TESTER, "team")
    join_group(OTHER, "team")
    outsider = gallery("CN=Outsider,O=Example Corp")
    other = gallery(OTHER)

    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={
                "name": "team nightly",
                "cadence": {"kind": "daily", "time": "02:00"},
                "params": {"region": "West"},
            },
        )
    ).json()
    sid = schedule["id"]
    # One run produced by the schedule itself, one ad-hoc "run now".
    app.state.db.set_next_run(sid, "2000-01-01T00:00:00+00:00")
    app.state.scheduler._tick()
    manual = (
        await client.post(
            "/api/schedules/sales-dashboard/run", json={"params": {"region": "West"}}
        )
    ).json()
    for _ in range(50):
        mine = (await client.get("/api/schedules/sales-dashboard")).json()["runs"]
        if all(r["status"] == "success" for r in mine):
            break
        await asyncio.sleep(0.05)
    run = next(r for r in mine if r["id"] != manual["id"])
    run_dir = runner.run_dir("sales-dashboard", run["id"])
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<html>team report</html>")

    # Not shared yet: invisible to everyone but the owner.
    assert (await other.get("/api/schedules/sales-dashboard")).json()["schedules"] == []

    # Owner can only share with a group they belong to.
    group_id = next(
        g["id"]
        for g in (await client.get("/api/schedules/sales-dashboard")).json()["my_groups"]
    )
    bad = await client.patch(
        f"/api/schedules/sales-dashboard/{sid}", json={"shared_group_id": 9999}
    )
    assert bad.status_code == 422
    shared = (
        await client.patch(
            f"/api/schedules/sales-dashboard/{sid}", json={"shared_group_id": group_id}
        )
    ).json()
    assert shared["shared_group"] == "team" and shared["own"] is True

    # Group member sees the schedule and its runs/artifacts, read-only.
    # The owner's ad-hoc "run now" (no schedule) stays private to the owner.
    theirs = (await other.get("/api/schedules/sales-dashboard")).json()
    row = next(s for s in theirs["schedules"] if s["id"] == sid)
    assert row["own"] is False and row["shared_group"] == "team"
    assert [r["id"] for r in theirs["runs"]] == [run["id"]]
    assert (
        await other.get(f"/runs/sales-dashboard/{manual['id']}/report")
    ).status_code == 404
    assert (await other.get(f"/runs/sales-dashboard/{run['id']}/report")).status_code == 200
    assert (
        await other.patch(f"/api/schedules/sales-dashboard/{sid}", json={"enabled": False})
    ).status_code == 404
    assert (await other.delete(f"/api/schedules/sales-dashboard/{sid}")).status_code == 404
    # A member may not re-share or unshare someone else's schedule either.
    assert (
        await other.patch(
            f"/api/schedules/sales-dashboard/{sid}", json={"shared_group_id": None}
        )
    ).status_code == 404

    # Outside the group nothing is visible.
    outsiders = (await outsider.get("/api/schedules/sales-dashboard")).json()
    assert outsiders["schedules"] == [] and outsiders["runs"] == []
    assert (
        await outsider.get(f"/runs/sales-dashboard/{run['id']}/report")
    ).status_code == 404

    # Unsharing makes it private again.
    await client.patch(f"/api/schedules/sales-dashboard/{sid}", json={"shared_group_id": None})
    assert (await other.get("/api/schedules/sales-dashboard")).json()["schedules"] == []
    assert (await other.get(f"/runs/sales-dashboard/{run['id']}/report")).status_code == 404


async def test_deleting_shared_group_reverts_to_private(gallery, client):
    join_group(TESTER, "team")
    join_group(OTHER, "team")
    other = gallery(OTHER)
    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={"name": "s", "cadence": {"kind": "daily", "time": "02:00"}},
        )
    ).json()
    group_id = next(g["id"] for g in app.state.db.list_groups() if g["name"] == "team")
    await client.patch(
        f"/api/schedules/sales-dashboard/{schedule['id']}", json={"shared_group_id": group_id}
    )
    assert (await other.get("/api/schedules/sales-dashboard")).json()["schedules"] != []
    app.state.db.delete_group(group_id)
    assert (await other.get("/api/schedules/sales-dashboard")).json()["schedules"] == []
    mine = (await client.get("/api/schedules/sales-dashboard")).json()["schedules"]
    assert mine[0]["shared_group_id"] is None  # ON DELETE SET NULL


async def test_scheduled_runs_belong_to_schedule_creator(gallery, client, tmp_path):
    stub_runner(tmp_path)
    schedule = (
        await client.post(
            "/api/schedules/sales-dashboard",
            json={"name": "mine", "cadence": {"kind": "daily", "time": "02:00"}},
        )
    ).json()
    # Make it due and fire the scheduler synchronously.
    app.state.db.set_next_run(schedule["id"], "2000-01-01T00:00:00+00:00")
    app.state.scheduler._tick()
    for _ in range(50):
        runs = (await client.get("/api/schedules/sales-dashboard")).json()["runs"]
        if runs and runs[0]["status"] == "success":
            break
        await asyncio.sleep(0.05)
    assert runs and runs[0]["created_by"] == TESTER and not runs[0]["manual"]

    other = gallery(OTHER)
    assert (await other.get("/api/schedules/sales-dashboard")).json()["runs"] == []
