"""Temporary and cached file storage for gallery notebooks.

Layout under the storage root (``$GALLERY_STORAGE_ROOT``, default ``./data``):

    scratch/<app>/<session>/   per-session scratch space, removed on process reap
    uploads/<app>/             user-uploaded files
    cache/<namespace>/         cross-session disk cache (or Redis when configured)

Kubernetes mapping: mount ``scratch/`` and ``uploads/`` on an emptyDir (with a
sizeLimit); back ``cache/`` with a PVC, or set ``REDIS_URL`` and skip the disk
cache entirely.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SESSION_ID = uuid.uuid4().hex[:12]


def storage_root() -> Path:
    root = Path(os.environ.get("GALLERY_STORAGE_ROOT", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _app_name(app: str | None) -> str:
    name = app or os.environ.get("MARIMO_GALLERY_APP") or "_local"
    return _sanitize(name)


def _sanitize(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^\w.\- ]", "_", name).strip(" .")
    return name or "unnamed"


def scratch_dir(app: str | None = None, session: str | None = None) -> Path:
    """A per-session scratch directory. Contents may vanish when the notebook
    process is reaped, so treat it as ephemeral."""
    path = storage_root() / "scratch" / _app_name(app) / (session or _SESSION_ID)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_upload(filename: str, data: bytes, app: str | None = None) -> Path:
    """Persist an uploaded file under uploads/<app>/ with a sanitized,
    collision-safe name, and return the final path."""
    directory = storage_root() / "uploads" / _app_name(app)
    directory.mkdir(parents=True, exist_ok=True)
    safe = _sanitize(filename)
    stem, dot, suffix = safe.partition(".")
    path = directory / safe
    counter = 1
    while path.exists():
        path = directory / f"{stem}-{counter}{dot}{suffix}"
        counter += 1
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return path


def cleanup_scratch(app: str, older_than_s: int | None = None) -> None:
    """Remove scratch space for an app: everything, or only sessions whose
    directories are older than ``older_than_s`` seconds."""
    base = storage_root() / "scratch" / _app_name(app)
    if not base.exists():
        return
    if older_than_s is None:
        shutil.rmtree(base, ignore_errors=True)
        return
    cutoff = time.time() - older_than_s
    for session in base.iterdir():
        if session.is_dir() and session.stat().st_mtime < cutoff:
            shutil.rmtree(session, ignore_errors=True)


class DiskCache:
    """File-backed cache with optional TTL. Values are stored atomically;
    keys are hashed so any string is a valid key."""

    def __init__(self, namespace: str = "_shared", root: Path | None = None) -> None:
        self.namespace = _sanitize(namespace)
        self.root = (root or storage_root() / "cache") / self.namespace
        self.root.mkdir(parents=True, exist_ok=True)

    backend = "disk"

    def _path(self, key: str) -> Path:
        return self.root / hashlib.sha256(key.encode()).hexdigest()

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            expires_raw = (path.with_suffix(".ttl")).read_text()
            if float(expires_raw) < time.time():
                path.unlink(missing_ok=True)
                path.with_suffix(".ttl").unlink(missing_ok=True)
                return None
        except FileNotFoundError:
            pass
        except ValueError:
            logger.warning("corrupt ttl sidecar for %s", key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        path = self._path(key)
        fd, tmp = tempfile.mkstemp(dir=self.root)
        with os.fdopen(fd, "wb") as fh:
            fh.write(value)
        os.replace(tmp, path)
        if ttl is not None:
            path.with_suffix(".ttl").write_text(str(time.time() + ttl))
        else:
            path.with_suffix(".ttl").unlink(missing_ok=True)

    def get_or_compute(
        self, key: str, fn: Callable[[], Any], ttl: int | None = None
    ) -> Any:
        raw = self.get(key)
        if raw is not None:
            try:
                return pickle.loads(raw)
            except Exception:
                logger.warning("cache entry for %r is unreadable; recomputing", key)
        value = fn()
        self.set(key, pickle.dumps(value), ttl=ttl)
        return value


class RedisCache:
    """Redis-backed cache with the same interface as DiskCache. Any Redis
    error degrades to a cache miss / no-op with a warning."""

    backend = "redis"

    def __init__(self, url: str, namespace: str = "_shared") -> None:
        import redis

        self.namespace = namespace
        self._client = redis.Redis.from_url(url)

    def _key(self, key: str) -> str:
        return f"gallery:{self.namespace}:{key}"

    def get(self, key: str) -> bytes | None:
        try:
            return self._client.get(self._key(key))
        except Exception as exc:
            logger.warning("redis get failed (%s); treating as miss", exc)
            return None

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        try:
            self._client.set(self._key(key), value, ex=ttl)
        except Exception as exc:
            logger.warning("redis set failed (%s); value not cached", exc)

    def get_or_compute(
        self, key: str, fn: Callable[[], Any], ttl: int | None = None
    ) -> Any:
        raw = self.get(key)
        if raw is not None:
            try:
                return pickle.loads(raw)
            except Exception:
                logger.warning("cache entry for %r is unreadable; recomputing", key)
        value = fn()
        self.set(key, pickle.dumps(value), ttl=ttl)
        return value


class NullCache:
    """A cache that never stores anything."""

    backend = "null"

    def get(self, key: str) -> bytes | None:
        return None

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        pass

    def get_or_compute(
        self, key: str, fn: Callable[[], Any], ttl: int | None = None
    ) -> Any:
        return fn()


def get_cache(namespace: str = "_shared") -> DiskCache | RedisCache:
    """RedisCache when ``$REDIS_URL`` is set, importable, and reachable;
    otherwise DiskCache."""
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            cache = RedisCache(url, namespace=namespace)
            cache._client.ping()
            return cache
        except Exception as exc:
            logger.warning("REDIS_URL set but unusable (%s); using disk cache", exc)
    return DiskCache(namespace=namespace)
