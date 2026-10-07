"""No file the engine writes into a data root is written through a symbolic link.

A catalogue takes pull requests, and git checks a committed symbolic link out as a link
on Linux and macOS. A link committed in place of README.md, or in place of a directory
such as docs/ or snapshots/, would otherwise carry the next routine `radar render` or
`radar snapshot` to any file the link names, written with the operator's account. So
every write into the data root is refused, with a `[FAIL]` naming the path, when the
target is a link, when a directory between the data root and it is one, or when it
resolves outside the data root; nothing is written in that run.

A directory link is made as a symbolic link where the account may create one, and as a
junction on Windows otherwise, which needs no privilege and redirects a directory the
same way. A file link needs a symbolic link, so those tests are skipped where none can
be made.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from engine_run import Catalogue, entry, run_engine
from test_render_feedback_targets import feedback_tool, render, with_destination
from test_sync_feedback import run_sync, sync_root


def link_dir(link: Path, target: Path) -> None:
    """`link` redirected to the directory `target`: a symbolic link, or a junction."""
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if sys.platform != "win32":
            pytest.skip("this account cannot create symbolic links")
    import _winapi  # noqa: PLC0415 - Windows only

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        pytest.skip("this account can create neither a symbolic link nor a junction")


def link_file(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create symbolic links")


def files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*")) if directory.exists() else []


# ------------------------------------------------------------------------ render


def test_render_does_not_write_through_a_linked_directory(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.tool("widget", entry("widget", ring="observe"))
    outside = tmp_path / "outside"
    link_dir(cat.root / "docs", outside)
    done = cat.run("render")
    assert files_in(outside) == [], "radar render wrote through the docs/ link"
    assert "[FAIL] render:" in done.stdout and "symbolic link" in done.stdout, done.stdout
    # Judged before anything was written: README.md was not written either.
    assert not (cat.root / "README.md").exists(), done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def test_render_does_not_write_through_a_linked_readme(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.tool("widget", entry("widget", ring="observe"))
    victim = tmp_path / "outside" / "authorized_keys"
    victim.parent.mkdir()
    victim.write_text("the operator's own line\n", encoding="utf-8")
    link_file(cat.root / "README.md", victim)
    done = cat.run("render")
    assert victim.read_text(encoding="utf-8") == "the operator's own line\n"
    assert "[FAIL] render:" in done.stdout, done.stdout
    assert done.returncode == 1


# ---------------------------------------------------------------------- snapshot


def test_snapshot_does_not_write_through_a_linked_directory(tmp_path: Path):
    cat = Catalogue(tmp_path)
    outside = tmp_path / "outside"
    link_dir(cat.root / "snapshots", outside)
    done = cat.run("snapshot")
    assert files_in(outside) == [], "radar snapshot wrote through the snapshots/ link"
    assert "[FAIL] snapshot:" in done.stdout, done.stdout + done.stderr
    assert done.returncode == 1


# --------------------------------------------------------------------- init, add


ADD = (
    "add",
    "widget",
    "--repo",
    "https://example.invalid/widget",
    "--axis",
    "lint-format",
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


def test_add_does_not_write_through_a_linked_tools_directory(tmp_path: Path):
    cat = Catalogue(tmp_path)
    (cat.root / "tools").rmdir()
    outside = tmp_path / "outside"
    link_dir(cat.root / "tools", outside)
    done = cat.run("cli", *ADD)
    assert files_in(outside) == [], "radar add wrote through the tools/ link"
    assert "[FAIL]" in done.stdout + done.stderr, done.stdout + done.stderr
    assert done.returncode == 1


def test_init_does_not_write_through_a_linked_directory(tmp_path: Path):
    target = tmp_path / "new-radar"
    outside = tmp_path / "outside"
    link_dir(target / ".github", outside)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    done = run_engine("cli", ("init", str(target)), cwd=tmp_path, env=_env(env))
    assert files_in(outside) == [], "radar init wrote through the .github link"
    assert not (target / "radar.toml").exists(), "init wrote part of a tree it refused"
    assert "[FAIL]" in done.stderr and "symbolic link" in done.stderr, done.stdout + done.stderr
    assert done.returncode == 2


def _env(extra: dict[str, str]) -> dict[str, str]:
    import os

    from engine_run import ENGINE_SRC

    env = {**os.environ, **extra}
    env.pop("RADAR_DATA_ROOT", None)
    env["PYTHONPATH"] = str(ENGINE_SRC)
    return env


# ------------------------------------------------------------------ sync-feedback


def test_sync_feedback_does_not_archive_through_a_linked_directory(tmp_path: Path):
    dest = tmp_path / "radar"
    sync_root(dest, pending={"alpha": ["a pending report\n"]})
    outside = tmp_path / "outside"
    link_dir(dest / "feedback", outside)
    done = run_sync(dest, "--env", "default")
    assert files_in(outside) == [], "radar sync-feedback archived through the feedback/ link"
    assert "[FAIL] alpha:" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1


def test_sync_feedback_does_not_mirror_a_linked_report(tmp_path: Path):
    dest = tmp_path / "radar"
    inbox = sync_root(dest, pending={"alpha": []})
    secret = tmp_path / "secret.txt"
    secret.write_text("KEY MATERIAL\n", encoding="utf-8")
    link_file(dest / "feedback" / "alpha" / "2026-09-01-report.md", secret)
    done = run_sync(dest, "--env", "default")
    mirrored = inbox / "alpha" / "2026-09-01-report.md"
    assert not mirrored.exists(), "a linked archive report was copied into the inbox"
    assert "not mirrored" in done.stdout, done.stdout
    assert done.returncode == 1


def _outside_archive(tmp_path: Path) -> Path:
    """A directory outside the data root laid out like feedback/, with one report."""
    outside = tmp_path / "outside"
    (outside / "alpha").mkdir(parents=True)
    (outside / "alpha" / "notes.md").write_text("not the catalogue's\n", encoding="utf-8")
    return outside


def test_sync_feedback_does_not_mirror_out_of_a_linked_feedback_directory(tmp_path: Path):
    # Each report resolves inside the resolved archive, so a check on the entries alone
    # passes; the link is feedback/ itself.
    dest = tmp_path / "radar"
    inbox = sync_root(dest, pending={"alpha": []})
    link_dir(dest / "feedback", _outside_archive(tmp_path))
    done = run_sync(dest, "--env", "default")
    assert not (inbox / "alpha" / "notes.md").exists(), "mirrored out of the feedback/ link"
    assert "[FAIL] alpha:" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def test_sync_feedback_does_not_run_the_builder_on_a_linked_feedback_directory(tmp_path: Path):
    wt = tmp_path / "builder-wt"
    wt.mkdir()
    marker = tmp_path / "builder-ran"
    (wt / "build_index.py").write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n"
        "(pathlib.Path(sys.argv[1]) / 'INDEX.md').write_text('# index\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    dest = tmp_path / "radar"
    extra = f'worktree = "{wt.as_posix()}"\nindex_builder = "build_index.py"\n'
    sync_root(dest, pending={"alpha": []}, under_git=True, extra=extra)
    outside = _outside_archive(tmp_path)
    link_dir(dest / "feedback", outside)
    listed = run_sync(dest, "--env", "default")
    approved = run_sync(dest, "--env", "default", "--trust-commands")
    shown = listed.stdout + approved.stdout
    assert not marker.exists(), "the index builder ran on the directory feedback/ links to"
    assert not (outside / "alpha" / "INDEX.md").exists(), shown
    assert "[FAIL] alpha:" in approved.stdout, shown
    assert approved.returncode == 1, shown


# ------------------------------------------------------- render-feedback-targets


def test_the_feedback_registry_is_not_written_through_a_linked_directory(tmp_path: Path):
    # Spelled inside the data root, where only feedback-targets.toml may be written, and
    # led by a link to claude_home, where the name and directory rules would not apply.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    commands = cat.claude_home / "commands"
    link_dir(cat.root / "out", commands)
    with_destination(cat, cat.root / "out" / "feedback-targets.toml")
    done = render(cat)
    assert files_in(commands) == [], "the registry was written through the out/ link"
    assert "[FAIL]" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1


def test_the_feedback_registry_is_not_written_through_a_linked_file(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    planted = cat.claude_home / "commands" / "planted.md"
    planted.parent.mkdir(parents=True)
    link_file(cat.root / "feedback-targets.toml", planted)
    with_destination(cat, cat.root / "feedback-targets.toml")
    done = render(cat)
    assert not planted.exists(), "the registry was written through a link into claude_home"
    assert "[FAIL]" in done.stdout, done.stdout
    assert done.returncode == 1


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_does_not_write_a_profile_through_a_linked_directory(tmp_path: Path):
    cat = Catalogue(tmp_path)
    outside = tmp_path / "outside"
    # The committed profile moves with the directory, so the verb still finds it.
    outside.mkdir()
    for p in (cat.root / "environments").iterdir():
        (outside / p.name).write_bytes(p.read_bytes())
        p.unlink()
    (cat.root / "environments").rmdir()
    link_dir(cat.root / "environments", outside)
    before = files_in(outside)
    done = cat.run("bootstrap", "--env", "laptop", "--write")
    assert files_in(outside) == before, "radar bootstrap wrote through the environments/ link"
    assert "[FAIL]" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


# ---------------------------------------------------------------- field reports


def test_a_field_report_is_not_written_through_a_linked_directory(radar):
    radar.tool(
        "ruff",
        'name = "ruff"\nring = "adopt"\nartifact = "cli"\n\n'
        '[telemetry]\nmatch_command = ["ruff"]\nsince = "2026-08-01"\n',
    )
    outside = radar.root.parent / "outside"
    link_dir(radar.root / "field", outside)
    done = radar.run("ruff", "--since", "2026-08-01", "--until", "2026-08-31")
    assert files_in(outside) == [], "radar field wrote through the field/ link"
    assert "[FAIL] ruff:" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def test_an_out_dir_inside_the_data_root_is_held_to_the_same_rule(radar):
    radar.tool(
        "ruff",
        'name = "ruff"\nring = "adopt"\nartifact = "cli"\n\n'
        '[telemetry]\nmatch_command = ["ruff"]\nsince = "2026-08-01"\n',
    )
    outside = radar.root.parent / "outside"
    link_dir(radar.root / "probe", outside)
    done = radar.run("ruff", "--since", "2026-08-01", "--until", "2026-08-31", "--out-dir", "probe")
    assert files_in(outside) == [], "radar field wrote through the probe/ link"
    assert "[FAIL] ruff:" in done.stdout, done.stdout
    assert done.returncode == 1


# -------------------------------------------------------------- redact-backfill


def test_redact_backfill_neither_reads_nor_rewrites_a_tracked_link(tmp_path: Path):
    cat = Catalogue(tmp_path)
    victim = tmp_path / "outside" / "notes.md"
    victim.parent.mkdir()
    home_text = f"a path under {cat.home.as_posix()}/work\n"
    victim.write_text(home_text, encoding="utf-8")
    link_file(cat.root / "docs" / "notes.md", victim)
    cat.commit()
    done = cat.run("redact_backfill")
    assert victim.read_text(encoding="utf-8") == home_text, "the link's target was rewritten"
    assert "[FAIL]" in done.stdout and "symbolic link" in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def _report_under_a_scoped_directory(cat: Catalogue) -> Path:
    """A tracked report in a directory named for a machine-local entry whose placeholder is
    `local-lib`, so redact-backfill renames it to field/local-lib/note.md."""
    (cat.root / ".gitignore").write_text("tools.local/\n", encoding="utf-8")
    local = cat.root / "tools.local" / "local-widget.toml"
    local.parent.mkdir()
    local.write_text('name = "local-widget"\nredact_as = "local-lib"\n', encoding="utf-8")
    report = cat.root / "field" / "local-widget" / "note.md"
    report.parent.mkdir(parents=True)
    report.write_text("a measured window\n", encoding="utf-8")
    cat.commit()
    return report


@pytest.mark.parametrize("check", [False, True], ids=["write", "check"])
def test_redact_backfill_does_not_rename_into_a_linked_directory(tmp_path: Path, check: bool):
    # A link at a MAPPED directory component: git mv, and the directory it needs created,
    # would follow it out of the data root.
    cat = Catalogue(tmp_path)
    report = _report_under_a_scoped_directory(cat)
    outside = tmp_path / "outside"
    link_dir(cat.root / "field" / "local-lib", outside)
    done = cat.run("redact_backfill", *(["--check"] if check else []))
    out = done.stdout.replace("\\", "/")
    assert files_in(outside) == [], "redact-backfill moved a file through the link"
    assert report.is_file(), out
    assert "[FAIL] field/local-widget/note.md not renamed:" in out, out
    assert "symbolic link" in out, out
    assert not [line for line in out.splitlines() if line.startswith("[rename")], out
    assert done.returncode == 1, out + done.stderr


def test_redact_backfill_names_a_dangling_link_on_the_rename_path(tmp_path: Path):
    cat = Catalogue(tmp_path)
    report = _report_under_a_scoped_directory(cat)
    outside = tmp_path / "outside"
    link_dir(cat.root / "field" / "local-lib", outside)
    outside.rmdir()
    done = cat.run("redact_backfill")
    out = done.stdout.replace("\\", "/")
    assert "Traceback" not in done.stderr, done.stderr
    assert not outside.exists(), "redact-backfill created the link's target"
    assert report.is_file(), out
    assert "[FAIL] field/local-widget/note.md not renamed:" in out, out
    assert done.returncode == 1, out + done.stderr
