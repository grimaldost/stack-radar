"""A catalogue declares the axes its entries may use; the default set applies otherwise.

`axis` says which problem a tool addresses, and the engine's default set is small and
general. A catalogue that wants axes of its own declares them in radar.toml
as `[entries].axes`, and that list replaces the default everywhere an axis is judged or
shown: the gate's schema check, `radar add --axis` and its help, and the rendered page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine_run import Catalogue, entry

DECLARED = ["build", "lint-format", "agent-plugins"]


def declare(cat: Catalogue, axes: object) -> None:
    marker = cat.root / "radar.toml"
    marker.write_text(
        marker.read_text(encoding="utf-8") + f"\n[entries]\naxes = {json.dumps(axes)}\n",
        encoding="utf-8",
    )


def with_axis(name: str, axis: str) -> str:
    return entry(name, ring="observe", top={"axis": axis})


def add(cat: Catalogue, name: str, axis: str):
    return cat.run(
        "cli",
        "add",
        name,
        "--repo",
        f"https://example.invalid/{name}",
        "--axis",
        axis,
        "--ring",
        "observe",
        "--license",
        "MIT",
        "--visibility",
        "public",
        "--artifact",
        "cli",
        "--note",
        "a synthetic entry",
        "--reason",
        "a synthetic fixture",
    )


def test_the_gate_judges_an_entry_by_the_declared_axes(tmp_path: Path):
    cat = Catalogue(tmp_path)
    declare(cat, DECLARED)
    cat.tool("ours", with_axis("ours", "lint-format"))
    cat.tool("theirs", with_axis("theirs", "testing"))
    done = cat.run("gate", "--no-data-plane")
    assert "[FAIL] ours:" not in done.stdout, done.stdout
    assert "[FAIL] theirs: bad axis: testing" in done.stdout, done.stdout
    assert "build, lint-format, agent-plugins" in done.stdout, done.stdout


def test_without_a_declaration_the_default_axes_apply(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.tool("widget", with_axis("widget", "testing"))
    cat.tool("other", with_axis("other", "documentation"))
    done = cat.run("gate", "--no-data-plane")
    assert "[FAIL] widget:" not in done.stdout, done.stdout
    assert "[FAIL] other: bad axis: documentation" in done.stdout, done.stdout


def test_a_malformed_declaration_is_a_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    declare(cat, ["Build Tools"])
    done = cat.run("gate", "--no-data-plane")
    assert "[FAIL] radar.toml: [entries].axes holds ['Build Tools']" in done.stdout, done.stdout
    assert done.returncode == 1


def test_add_accepts_a_declared_axis_and_refuses_a_default_one(tmp_path: Path):
    cat = Catalogue(tmp_path)
    declare(cat, DECLARED)
    ours = add(cat, "ours", "agent-plugins")
    assert ours.returncode == 0, ours.stdout + ours.stderr
    assert 'axis = "agent-plugins"' in (cat.root / "tools" / "ours.toml").read_text(
        encoding="utf-8"
    )
    theirs = add(cat, "theirs", "testing")
    assert theirs.returncode == 2, theirs.stdout + theirs.stderr
    assert "invalid choice: 'testing'" in theirs.stderr, theirs.stderr
    assert not (cat.root / "tools" / "theirs.toml").exists()


def test_add_help_lists_the_declared_axes(tmp_path: Path):
    cat = Catalogue(tmp_path)
    declare(cat, DECLARED)
    done = cat.run("cli", "add", "--help")
    flat = " ".join(done.stdout.split())
    assert "build, lint-format, agent-plugins" in flat, done.stdout
    assert "testing" not in flat, done.stdout


def test_the_page_is_laid_out_on_the_declared_axes(tmp_path: Path):
    cat = Catalogue(tmp_path)
    declare(cat, DECLARED)
    cat.tool("ours", with_axis("ours", "build"))
    done = cat.run("render")
    assert done.returncode == 0, done.stdout + done.stderr
    page = (cat.root / "docs" / "radar" / "index.html").read_text(encoding="utf-8")
    shown = re.search(r"const AXES=(\[.*?\]);", page)
    assert shown is not None, page
    assert json.loads(shown.group(1)) == DECLARED


def test_the_default_set_is_a_general_one(tmp_path: Path):
    # Without a declaration, `radar add --help` offers the default set: general axes any
    # stack can file under, which a catalogue replaces with its own in radar.toml.
    cat = Catalogue(tmp_path)
    done = cat.run("cli", "add", "--help")
    # argparse may wrap a line after a hyphen (`mcp-` / `servers`).
    flat = re.sub(r"-\s+", "-", " ".join(done.stdout.split()))
    assert "build, lint-format, testing, agent-plugins, mcp-servers, other" in flat, done.stdout
