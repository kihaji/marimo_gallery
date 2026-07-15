from pathlib import Path

from gallery.registry import Registry


def make_notebook(root: Path, slug: str, meta: str, thumbnail: bool = False) -> Path:
    d = root / slug
    d.mkdir(parents=True)
    (d / "app.py").write_text("import marimo\napp = marimo.App()\n")
    (d / "meta.yaml").write_text(meta)
    if thumbnail:
        (d / "thumbnail.png").write_bytes(b"\x89PNG")
    return d


GOOD = """
title: Demo
description: A demo notebook
tags: [demo, test]
"""


def test_scan_finds_valid_notebooks(tmp_path):
    make_notebook(tmp_path, "demo-app", GOOD, thumbnail=True)
    reg = Registry(tmp_path)
    found = reg.scan()
    assert list(found) == ["demo-app"]
    meta = found["demo-app"]
    assert meta.title == "Demo"
    assert meta.thumbnail_path is not None
    assert meta.sandbox is False
    assert meta.summary()["url"] == "/apps/demo-app/"


def test_scan_skips_broken_entries(tmp_path):
    make_notebook(tmp_path, "good", GOOD)
    make_notebook(tmp_path, "no-tags", "title: X\ndescription: Y\ntags: []\n")
    make_notebook(tmp_path, "bad-yaml", "title: [unclosed\n")
    make_notebook(tmp_path, "Bad_Slug", GOOD)
    (tmp_path / "not-a-dir").write_text("x")
    missing_meta = tmp_path / "no-meta"
    missing_meta.mkdir()
    (missing_meta / "app.py").write_text("x")
    found = Registry(tmp_path).scan()
    assert list(found) == ["good"]


def test_scan_respects_enabled_and_options(tmp_path):
    make_notebook(tmp_path, "hidden", GOOD + "enabled: false\n")
    make_notebook(tmp_path, "sandboxed", GOOD + "sandbox: true\nsession_ttl: 60\n")
    found = Registry(tmp_path).scan()
    assert "hidden" not in found
    assert found["sandboxed"].sandbox is True
    assert found["sandboxed"].session_ttl == 60


def test_scan_missing_dir(tmp_path):
    found = Registry(tmp_path / "nope").scan()
    assert found == {}


PARAMS = """
parameters:
  - name: region
    label: Region
    type: choice
    default: West
    choices: [North, South, East, West]
  - name: days
    type: number
    default: 90
  - name: verbose
    type: boolean
    default: false
"""


def test_scan_parses_parameters(tmp_path):
    make_notebook(tmp_path, "with-params", GOOD + PARAMS)
    meta = Registry(tmp_path).scan()["with-params"]
    assert [p.name for p in meta.parameters] == ["region", "days", "verbose"]
    assert meta.parameters[0].choices == ["North", "South", "East", "West"]
    assert meta.parameters[1].default == 90
    assert meta.summary()["schedules_url"] == "/schedules/with-params"


def test_scan_skips_invalid_parameters(tmp_path):
    make_notebook(tmp_path, "good", GOOD)
    make_notebook(
        tmp_path,
        "bad-choice",
        GOOD + "parameters:\n  - name: x\n    type: choice\n    choices: []\n",
    )
    make_notebook(
        tmp_path,
        "bad-name",
        GOOD + "parameters:\n  - name: --evil\n",
    )
    make_notebook(
        tmp_path,
        "bad-default",
        GOOD + "parameters:\n  - name: n\n    type: number\n    default: hello\n",
    )
    assert list(Registry(tmp_path).scan()) == ["good"]
