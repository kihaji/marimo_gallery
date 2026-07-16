"""Scheduled notebook runs.

A schedule stores a cron expression plus a validated set of parameters for a
notebook. The Scheduler ticks on the event loop, fires due schedules, and the
Runner executes each run as ``marimo export html`` — a full headless
execution of the notebook whose cell outputs are captured into
``<storage_root>/runs/<slug>/<run_id>/report.html`` (log beside it).
Parameters reach the notebook through ``mo.cli_args()``.

Cron expressions are evaluated in the server's local timezone (containers
default to UTC unless TZ is set); all stored timestamps are UTC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from croniter import CroniterBadCronError, croniter

from gallery.config import Settings
from gallery.db import Database
from gallery.manager import kill_process_group
from gallery.registry import NotebookMeta, NotebookParameter, Registry

logger = logging.getLogger(__name__)

WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}


class ValidationFailure(ValueError):
    """User input that should surface as a 422."""


# -- cadence ------------------------------------------------------------------


def compile_cadence(cadence: dict) -> str:
    """Compile a UI cadence preset into a cron expression."""
    kind = cadence.get("kind")
    if kind == "hours":
        try:
            every = int(cadence.get("every", 0))
        except (TypeError, ValueError):
            every = 0
        if not 1 <= every <= 23:
            raise ValidationFailure("hours cadence needs 'every' between 1 and 23")
        return f"0 */{every} * * *"
    if kind in ("daily", "weekly"):
        hour, minute = _parse_time(cadence.get("time"))
        if kind == "daily":
            return f"{minute} {hour} * * *"
        weekday = WEEKDAYS.get(str(cadence.get("weekday", "")).lower())
        if weekday is None:
            raise ValidationFailure("weekly cadence needs weekday mon..sun")
        return f"{minute} {hour} * * {weekday}"
    raise ValidationFailure("cadence kind must be one of: hours, daily, weekly")


def _parse_time(value) -> tuple[int, int]:
    try:
        hour_s, minute_s = str(value).split(":")
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    raise ValidationFailure("time must be HH:MM (24h)")


def validate_cron(expr: str) -> str:
    expr = expr.strip()
    if len(expr.split()) != 5:
        raise ValidationFailure("cron expression must have exactly 5 fields")
    try:
        croniter(expr)
    except CroniterBadCronError as exc:
        raise ValidationFailure(f"invalid cron expression: {exc}") from exc
    return expr


def describe_cron(expr: str) -> str:
    """A human-readable label for the cron expressions our presets produce;
    anything else is shown verbatim."""
    minute, hour, dom, month, dow = expr.split()
    names = {v: k.capitalize() for k, v in WEEKDAYS.items()}
    if dom == month == "*" and minute.isdigit():
        time = f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() else None
        if dow == "*":
            if hour.startswith("*/"):
                return f"every {hour[2:]} hours"
            if time:
                return f"every day at {time}"
        elif time and dow.isdigit() and int(dow) in names:
            return f"every {names[int(dow)]} at {time}"
    return f"cron: {expr}"


def next_run_utc(expr: str, after: datetime | None = None) -> str:
    """Next fire time (ISO UTC), evaluated in the server's local timezone."""
    base = after or datetime.now().astimezone()
    nxt: datetime = croniter(expr, base).get_next(datetime)
    return nxt.astimezone(timezone.utc).isoformat(timespec="seconds")


# -- parameters ----------------------------------------------------------------


def validate_params(spec: list[NotebookParameter], values: dict) -> dict:
    """Validate user-supplied values against the notebook's declared
    parameters, filling defaults. Raises ValidationFailure with a readable
    message."""
    if not isinstance(values, dict):
        raise ValidationFailure("params must be an object")
    by_name = {p.name: p for p in spec}
    unknown = sorted(set(values) - set(by_name))
    if unknown:
        raise ValidationFailure(f"unknown parameter(s): {', '.join(unknown)}")
    result: dict = {}
    for param in spec:
        value = values.get(param.name, param.default)
        if value is None:
            continue
        result[param.name] = _coerce(param, value)
    return result


