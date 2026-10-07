"""`snapshot.py --help` must never start a refresh.

Without an argument parser in `main()`, `--help` is not a flag the script recognises, so
it falls straight through to `begin_command()` and then into the loop that shells out to
`gh api` and hits the network for every tool in the catalogue, writing a fresh
`snapshots/<today>.json` on the way.

`begin_command` is the function monkeypatched to raise below: it is the first
side-effecting call `main()` makes, so proving it was never reached proves the network
calls and the disk write after it never ran either. A `main()` with no parser reaches it
on `--help` exactly like no arguments would - nothing stops it, prints usage or exits 0
first - so these tests fail against one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from engine_run import fake_program

from stack_radar import snapshot


def _forbid_begin_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "begin_command() ran - snapshot.main() did work before parsing its arguments"
        )

    monkeypatch.setattr(snapshot, "begin_command", _boom)


def test_help_exits_zero_prints_usage_and_never_touches_begin_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_begin_command(monkeypatch)
    monkeypatch.setattr("sys.argv", ["snapshot.py", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        snapshot.main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()


def test_an_unknown_argument_exits_2_without_touching_begin_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_begin_command(monkeypatch)
    monkeypatch.setattr("sys.argv", ["snapshot.py", "--not-a-real-flag"])

    with pytest.raises(SystemExit) as excinfo:
        snapshot.main()

    assert excinfo.value.code == 2


def test_data_root_still_reaches_begin_command_as_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--data-root` keeps resolving the same way it always did - through the override
    argument `begin_command` accepts - rather than falling back to the resolver's own
    argv sniffing now that a real parser declares the flag."""
    seen: dict[str, object] = {}

    def _record(override: str | None = None, **kwargs: object):
        seen["override"] = override
        raise SystemExit(0)

    monkeypatch.setattr(snapshot, "begin_command", _record)
    monkeypatch.setattr("sys.argv", ["snapshot.py", "--data-root", "C:/somewhere"])

    with pytest.raises(SystemExit):
        snapshot.main()

    assert seen["override"] == "C:/somewhere"


def test_the_snapshot_file_is_written_with_lf_line_endings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """snapshots/ is tracked, so CRLF output would show the file as modified on Windows
    even when no figure moved. Checked on the bytes, on every OS."""
    import datetime

    monkeypatch.setattr(snapshot, "begin_command", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(snapshot, "snap_dir", lambda root=None: tmp_path)
    monkeypatch.setattr(
        snapshot,
        "load_tools",
        lambda *_a, **_kw: [{"name": "widget", "repo": "https://github.com/example/widget"}],
    )
    monkeypatch.setattr(snapshot, "latest_snapshot", lambda root=None: (None, {}))
    monkeypatch.setattr(snapshot, "gh_repo", lambda slug: {"stars": 1, "license": "MIT"})
    monkeypatch.setattr("sys.argv", ["snapshot.py"])

    snapshot.main()

    data = (tmp_path / f"{datetime.date.today().isoformat()}.json").read_bytes()
    assert b"\n" in data
    assert b"\r" not in data


def test_a_machine_local_entry_never_reaches_the_tracked_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """snapshots/ is tracked, and a tools.local/ entry exists precisely so that its real
    name and repository stay out of tracked files. Read through the real loader, so the
    test holds whatever filter the loader applies rather than what a stub pretends."""
    import datetime

    from stack_radar import radar_lib

    root = tmp_path / "catalogue"
    (root / "tools").mkdir(parents=True)
    (root / "tools.local").mkdir()
    (root / "tools" / "widget.toml").write_text(
        'name = "widget"\nrepo = "https://github.com/example/widget"\n', encoding="utf-8"
    )
    (root / "tools.local" / "secret-tool.toml").write_text(
        'name = "secret-tool"\nrepo = "https://github.com/example-private/secret-tool"\n'
        'redact_as = "local-tool-1"\n',
        encoding="utf-8",
    )
    looked_up: list[str] = []

    def _gh(slug: str) -> dict:
        looked_up.append(slug)
        return {"stars": 1, "license": "MIT"}

    monkeypatch.setattr(snapshot, "begin_command", lambda *_a, **_kw: root)
    monkeypatch.setattr(
        snapshot,
        "load_tools",
        lambda include_local=True: radar_lib.load_tools(include_local=include_local, root=root),
    )
    monkeypatch.setattr(snapshot, "latest_snapshot", lambda r=None: (None, {}))
    monkeypatch.setattr(snapshot, "gh_repo", _gh)
    monkeypatch.setattr("sys.argv", ["snapshot.py"])

    snapshot.main()

    written = root / "snapshots" / f"{datetime.date.today().isoformat()}.json"
    text = written.read_text(encoding="utf-8")
    assert "widget" in text
    assert "secret-tool" not in text
    assert "example-private" not in text
    assert "local-tool-1" not in text
    # Not even looked up: the lookup is where the name leaves the machine in the first place.
    assert looked_up == ["example/widget"]
    assert "1 machine-local entry not measured" in capsys.readouterr().out


# ------------------------------------------------------------------ repository facts


def _refresh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools: list[dict],
    prev: dict,
    gh=None,
    downloads=None,
    prev_name: str = "2026-08-01.json",
) -> dict:
    """One refresh with every network call stubbed; returns the written snapshot."""
    import datetime
    import json

    def _no_gh(slug: str) -> dict:
        raise AssertionError(f"gh_repo called for {slug}")

    def _no_downloads(registry: str):
        raise AssertionError(f"downloads called for {registry}")

    monkeypatch.setattr(snapshot, "begin_command", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(snapshot, "snap_dir", lambda r=None: tmp_path)
    monkeypatch.setattr(snapshot, "load_tools", lambda *_a, **_kw: tools)
    monkeypatch.setattr(snapshot, "latest_snapshot", lambda r=None: (prev_name, prev))
    monkeypatch.setattr(snapshot, "gh_repo", gh or _no_gh)
    monkeypatch.setattr(snapshot, "downloads", downloads or _no_downloads)
    monkeypatch.setattr("sys.argv", ["snapshot.py"])
    snapshot.main()
    today = datetime.date.today().isoformat()
    return json.loads((tmp_path / f"{today}.json").read_text(encoding="utf-8"))


GONE = {"repo_status": 404, "repo_error": "gh: Not Found (HTTP 404)"}
KNOWN = {
    "stars": 9000,
    "watchers": 40,
    "pushed": "2026-07-30",
    "archived": False,
    "license": "MIT",
}


def test_a_failed_lookup_keeps_the_last_good_repository_facts(tmp_path, monkeypatch):
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget"}],
        prev={"widget": dict(KNOWN)},
        gh=lambda slug: dict(GONE),
    )
    w = out["widget"]
    for key, value in KNOWN.items():
        assert w[key] == value, key
    assert w["repo_stale"] is True
    assert w["repo_as_of"] == "2026-08-01"
    assert w["repo_status"] == 404
    assert "error" not in w


