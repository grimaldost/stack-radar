"""A repository inside a catalogue never runs git configuration of its own.

A pull request can commit a directory laid out as a bare git repository whose `config`
names a program in `core.fsmonitor`, and a nested `.git` with such a config can exist on
disk (git never tracks a `.git` path component, but an untracked clone carries one). git,
discovering either through `-C <path>`, would run the program while listing files.
`radar gate`, `radar redact-backfill` and `radar changelog-gate` are the verbs an operator
runs on an unreviewed branch, and the docs call the gate read-only, so none of them may
run it.

The marker these tests watch for is written by the fsmonitor command the planted
repository names. It must never appear.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from engine_run import Catalogue, entry, git


def plant_embedded_repo(cat: Catalogue, where: Path, marker: Path, *, nested: bool) -> None:
    """Plant a directory laid out as a git repository, carrying an fsmonitor command.

    `nested` puts the layout under a `.git` subdirectory (so `<where>/.git` exists, on disk
    only, since git does not commit it); otherwise the git files sit directly in `where`
    (a bare layout, no `.git`, committed with the rest of the catalogue). The config names
    an fsmonitor command that writes `marker` and declares a worktree, which is what a read
    such as `git ls-files --others` would otherwise run.
    """
    seed = cat.base / "seed"
    seed.mkdir(parents=True)
    git(seed, cat.home, "init", "-q")
    (seed / "f").write_text("x", encoding="utf-8")
    git(seed, cat.home, "add", "-A")
    git(seed, cat.home, "commit", "-q", "-m", "seed")
    gitdir = (where / ".git") if nested else where
    gitdir.mkdir(parents=True)
    for item in (seed / ".git").iterdir():
        dest = gitdir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    worktree = "../../.." if nested else "../.."
    (gitdir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tbare = false\n"
        f"\tworktree = {worktree}\n"
        f'\tfsmonitor = "echo pwned > {marker.as_posix()}; false"\n',
        encoding="utf-8",
    )


def own(name: str, **top: str) -> str:
    return entry(
        name,
        ring="own",
        top={"visibility": "private", **top},
        install={"kind": "repo", "check": "git status"},
    )


def catalogue_with_embedded_repo(tmp_path: Path, *, nested: bool) -> tuple[Catalogue, Path]:
    cat = Catalogue(tmp_path)
    marker = cat.base / "FSMONITOR-RAN"
    plant_embedded_repo(cat, cat.root / "vendor" / "evil", marker, nested=nested)
    # The embedded repo is referenced three ways a catalogue can name a tree to enumerate.
    cat.tool("by-repo", own("by-repo", repo="vendor/evil"))
    cat.tool(
        "by-feedback",
        own("by-feedback", repo="https://example.invalid/x")
        + '\n[feedback]\nworktree = "vendor/evil"\n',
    )
    marker_text = (cat.root / "radar.toml").read_text(encoding="utf-8")
    (cat.root / "radar.toml").write_text(
        marker_text + '\n[publish]\nframework_worktree = "vendor/evil"\n', encoding="utf-8"
    )
    cat.commit()
    return cat, marker


def _assert_no_marker(cat: Catalogue, marker: Path) -> None:
    cat.run("gate")
    cat.run("redact-backfill")
    cat.run("changelog-gate", "HEAD")
    assert not marker.exists(), "a verb ran git configuration committed inside the catalogue"


def test_a_bare_layout_committed_in_the_catalogue_runs_no_fsmonitor(tmp_path: Path):
    cat, marker = catalogue_with_embedded_repo(tmp_path, nested=False)
    _assert_no_marker(cat, marker)


def test_a_nested_git_dir_committed_in_the_catalogue_runs_no_fsmonitor(tmp_path: Path):
    cat, marker = catalogue_with_embedded_repo(tmp_path, nested=True)
    _assert_no_marker(cat, marker)


def test_an_absolute_profile_path_into_the_catalogue_runs_no_fsmonitor(tmp_path: Path):
    # A profile is committed catalogue content a pull request controls, so `{documents}`
    # can expand to an ABSOLUTE path inside the catalogue, which the relative-path refusal
    # does not catch. The git call that then enumerates it must still run none of the
    # repository's own configuration - this is what GIT_SAFE_CONFIG and the `.git` check
    # on git_ls_files are for.
    cat = Catalogue(tmp_path)
    marker = cat.base / "FSMONITOR-RAN"
    vendor = cat.root / "vendor"
    plant_embedded_repo(cat, vendor / "evil", marker, nested=True)
    cat.profile(
        'name = "default"\nrings = ["own", "adopt", "pilot"]\n\n[paths]\n'
        f'claude_home = "{cat.claude_home.as_posix()}"\n'
        f'documents = "{vendor.as_posix()}"\n'
    )
    cat.tool("by-abs", own("by-abs", repo="{documents}/evil"))
    cat.commit()
    _assert_no_marker(cat, marker)
