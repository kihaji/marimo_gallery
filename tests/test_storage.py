import time

import pytest

from gallery_shared import storage


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GALLERY_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    return tmp_path


def test_scratch_dir_layout(isolated_root, monkeypatch):
    monkeypatch.setenv("MARIMO_GALLERY_APP", "my-app")
    path = storage.scratch_dir(session="abc")
    assert path == isolated_root / "scratch" / "my-app" / "abc"
    assert path.is_dir()


def test_save_upload_sanitizes_and_avoids_collisions(isolated_root):
    p1 = storage.save_upload("../../etc/passwd", b"one", app="csv")
    assert p1.parent == isolated_root / "uploads" / "csv"
    assert p1.name == "passwd"
    p2 = storage.save_upload("data.csv", b"a", app="csv")
    p3 = storage.save_upload("data.csv", b"b", app="csv")
    assert p2 != p3
    assert p3.read_bytes() == b"b"
    weird = storage.save_upload("we?ird/na|me.c$v", b"x", app="csv")
    assert weird.exists()


def test_cleanup_scratch(isolated_root):
    path = storage.scratch_dir(app="app1", session="s1")
    (path / "f.txt").write_text("x")
    storage.cleanup_scratch("app1")
    assert not path.exists()


def test_disk_cache_roundtrip_and_ttl(isolated_root):
    cache = storage.DiskCache("t")
    assert cache.get("missing") is None
    cache.set("k", b"v")
    assert cache.get("k") == b"v"
    cache.set("expiring", b"v", ttl=1)
    assert cache.get("expiring") == b"v"
    # simulate expiry by rewriting the sidecar into the past
    sidecar = cache._path("expiring").with_suffix(".ttl")
    sidecar.write_text(str(time.time() - 10))
    assert cache.get("expiring") is None


def test_disk_cache_get_or_compute(isolated_root):
    cache = storage.DiskCache("t")
    calls = []

    def build():
        calls.append(1)
        return {"answer": 42}

    assert cache.get_or_compute("key", build) == {"answer": 42}
    assert cache.get_or_compute("key", build) == {"answer": 42}
    assert len(calls) == 1


def test_get_cache_falls_back_to_disk_without_redis(isolated_root, monkeypatch):
    cache = storage.get_cache("ns")
    assert cache.backend == "disk"
    # unreachable redis degrades to disk with a warning, not an exception
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    cache = storage.get_cache("ns")
    assert cache.backend == "disk"


def test_null_cache():
    cache = storage.NullCache()
    cache.set("k", b"v")
    assert cache.get("k") is None
    assert cache.get_or_compute("k", lambda: 5) == 5