def test_a_failed_lookup_with_nothing_to_keep_records_only_the_status(tmp_path, monkeypatch):
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget"}],
        prev={},
        gh=lambda slug: dict(GONE),
    )
    assert out["widget"] == GONE


def test_facts_carried_twice_keep_their_original_date(tmp_path, monkeypatch):
    stale = dict(KNOWN, repo_stale=True, repo_as_of="2026-06-01", repo_status=404)
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget"}],
        prev={"widget": stale},
        gh=lambda slug: dict(GONE),
    )
    assert out["widget"]["repo_as_of"] == "2026-06-01"


def test_a_good_lookup_clears_the_stale_marker(tmp_path, monkeypatch):
    stale = dict(KNOWN, repo_stale=True, repo_as_of="2026-06-01", repo_status=404)
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget.git"}],
        prev={"widget": stale},
        gh=lambda slug: dict(KNOWN, stars=9100) if slug == "example/widget" else {},
    )
    assert out["widget"] == dict(KNOWN, stars=9100, repo_slug="example/widget")


def gh_on_path(tmp_path: Path, monkeypatch) -> None:
    """A `gh` on PATH, so the lookup that precedes every run finds one; the tests below
    replace `subprocess.run`, so it never runs."""
    fake_program(tmp_path / "bin", "gh", "")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))