def _coerce(param: NotebookParameter, value):
    name = param.name
    if param.type == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValidationFailure(f"parameter {name!r} must be a boolean")
    if param.type == "number":
        if isinstance(value, bool):
            raise ValidationFailure(f"parameter {name!r} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationFailure(f"parameter {name!r} must be a number") from None
        return int(number) if number.is_integer() else number
    text = str(value)
    if len(text) > 500:
        raise ValidationFailure(f"parameter {name!r} is too long")
    if text.startswith("-"):
        raise ValidationFailure(f"parameter {name!r} must not start with '-'")
    if param.type == "choice" and text not in (param.choices or []):
        raise ValidationFailure(f"parameter {name!r} must be one of {param.choices}")
    return text


def params_to_cli(params: dict) -> list[str]:
    argv: list[str] = []
    for name, value in params.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        argv += [f"--{name}", str(value)]
    return argv


# -- execution -------------------------------------------------------------------


class Runner:
    """Executes one run as a `marimo export html` subprocess."""

    def __init__(self, settings: Settings, repo_root: Path) -> None:
        self.settings = settings
        self.repo_root = repo_root

    def run_dir(self, slug: str, run_id: str) -> Path:
        return self.settings.storage_root.resolve() / "runs" / slug / run_id

    async def run(
        self, run_id: str, meta: NotebookMeta, params: dict, dn: str | None
    ) -> tuple[str, int | None]:
        """Execute the notebook on behalf of ``dn`` (the schedule/run creator);
        returns (status, exit_code) where status is success | failed | timeout."""
        run_dir = self.run_dir(meta.slug, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        report = run_dir / "report.html"
        argv = [
            sys.executable,
            "-m",
            "marimo",
            "export",
            "html",
            str(meta.app_path.resolve()),
            "-o",
            str(report),
        ]
        if not meta.include_code:
            argv.append("--no-include-code")
        if meta.sandbox:
            argv.append("--sandbox")
        argv.append("--")
        argv += params_to_cli(params)

        env = os.environ.copy()
        env["MARIMO_GALLERY_APP"] = meta.slug
        env["GALLERY_STORAGE_ROOT"] = str(self.settings.storage_root.resolve())
        if dn:
            # Exports have no HTTP request to read the DN header from;
            # gallery_shared.identity.current_dn() falls back to this.
            env["GALLERY_USER_DN"] = dn
        if self.settings.redis_url:
            env["REDIS_URL"] = self.settings.redis_url
        src = str(self.repo_root / "src")
        env["PYTHONPATH"] = src + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src

        timeout = (
            self.settings.schedule_sandbox_run_timeout_seconds
            if meta.sandbox
            else self.settings.schedule_run_timeout_seconds
        )
        with open(run_dir / "run.log", "w") as log:
            log.write(f"# started {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            log.write(f"# command marimo {' '.join(argv[3:])}\n")
            log.write(f"# params {json.dumps(params)}\n")
            log.write(f"# on behalf of {dn or '(unknown)'}\n\n")
            log.flush()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                cwd=meta.app_path.parent,
                start_new_session=True,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                await kill_process_group(proc, self.settings.stop_grace_seconds)
                log.write(f"\n# killed: run exceeded {timeout}s timeout\n")
                return "timeout", None
            except asyncio.CancelledError:
                await kill_process_group(proc, self.settings.stop_grace_seconds)
                log.write("\n# killed: gateway shutting down\n")
                raise
        if exit_code == 0 and report.is_file():
            return "success", exit_code
        return "failed", exit_code


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        db: Database,
        runner: Runner,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.db = db
        self.runner = runner
        self._semaphore = asyncio.Semaphore(settings.schedule_max_concurrent_runs)
        self._active_schedules: set[int] = set()
        self._tick_task: asyncio.Task | None = None
        self._run_tasks: set[asyncio.Task] = set()
        self._closing = False

    def start(self) -> None:
        self.db.recover_stale_runs()
        if not self.settings.schedules_enabled:
            logger.info("scheduler disabled (GALLERY_SCHEDULES_ENABLED=false)")
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for schedule in self.db.list_schedules():
            if not schedule["enabled"]:
                continue
            if schedule["next_run_at"] and schedule["next_run_at"] <= now:
                logger.info(
                    "[%s] schedule %s missed fire(s) while gateway was down; skipping",
                    schedule["slug"],
                    schedule["id"],
                )
            self.db.set_next_run(schedule["id"], next_run_utc(schedule["cron"]))
        self._tick_task = asyncio.get_running_loop().create_task(self._tick_loop())
        logger.info("scheduler started (tick every %ds)", self.settings.schedule_tick_seconds)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.schedule_tick_seconds)
            try:
                self._tick()
            except Exception:
                logger.exception("scheduler tick failed")

    def _tick(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for schedule in self.db.due_schedules(now):
            # Claim first so the next tick doesn't pick this fire up again.
            self.db.set_next_run(schedule["id"], next_run_utc(schedule["cron"]))
            meta = self.registry.notebooks.get(schedule["slug"])
            if meta is None:
                logger.warning(
                    "schedule %s references missing notebook %r; skipping",
                    schedule["id"],
                    schedule["slug"],
                )
                continue
            if schedule["id"] in self._active_schedules:
                logger.warning(
                    "[%s] schedule %s still running; skipping this fire",
                    schedule["slug"],
                    schedule["id"],
                )
                continue
            # Runs inherit the schedule creator so ownership-based visibility
            # survives even if the schedule is later deleted.
            run = self.db.create_run(
                meta.slug,
                schedule["params"],
                schedule_id=schedule["id"],
                created_by=schedule["created_by"],
            )
            logger.info("[%s] schedule %s fired -> run %s", meta.slug, schedule["id"], run["id"])
            self._spawn_run(run, meta, schedule_id=schedule["id"])

    def run_now(self, meta: NotebookMeta, params: dict, user: str) -> dict:
        run = self.db.create_run(meta.slug, params, created_by=user, manual=True)
        logger.info("[%s] manual run %s by %s", meta.slug, run["id"], user)
        self._spawn_run(run, meta, schedule_id=None)
        return run

    def _spawn_run(self, run: dict, meta: NotebookMeta, schedule_id: int | None) -> None:
        if schedule_id is not None:
            self._active_schedules.add(schedule_id)
        task = asyncio.get_running_loop().create_task(self._execute(run, meta, schedule_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def _execute(self, run: dict, meta: NotebookMeta, schedule_id: int | None) -> None:
        run_id = run["id"]
        try:
            async with self._semaphore:
                if self._closing:
                    self.db.mark_run_finished(run_id, "failed", None)
                    return
                self.db.mark_run_started(run_id)
                try:
                    status, exit_code = await self.runner.run(
                        run_id, meta, run["params"], run["created_by"]
                    )
                except asyncio.CancelledError:
                    self.db.mark_run_finished(run_id, "failed", None)
                    raise
                except Exception:
                    logger.exception("[%s] run %s crashed", meta.slug, run_id)
                    status, exit_code = "failed", None
                self.db.mark_run_finished(run_id, status, exit_code)
                logger.info("[%s] run %s finished: %s", meta.slug, run_id, status)
        finally:
            if schedule_id is not None:
                self._active_schedules.discard(schedule_id)
            self._prune(meta.slug, schedule_id)

    def _prune(self, slug: str, schedule_id: int | None) -> None:
        pruned = self.db.prune_runs(slug, schedule_id, self.settings.schedule_runs_keep)
        for run_id in pruned:
            shutil.rmtree(self.runner.run_dir(slug, run_id), ignore_errors=True)
        if pruned:
            logger.info("[%s] pruned %d old run(s)", slug, len(pruned))

    async def shutdown(self) -> None:
        self._closing = True
        if self._tick_task:
            self._tick_task.cancel()
        for task in list(self._run_tasks):
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)
