from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GALLERY_")

    notebooks_dir: Path = Path("notebooks")
    storage_root: Path = Path("./data")

    # Internal ports handed to marimo subprocesses.
    port_range_start: int = 10000
    port_range_end: int = 10999

    # Process lifecycle.
    idle_ttl_seconds: int = 900
    reaper_interval_seconds: int = 30
    startup_timeout_seconds: int = 40
    sandbox_startup_timeout_seconds: int = 300
    stop_grace_seconds: int = 10
    marimo_session_ttl: int = 600

    max_upload_bytes: int = 100 * 1024 * 1024

    # External backends: when set (e.g. "http://nb-{slug}:2718"), the gateway
    # spawns nothing and proxies each notebook to this URL instead — the
    # per-notebook-Deployment topology in deploy/horizontal_k8s/.
    backend_url_template: str | None = None

    redis_url: str | None = None

    # Scheduled runs. The scheduler is in-process: with multiple gateway
    # replicas, enable it on exactly one (schedules_enabled=false elsewhere).
    schedules_enabled: bool = True
    schedule_tick_seconds: int = 20
    schedule_max_concurrent_runs: int = 2
    schedule_run_timeout_seconds: int = 1800
    schedule_sandbox_run_timeout_seconds: int = 3600
    schedule_runs_keep: int = 20

    # Authentication. users_file maps usernames to pbkdf2 hashes (generate
    # entries with `uv run python -m gallery.passwd <username>`). Set
    # secret_key in production so sessions survive gateway restarts.
    users_file: Path = Path("users.yaml")
    secret_key: str | None = None
    session_max_age_seconds: int = 8 * 3600


settings = Settings()