def test_gh_repo_reports_the_http_status_instead_of_an_error_string(tmp_path, monkeypatch):
    import subprocess

    gh_on_path(tmp_path, monkeypatch)

    def _gh_404(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(snapshot.subprocess, "run", _gh_404)
    assert snapshot.gh_repo("example/widget") == {
        "repo_status": 404,
        "repo_error": "gh: Not Found (HTTP 404)",
    }


def test_gh_repo_decodes_output_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    import subprocess

    gh_on_path(tmp_path, monkeypatch)

    seen: dict = {}

    def _gh(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, '{"stars": 1, "license": "MIT"}', "")

    monkeypatch.setattr(snapshot.subprocess, "run", _gh)
    assert snapshot.gh_repo("example/widget") == {"stars": 1, "license": "MIT"}
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


def test_a_repository_without_a_licence_is_recorded_as_none(tmp_path, monkeypatch):
    import subprocess

    gh_on_path(tmp_path, monkeypatch)

    def _gh(argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, '{"stars": 1, "license": null}', "")

    monkeypatch.setattr(snapshot.subprocess, "run", _gh)
    assert snapshot.gh_repo("example/widget") == {"stars": 1, "license": "none"}


def test_one_undecodable_lookup_does_not_abort_the_refresh(tmp_path, monkeypatch):
    import datetime
    import json
    import subprocess

    def _gh(argv, **_kw):
        if "repos/example/broken" in argv:
            raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "undefined")
        return subprocess.CompletedProcess(argv, 0, '{"stars": 2, "license": "MIT"}', "")

    gh_on_path(tmp_path, monkeypatch)
    monkeypatch.setattr(snapshot.subprocess, "run", _gh)
    monkeypatch.setattr(snapshot, "begin_command", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(snapshot, "snap_dir", lambda r=None: tmp_path)
    monkeypatch.setattr(
        snapshot,
        "load_tools",
        lambda *_a, **_kw: [
            {"name": "broken", "repo": "https://github.com/example/broken"},
            {"name": "widget", "repo": "https://github.com/example/widget"},
        ],
    )
    monkeypatch.setattr(snapshot, "latest_snapshot", lambda r=None: (None, {}))
    monkeypatch.setattr("sys.argv", ["snapshot.py"])
    snapshot.main()
    today = datetime.date.today().isoformat()
    out = json.loads((tmp_path / f"{today}.json").read_text(encoding="utf-8"))
    assert out["broken"] == {"repo_status": "error", "repo_error": "UnicodeDecodeError"}
    assert out["widget"] == {"stars": 2, "license": "MIT", "repo_slug": "example/widget"}


def test_gh_repo_refuses_a_slug_outside_the_owner_name_alphabet(monkeypatch):
    def _never(*_a, **_kw):
        raise AssertionError("gh ran on an unvalidated slug")

    monkeypatch.setattr(snapshot.subprocess, "run", _never)
    for slug in ("example/widget;id", "example/../other", "-/x y", "example"):
        got = snapshot.gh_repo(slug)
        assert got["repo_status"] == "invalid-slug", slug


# ------------------------------------------------------------------ github: registry


def test_a_github_registry_makes_no_registry_call_and_records_no_downloads(tmp_path, monkeypatch):
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[
            {
                "name": "widget",
                "repo": "https://github.com/example/widget",
                "registry": "github:example/widget",
            }
        ],
        prev={},
        gh=lambda slug: dict(KNOWN),
    )
    assert out["widget"] == dict(KNOWN, repo_slug="example/widget")
    assert not [k for k in out["widget"] if k.startswith("downloads")]


def test_a_github_registry_is_not_a_failed_lookup(tmp_path, monkeypatch, capsys):
    """A figure measured earlier is dropped, not carried forward as if a lookup failed:
    there is no download count behind a git-tag registry to be stale against."""
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[
            {
                "name": "widget",
                "repo": "https://github.com/example/widget",
                "registry": "github:example/widget",
            }
        ],
        prev={"widget": dict(KNOWN, downloads_month=500, downloads_as_of="2026-07-01")},
        gh=lambda slug: dict(KNOWN),
    )
    assert "downloads_month" not in out["widget"]
    assert "downloads_stale" not in out["widget"]
    printed = capsys.readouterr().out
    assert "registry lookup failed" not in printed
    assert "git tags" in printed


# ------------------------------------------------------------------ concurrency


def test_lookups_overlap_and_the_output_keeps_catalogue_order(tmp_path, monkeypatch, capsys):
    """The first entry's lookup cannot finish until the second one has started, which a
    one-at-a-time loop never allows; the printed lines still come out in catalogue order."""
    import threading

    second_started = threading.Event()

    def _gh(slug: str) -> dict:
        if slug == "example/first":
            if not second_started.wait(timeout=5):
                return {"repo_status": "error", "repo_error": "lookups ran one at a time"}
            return {"stars": 1}
        second_started.set()
        return {"stars": 2}

    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[
            {"name": "first", "repo": "https://github.com/example/first"},
            {"name": "second", "repo": "https://github.com/example/second"},
        ],
        prev={},
        gh=_gh,
    )
    assert out == {
        "first": {"stars": 1, "repo_slug": "example/first"},
        "second": {"stars": 2, "repo_slug": "example/second"},
    }
    printed = capsys.readouterr().out
    assert printed.index("first: ") < printed.index("second: ")


def test_facts_measured_under_another_repository_are_not_carried(tmp_path, monkeypatch):
    # The entry's `repo` was corrected to a different repository and the new lookup
    # failed: the old repository's stars, licence and push date are not this entry's.
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget-renamed"}],
        prev={"widget": dict(KNOWN, repo_slug="example/widget")},
        gh=lambda slug: dict(GONE),
    )
    assert out["widget"] == GONE


def test_facts_measured_under_the_same_repository_are_carried(tmp_path, monkeypatch):
    out = _refresh(
        tmp_path,
        monkeypatch,
        tools=[{"name": "widget", "repo": "https://github.com/example/widget"}],
        prev={"widget": dict(KNOWN, repo_slug="example/widget")},
        gh=lambda slug: dict(GONE),
    )
    assert out["widget"]["repo_stale"] is True
    assert out["widget"]["stars"] == KNOWN["stars"]
    assert out["widget"]["repo_slug"] == "example/widget"


def test_a_slug_outside_github_s_alphabet_is_never_handed_to_gh(monkeypatch):
    def _no_run(*_a, **_kw):
        raise AssertionError("gh was run")

    monkeypatch.setattr(snapshot.subprocess, "run", _no_run)
    for slug in ("example/widget\n", "-x/widget", "example/..", "example/a b"):
        assert snapshot.gh_repo(slug)["repo_status"] == "invalid-slug", slug
