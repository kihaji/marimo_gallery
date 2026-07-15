"""Scan the notebooks directory into a registry of gallery entries.

Each notebook lives in its own directory:

    notebooks/<slug>/app.py         the marimo notebook
    notebooks/<slug>/meta.yaml      title/description/tags/options
    notebooks/<slug>/thumbnail.png  optional card image

A broken or incomplete entry is logged and skipped; it never takes the
gateway down.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

ParamType = Literal["string", "number", "boolean", "choice"]


class NotebookParameter(BaseModel):
    """A scheduling parameter a notebook accepts via mo.cli_args()."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str | None = None
    type: ParamType = "string"
    default: str | int | float | bool | None = None
    choices: list[str] | None = None

    @model_validator(mode="after")
    def _check(self) -> "NotebookParameter":
        if self.type == "choice":
            if not self.choices:
                raise ValueError(f"parameter {self.name!r}: choice type needs choices")
            if self.default is not None and str(self.default) not in self.choices:
                raise ValueError(f"parameter {self.name!r}: default not in choices")
        elif self.choices is not None:
            raise ValueError(f"parameter {self.name!r}: choices only valid for choice type")
        if self.default is not None:
            if self.type == "number" and isinstance(self.default, (bool, str)):
                raise ValueError(f"parameter {self.name!r}: default must be a number")
            if self.type == "boolean" and not isinstance(self.default, bool):
                raise ValueError(f"parameter {self.name!r}: default must be a boolean")
        return self


class NotebookMeta(BaseModel):
    slug: str
    title: str
    description: str
    tags: list[str] = Field(min_length=1)
    sandbox: bool = False
    include_code: bool = False
    requires_login: bool = False
    session_ttl: int | None = None
    enabled: bool = True
    thumbnail: str = "thumbnail.png"
    parameters: list[NotebookParameter] = Field(default_factory=list)

    app_path: Path
    thumbnail_path: Path | None = None
    mtime: float = 0.0

    def summary(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "sandbox": self.sandbox,
            "requires_login": self.requires_login,
            "mtime": self.mtime,
            "has_thumbnail": self.thumbnail_path is not None,
            "url": f"/apps/{self.slug}/",
            "schedules_url": f"/schedules/{self.slug}",
        }


class Registry:
    def __init__(self, notebooks_dir: Path) -> None:
        self.notebooks_dir = notebooks_dir
        self.notebooks: dict[str, NotebookMeta] = {}

    def scan(self) -> dict[str, NotebookMeta]:
        found: dict[str, NotebookMeta] = {}
        if not self.notebooks_dir.is_dir():
            logger.warning("notebooks directory %s does not exist", self.notebooks_dir)
            self.notebooks = found
            return found
        for entry in sorted(self.notebooks_dir.iterdir()):
            meta = self._load_entry(entry)
            if meta is not None and meta.enabled:
                found[meta.slug] = meta
        self.notebooks = found
        logger.info("registry: %d notebook(s): %s", len(found), ", ".join(found))
        return found

    def _load_entry(self, entry: Path) -> NotebookMeta | None:
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            return None
        slug = entry.name
        if not SLUG_RE.match(slug):
            logger.warning("skipping %s: directory name is not a valid slug", entry)
            return None
        app_path = entry / "app.py"
        meta_path = entry / "meta.yaml"
        if not app_path.is_file() or not meta_path.is_file():
            logger.warning("skipping %s: needs both app.py and meta.yaml", entry)
            return None
        try:
            raw = yaml.safe_load(meta_path.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError("meta.yaml must be a mapping")
            raw.pop("slug", None)
            raw.pop("app_path", None)
            raw.pop("thumbnail_path", None)
            meta = NotebookMeta(slug=slug, app_path=app_path, **raw)
        except (yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
            logger.warning("skipping %s: invalid meta.yaml: %s", entry, exc)
            return None
        thumb = entry / meta.thumbnail
        meta.thumbnail_path = thumb if thumb.is_file() else None
        meta.mtime = app_path.stat().st_mtime
        return meta
