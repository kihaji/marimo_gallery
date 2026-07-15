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

    redis_url: str | None = None


settings = Settings()
