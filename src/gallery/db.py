"""SQLite persistence for notebook schedules and run history.

One database file lives at ``<storage_root>/gallery.db``. All methods are
synchronous and MUST only be called from the event loop thread — every query
touches a handful of tiny rows, so this serializes access without locks. If
an operation ever grows past that, wrap it in ``asyncio.to_thread`` behind an
``asyncio.Lock`` instead of calling it from a thread directly.

Run artifacts are NOT stored in the database; their location is derived:
``<storage_root>/runs/<slug>/<run_id>/{report.html,run.log}``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_STATUSES = ("queued", "running", "success", "failed", "timeout")

_DDL = """
CREATE TABLE IF NOT EXISTS schedules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL,
  name        TEXT NOT NULL,
  cron        TEXT NOT NULL,
  params      TEXT NOT NULL DEFAULT '{}',
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  next_run_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
  slug        TEXT NOT NULL,
  status      TEXT NOT NULL,
  params      TEXT NOT NULL DEFAULT '{}',
  created_by  TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  exit_code   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_slug ON runs(slug, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_schedule ON runs(schedule_id, created_at DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    record = dict(row)
    if "params" in record:
        record["params"] = json.loads(record["params"])
    if "enabled" in record:
        record["enabled"] = bool(record["enabled"])
    return record


class Database:
    def __init__(self, storage_root: Path) -> None:
        storage_root.mkdir(parents=True, exist_ok=True)
        self.path = storage_root / "gallery.db"
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- schedules ------------------------------------------------------------

    def create_schedule(
        self,
        slug: str,
        name: str,
        cron: str,
        params: dict,
        created_by: str,
        next_run_at: str,
    ) -> dict:
        cur = self._conn.execute(
            "INSERT INTO schedules (slug, name, cron, params, created_by, created_at,"
            " next_run_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, name, cron, json.dumps(params), created_by, utcnow(), next_run_at),
        )
        self._conn.commit()
        return self.get_schedule(cur.lastrowid)

    def get_schedule(self, schedule_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_schedules(self, slug: str | None = None) -> list[dict]:
        if slug is None:
            rows = self._conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE slug = ? ORDER BY id", (slug,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def due_schedules(self, now_utc: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL"
            " AND next_run_at <= ?",
            (now_utc,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def set_next_run(self, schedule_id: int, next_run_at: str | None) -> None:
        self._conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run_at, schedule_id)
        )
        self._conn.commit()

    def set_enabled(self, schedule_id: int, enabled: bool, next_run_at: str | None) -> None:
        self._conn.execute(
            "UPDATE schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
            (int(enabled), next_run_at, schedule_id),
        )
        self._conn.commit()

    def delete_schedule(self, schedule_id: int) -> None:
        self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self._conn.commit()

    # -- runs -----------------------------------------------------------------

    def create_run(
        self,
        slug: str,
        params: dict,
        schedule_id: int | None = None,
        created_by: str | None = None,
    ) -> dict:
        run_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO runs (id, schedule_id, slug, status, params, created_by,"
            " created_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (run_id, schedule_id, slug, json.dumps(params), created_by, utcnow()),
        )
        self._conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def mark_run_started(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
            (utcnow(), run_id),
        )
        self._conn.commit()

    def mark_run_finished(self, run_id: str, status: str, exit_code: int | None) -> None:
        assert status in ("success", "failed", "timeout")
        self._conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, exit_code = ? WHERE id = ?",
            (status, utcnow(), exit_code, run_id),
        )
        self._conn.commit()

    def list_runs(self, slug: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT runs.*, schedules.name AS schedule_name FROM runs"
            " LEFT JOIN schedules ON schedules.id = runs.schedule_id"
            " WHERE runs.slug = ? ORDER BY runs.created_at DESC, runs.rowid DESC LIMIT ?",
            (slug, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def has_active_runs(self, slug: str | None = None) -> bool:
        query = "SELECT 1 FROM runs WHERE status IN ('queued', 'running')"
        args: tuple = ()
        if slug is not None:
            query += " AND slug = ?"
            args = (slug,)
        return self._conn.execute(query + " LIMIT 1", args).fetchone() is not None

    def recover_stale_runs(self) -> int:
        """Mark runs interrupted by a gateway restart as failed."""
        cur = self._conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?"
            " WHERE status IN ('queued', 'running')",
            (utcnow(),),
        )
        self._conn.commit()
        if cur.rowcount:
            logger.warning("marked %d interrupted run(s) as failed", cur.rowcount)
        return cur.rowcount

    def prune_runs(self, slug: str, schedule_id: int | None, keep: int) -> list[str]:
        """Delete finished runs beyond the newest ``keep`` for one schedule
        (or for ad-hoc runs when schedule_id is None) and return their ids so
        the caller can remove artifact directories."""
        clause = "schedule_id IS NULL" if schedule_id is None else "schedule_id = ?"
        args: list = [slug] if schedule_id is None else [slug, schedule_id]
        rows = self._conn.execute(
            f"SELECT id FROM runs WHERE slug = ? AND {clause}"
            " AND status IN ('success', 'failed', 'timeout')"
            # rowid tie-break: created_at has second resolution, so runs
            # created in the same second need insertion order to decide age.
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (*args, keep),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            marks = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM runs WHERE id IN ({marks})", ids)
            self._conn.commit()
        return ids
