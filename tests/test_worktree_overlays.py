"""A catalogue checked out as a linked git worktree reads the main worktree's overlays.

`tools.local/` and `redact.local.toml` are gitignored, so `git worktree add` never copies
them: a second checkout of a catalogue starts without its machine-local entries and its
declared homes, and every command run there would silently lose both. When the data root
is a linked worktree with no copy of its own, the main worktree's copy is read and the
command says so once on stderr.

The layout is written by hand - the `.git` FILE and the `commondir` file - because that is
exactly what the engine reads, and it keeps these tests off a real `git worktree add`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stack_radar import paths, radar_lib
from stack_radar.redact import EXTRA_HOMES_FILE, extra_homes

MARKER = 'schema = "radar-data/v1"\n'

LOCAL_ENTRY = """\
name = "local-widget"
repo = "https://example.invalid/local-widget"
axis = "lint-format"
artifact = "cli"
ring = "observe"
license = "MIT"
visibility = "private"
note = "a machine-local entry"
redact_as = "local-lib"

[[history]]
date = "2026-09-01"
ring = "observe"
reason = "a fixture"
"""


@pytest.fixture(autouse=True)
def fresh_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The note is once per PROCESS; each test is its own process as far as it is concerned.
    monkeypatch.setattr(paths, "_NOTED", set(), raising=False)


def linked(tmp_path: Path, *, relative: bool = False) -> tuple[Path, Path]:
    """A main worktree and one linked worktree of it, both carrying the marker."""
    main = tmp_path / "catalogue"
    wt = tmp_path / "catalogue-review"
    gitdir = main / ".git" / "worktrees" / "catalogue-review"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{(wt / '.git').as_posix()}\n", encoding="utf-8")
    wt.mkdir()
    pointer = "../catalogue/.git/worktrees/catalogue-review" if relative else gitdir.as_posix()
    (wt / ".git").write_text(f"gitdir: {pointer}\n", encoding="utf-8")
    for root in (main, wt):
        (root / "radar.toml").write_text(MARKER, encoding="utf-8")
        (root / "tools").mkdir()
    return main, wt


def local_entry(root: Path) -> None:
    (root / "tools.local").mkdir(exist_ok=True)
    (root / "tools.local" / "local-widget.toml").write_text(LOCAL_ENTRY, encoding="utf-8")


class TestMainWorktree:
    def test_a_linked_worktree_names_its_main_worktree(self, tmp_path):
        main, wt = linked(tmp_path)
        assert paths.main_worktree(wt) == main.resolve()

    def test_a_relative_gitdir_pointer_is_resolved_from_the_worktree(self, tmp_path):
        main, wt = linked(tmp_path, relative=True)
        assert paths.main_worktree(wt) == main.resolve()

    def test_the_main_worktree_itself_is_not_linked(self, tmp_path):
        main, _ = linked(tmp_path)
        assert paths.main_worktree(main) is None

    def test_a_directory_outside_git_is_not_linked(self, tmp_path):
        assert paths.main_worktree(tmp_path) is None

    def test_a_gitdir_without_commondir_is_not_a_worktree(self, tmp_path):
        # A submodule's `.git` file points at a gitdir with no `commondir`.
        main, wt = linked(tmp_path)
        (main / ".git" / "worktrees" / "catalogue-review" / "commondir").unlink()
        assert paths.main_worktree(wt) is None

    def test_a_worktree_of_a_bare_repository_has_no_main_worktree(self, tmp_path):
        # A bare repository's common dir is the repository itself. Its parent is whatever
        # directory the repository sits in, not a checkout, so it holds no overlays and is
        # not the catalogue's own path.
        bare = tmp_path / "catalogue.git"
        wt = tmp_path / "catalogue-review"
        gitdir = bare / "worktrees" / "catalogue-review"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
        assert paths.main_worktree(wt) is None


class TestToolsLocal:
    def test_the_main_worktree_s_entries_are_read_and_the_note_printed_once(self, tmp_path, capsys):
        main, wt = linked(tmp_path)
        local_entry(main)
        tools = radar_lib.load_tools(root=wt)
        radar_lib.load_tools(root=wt)
        assert [t["name"] for t in tools if t["_local"]] == ["local-widget"]
        err = capsys.readouterr().err
        assert err.count("[NOTE] tools.local read from the main worktree") == 1, err
        assert main.resolve().as_posix() in err

    def test_a_worktree_with_its_own_copy_reads_its_own(self, tmp_path, capsys):
        main, wt = linked(tmp_path)
        local_entry(main)
        (wt / "tools.local").mkdir()
        assert paths.tools_local_dir(wt) == wt / "tools.local"
        assert radar_lib.load_tools(root=wt) == []
        assert "[NOTE]" not in capsys.readouterr().err

    def test_with_neither_copy_nothing_changes(self, tmp_path, capsys):
        _, wt = linked(tmp_path)
        assert paths.tools_local_dir(wt) == wt / "tools.local"
        assert radar_lib.load_tools(root=wt) == []
        assert capsys.readouterr().err == ""

    def test_excluding_local_entries_reads_and_prints_nothing(self, tmp_path, capsys):
        main, wt = linked(tmp_path)
        local_entry(main)
        assert radar_lib.load_tools(include_local=False, root=wt) == []
        assert capsys.readouterr().err == ""


class TestDeclaredHomes:
    def test_the_main_worktree_s_declaration_is_read(self, tmp_path, capsys):
        main, wt = linked(tmp_path)
        (main / EXTRA_HOMES_FILE).write_text('homes = ["/home/other"]\n', encoding="utf-8")
        assert [h.as_posix() for h in extra_homes(wt)] == ["/home/other"]
        extra_homes(wt)
        err = capsys.readouterr().err
        assert err.count(f"[NOTE] {EXTRA_HOMES_FILE} read from the main worktree") == 1, err

    def test_a_worktree_with_its_own_declaration_reads_its_own(self, tmp_path, capsys):
        main, wt = linked(tmp_path)
        (main / EXTRA_HOMES_FILE).write_text('homes = ["/home/other"]\n', encoding="utf-8")
        (wt / EXTRA_HOMES_FILE).write_text('homes = ["/home/mine"]\n', encoding="utf-8")
        assert [h.as_posix() for h in extra_homes(wt)] == ["/home/mine"]
        assert capsys.readouterr().err == ""

    def test_with_neither_there_is_no_extra_home(self, tmp_path):
        _, wt = linked(tmp_path)
        assert extra_homes(wt) == []


GATE_MARKER = """\
schema = "radar-data/v1"
title = "mini-radar"
default_environment = "default"
requires_framework = "{requires}"

