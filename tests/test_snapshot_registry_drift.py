"""A carried-forward download figure must not survive a change to WHICH registry backs
it.

`entry["downloads_source"]` records the registry a figure came from (set beside
`downloads_month` in the fetch branch). The carry-forward branch compares it with the
entry's current `registry` before it copies the previous `downloads_month` forward. A
copy that skipped the comparison would, when an entry declared a wrong `registry`, had a
figure measured under it and then had the registry REMOVED, carry the wrong figure
forward on every refresh after.

An entry that has NEVER declared a registry (any figure it carries is entered by hand)
records no `downloads_source` at all, and keeps carrying its figure forward - there is
nothing to compare it against, and that absence is legal.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from stack_radar import snapshot

TODAY = datetime.date.today().isoformat()


def _run(tmp_path: Path, monkeypatch, *, tools: list[dict], prev: dict) -> dict:
    monkeypatch.setattr(snapshot, "begin_command", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(snapshot, "snap_dir", lambda root=None: tmp_path)
    monkeypatch.setattr(snapshot, "load_tools", lambda *_a, **_kw: tools)
    monkeypatch.setattr(snapshot, "latest_snapshot", lambda root=None: ("prev.json", prev))
    # No entry below declares a github.com repo or a registry that would still need a
    # network call this pass, but pin `downloads`/`gh_repo` shut anyway so a test that
    # regresses that never silently reaches the network.
    monkeypatch.setattr(snapshot, "downloads", lambda reg: (None, reg))
    monkeypatch.setattr(snapshot, "gh_repo", lambda slug: {})
    monkeypatch.setattr("sys.argv", ["snapshot.py"])

    snapshot.main()

    return json.loads((tmp_path / f"{TODAY}.json").read_text(encoding="utf-8"))


def test_a_removed_registry_drops_the_old_figure_instead_of_carrying_it(tmp_path, monkeypatch):
    out = _run(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://example.invalid/widget"}],  # no registry
        prev={
            "widget": {
                "stars": 30000,
                "downloads_month": 250,
                "downloads_source": "npm:a-different-package-entirely",
                "downloads_as_of": "2026-08-01",
            }
        },
    )

    # Nothing else populates the entry in this fixture (no github repo, no fresh
    # registry lookup), so a dropped figure leaves it out of the snapshot entirely -
    # which is `if entry:` at the end of the loop, unrelated to this change and not
    # what is under test here. Either shape proves the figure was not carried.
    assert "downloads_month" not in out.get("widget", {}), out.get("widget")
    assert "downloads_source" not in out.get("widget", {}), out.get("widget")


def test_a_changed_registry_drops_the_old_figure_instead_of_carrying_it(tmp_path, monkeypatch):
    out = _run(
        tmp_path,
        monkeypatch,
        tools=[
            {
                "name": "widget",
                "repo": "https://example.invalid/widget",
                "registry": "pypi:widget",
            }
        ],
        prev={
            "widget": {
                "stars": 9000,
                "downloads_month": 500,
                "downloads_source": "npm:widget",
                "downloads_as_of": "2026-08-01",
            }
        },
    )

    assert "downloads_month" not in out.get("widget", {}), out.get("widget")


def test_an_unchanged_registry_still_carries_the_figure_forward(tmp_path, monkeypatch):
    out = _run(
        tmp_path,
        monkeypatch,
        tools=[
            {
                "name": "widget",
                "repo": "https://example.invalid/widget",
                "registry": "npm:widget",
            }
        ],
        prev={
            "widget": {
                "stars": 9000,
                "downloads_month": 500,
                "downloads_source": "npm:widget",
                "downloads_as_of": "2026-08-01",
            }
        },
    )

    assert out["widget"]["downloads_month"] == 500
    assert out["widget"]["downloads_stale"] is True
    assert out["widget"]["downloads_source"] == "npm:widget"


def test_an_entry_that_never_declared_a_registry_still_carries_its_figure_forward(
    tmp_path, monkeypatch
):
    """No `downloads_source` recorded at all (a hand-measured figure) is not a
    registry mismatch - there is nothing to compare, so the figure is carried forward."""
    out = _run(
        tmp_path,
        monkeypatch,
        tools=[{"name": "gadget", "repo": "https://example.invalid/gadget"}],
        prev={
            "gadget": {
                "stars": 50000,
                "downloads_month": 40000,
                "downloads_as_of": "2026-08-01",
            }
        },
    )

    assert out["gadget"]["downloads_month"] == 40000
    assert out["gadget"]["downloads_stale"] is True