[invariant]
needles = ["mini-radar"]
"""


class TestTheGateInALinkedWorktree:
    """The main worktree is the same catalogue, so naming it in the data plane is a
    reference to the catalogue and the invariant scan looks for it."""

    @staticmethod
    def link(radar, tmp_path: Path) -> Path:
        from stack_radar import __version__
        from stack_radar.init import compatible_range

        main = tmp_path / "main-checkout"
        gitdir = main / ".git" / "worktrees" / "radar"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        (radar.root / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
        (radar.root / "radar.toml").write_text(
            GATE_MARKER.format(requires=compatible_range(__version__)), encoding="utf-8"
        )
        return main.resolve()

    def test_the_main_worktree_path_is_a_needle(self, radar, tmp_path):
        main = self.link(radar, tmp_path)
        (radar.home / "CLAUDE.md").write_text(
            f"Ring policy: see {main.as_posix()}/README.md\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant: data-plane file references the control plane" in p.stdout
        assert "CLAUDE.md" in p.stdout

    @pytest.mark.parametrize("ending", ["", "/", '"', " ", "\\README.md"])
    def test_the_path_itself_is_matched_whatever_follows_it(self, radar, tmp_path, ending):
        main = self.link(radar, tmp_path)
        (radar.home / "CLAUDE.md").write_text(f"see {main.as_posix()}{ending}", encoding="utf-8")
        p = radar.gate()
        assert "data-plane file references the control plane" in p.stdout, p.stdout

    @pytest.mark.parametrize("which", ["main", "data root"])
    def test_a_sibling_that_only_starts_with_the_path_is_not_a_reference(
        self, radar, tmp_path, which
    ):
        # A path needle ends at a path boundary: `<documents>/catalogue-other` is another
        # directory that happens to share a prefix, not this catalogue.
        main = self.link(radar, tmp_path)
        base = main if which == "main" else radar.root.resolve()
        (radar.home / "CLAUDE.md").write_text(
            f"see {base.as_posix()}-other/x and {base.as_posix()}.bak\n", encoding="utf-8"
        )
        p = radar.gate()
        assert "data-plane file references the control plane" not in p.stdout, p.stdout
