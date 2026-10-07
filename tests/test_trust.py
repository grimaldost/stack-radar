"""A command written in the catalogue runs only once the operator approved that command.

The catalogue is a git repository that takes pull requests, and `[install].check`,
`[install].apply` and `[feedback].index_builder` are commands the engine runs with the
operator's account. These tests hold the approval gate to its contract through
`radar apply --check`, the command a reviewer reaches for first: a pending probe does not
run, an approval runs it, a change to the command or to a file it names makes it pending
again, the approvals live in the git directory (so no pull request can carry one), every
worktree of the catalogue shares them, and a catalogue outside git runs nothing at all.

The probe used throughout leaves a marker file behind when it runs, so "did it run" is a
fact on disk rather than something read out of the command's own report.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from engine_run import PY, Catalogue, cmdline, engine_eval, entry, fake_program, git, probe_script

from stack_radar import trust


def widget_with_probe(cat: Catalogue, *, script: Path | None = None) -> tuple[str, Path]:
    """The `widget` entry with a probe that leaves a marker. Returns (command, marker)."""
    marker = cat.markers / "probe-ran"
    cmd = probe_script(script or cat.base / "probes" / "probe.py", marker)
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )
    return cmd, marker


CHECK_RUN = ("apply", "--env", "default", "--check")


def approve(cat: Catalogue, *args: str, cwd: Path | None = None):
    """The two runs an approval takes: one that lists what is pending, then one with
    --trust-commands, which approves what the first listed. Returns the second."""
    args = args or CHECK_RUN
    cat.run(*args, cwd=cwd)
    return cat.run(*args, "--trust-commands", cwd=cwd)


# --------------------------------------------------------------------- pending


def test_a_pending_check_is_not_run(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    done = cat.run("apply", "--env", "default", "--check")
    assert not marker.exists(), "the probe ran without an approval"
    assert (
        "[UNTRUSTED] widget: [install].check not run - new or changed since it was last "
        f"approved: {cmd}"
    ) in done.stdout, done.stdout
    # Unknown: neither present nor ok.
    row = next(line for line in done.stdout.splitlines() if line.startswith("widget "))
    assert "unknown" in row and "not approved" in row, row
    assert "--trust-commands" in done.stdout.splitlines()[-1], done.stdout
    assert done.returncode != 0, done.stdout + done.stderr


def test_trust_commands_approves_records_and_runs(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    listing = cat.run(*CHECK_RUN)
    ident = listing.stdout.split("listed as id ", 1)[1].split(":", 1)[0]
    done = cat.run(*CHECK_RUN, "--trust-commands")
    assert f"[TRUSTED] widget: [install].check approved and recorded: {cmd}" in done.stdout
    assert f"id {ident}, as an earlier run listed it" in done.stdout, done.stdout
    assert marker.exists(), done.stdout + done.stderr
    assert done.returncode == 0, done.stdout + done.stderr

    # Approved once, it runs on later invocations without the flag.
    marker.unlink()
    again = cat.run("apply", "--env", "default", "--check")
    assert marker.exists(), again.stdout + again.stderr
    assert "[UNTRUSTED]" not in again.stdout and "[TRUSTED]" not in again.stdout
    assert again.returncode == 0, again.stdout + again.stderr


def test_a_changed_command_is_pending_again(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    approve(cat)
    marker.unlink()
    changed = cmd + " --extra"
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": changed, "instruction": "by hand"}),
    )
    done = cat.run("apply", "--env", "default", "--check")
    assert not marker.exists(), "a changed command ran on the old approval"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert done.returncode != 0


def test_a_changed_file_the_command_names_is_pending_again(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    probe_script(cat.root / "scripts" / "probe.py", marker)
    cmd = cmdline(PY, "scripts/probe.py")
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )
    first = approve(cat)
    assert marker.exists(), first.stdout + first.stderr
    assert "it names scripts/probe.py" in first.stdout, first.stdout
    marker.unlink()

    # Same command text, different script: a pull request that edits only the script.
    script = cat.root / "scripts" / "probe.py"
    script.write_text(script.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    done = cat.run("apply", "--env", "default", "--check")
    assert not marker.exists(), "an edited script ran on the approval of its old content"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert "it names scripts/probe.py" in done.stdout, done.stdout
    assert done.returncode != 0


# ------------------------------------------------------- what the flag may approve


def test_the_flag_does_not_approve_a_command_no_run_listed(tmp_path: Path):
    # A command the operator was never shown is listed, not run - even with the flag. The
    # next run with the flag approves it, as that listing showed it.
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    first = cat.run(*CHECK_RUN, "--trust-commands")
    assert not marker.exists(), "the flag approved a command no run had listed"
    assert (
        "[UNTRUSTED] widget: [install].check not run - no earlier `radar apply` run listed "
        f"it as it stands now, so --trust-commands does not approve it yet: {cmd}"
    ) in first.stdout, first.stdout
    assert "listed as id " in first.stdout, first.stdout
    assert first.returncode != 0

    second = cat.run(*CHECK_RUN, "--trust-commands")
    assert marker.exists(), second.stdout + second.stderr
    assert second.returncode == 0, second.stdout + second.stderr


def test_a_script_changed_after_its_listing_is_not_approved(tmp_path: Path):
    # The review run listed the command; a pull request then edited the script it names.
    # The approving run must not approve what the operator never saw.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    probe_script(cat.root / "scripts" / "probe.py", marker)
    cmd = cmdline(PY, "scripts/probe.py")
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )
    listing = cat.run(*CHECK_RUN)
    shown = listing.stdout.split("listed as id ", 1)[1].split(":", 1)[0]
    script = cat.root / "scripts" / "probe.py"
    script.write_text(script.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    done = cat.run(*CHECK_RUN, "--trust-commands")
    assert not marker.exists(), "the flag approved a script changed after its listing"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert f"listed as id {shown}" not in done.stdout, "the id did not follow the script"
    assert done.returncode != 0


def _widget_check(cat: Catalogue, cmd: str) -> None:
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )


def test_a_command_the_latest_listing_no_longer_showed_is_not_approved(tmp_path: Path):
    # A run without the flag is the operator's latest look at what the verb would run. A
    # command an older run listed and that look did not show - a branch reviewed and
    # dropped - is not approved when it comes back.
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    cat.run(*CHECK_RUN)
    _widget_check(cat, cmd + " --other")
    cat.run(*CHECK_RUN)
    _widget_check(cat, cmd)
    done = cat.run(*CHECK_RUN, "--trust-commands")
    assert not marker.exists(), "the flag approved a command the latest listing did not show"
    assert "no earlier `radar apply` run listed it as it stands now" in done.stdout, done.stdout
    assert done.returncode != 0


def test_a_run_with_the_flag_also_replaces_the_listing(tmp_path: Path):
    # The same look, taken only ever with the flag: a branch's command is listed, the main
    # line runs the verb with --trust-commands alone, and the branch's command comes back.
    # The flag run did not show it, so it is not approved.
    cat = Catalogue(tmp_path)
    cmd, marker = widget_with_probe(cat)
    cat.run(*CHECK_RUN)
    _widget_check(cat, cmd + " --other")
    cat.run(*CHECK_RUN, "--trust-commands")
    _widget_check(cat, cmd)
    done = cat.run(*CHECK_RUN, "--trust-commands")
    assert not marker.exists(), "the flag approved a command its own latest run did not show"
    assert "no earlier `radar apply` run listed it as it stands now" in done.stdout, done.stdout
    assert done.returncode != 0


def test_a_probe_another_verb_listed_is_not_approved_by_this_one(tmp_path: Path):
    # The listing is per verb, so the flag on one verb approves only what that verb's
    # latest run showed. Here `radar reconcile` and `radar apply` both list the probe; a
    # branch then changes the script and `radar apply` is run on it, which replaces apply's
    # listing; the script goes back. The listing reconcile left behind must not let
    # `radar apply --trust-commands` run the probe apply's latest run stopped showing.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    script = cat.root / "scripts" / "probe.py"
    probe_script(script, marker)
    shown = script.read_text(encoding="utf-8")
    _widget_check(cat, cmdline(PY, "scripts/probe.py"))
    cat.run("reconcile", "--env", "default")
    cat.run(*CHECK_RUN)
    script.write_text(shown + "# the branch\n", encoding="utf-8")
    cat.run(*CHECK_RUN)
    script.write_text(shown, encoding="utf-8")

    done = cat.run(*CHECK_RUN, "--trust-commands")
    assert not marker.exists(), "apply approved a probe only reconcile's listing still showed"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert done.returncode != 0

    # reconcile's own latest run did show it, so reconcile's flag approves it.
    again = cat.run("reconcile", "--env", "default", "--trust-commands")
    assert marker.exists(), again.stdout + again.stderr
    assert "[TRUSTED] widget: [install].check approved and recorded" in again.stdout


def test_an_install_listed_by_apply_is_what_yes_approves(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.profile('name = "default"\nrings = ["adopt"]\n\n[flags]\nallow_network_install = true\n')
    installed = cat.markers / "install-ran"
    check = probe_script(cat.base / "probes" / "absent.py", cat.markers / "probe-ran", code=1)
    install = probe_script(cat.base / "probes" / "install.py", installed)
    cat.tool(
        "widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": install})
    )
    approve(cat)
    unseen = cat.run("apply", "--env", "default", "--apply", "--yes", "--trust-commands")
    assert not installed.exists(), "an install nobody was shown ran under --yes"
    assert "[UNTRUSTED] widget: [install].apply not run" in unseen.stdout, unseen.stdout

    shown = cat.run("apply", "--env", "default", "--apply")
    assert "(not yet approved, id " in shown.stdout, shown.stdout
    done = cat.run("apply", "--env", "default", "--apply", "--yes", "--trust-commands")
    assert installed.exists(), done.stdout + done.stderr


# ------------------------------------------------------------ what an approval covers


def _probe_with_helper(directory: Path, marker: Path) -> None:
    """A probe that loads `helper.py` from beside itself by path, the way node's
    `require('./helper')` or a shell `source "$(dirname "$0")/lib.sh"` does."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "probe.py").write_text(
        "import pathlib\n"
        "here = pathlib.Path(__file__).parent\n"
        "exec((here / 'helper.py').read_text(encoding='utf-8'))\n"
        f"pathlib.Path({marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n"
        "print('widget 1.0.0')\n",
        encoding="utf-8",
    )
    (directory / "helper.py").write_text("VERSION = 1\n", encoding="utf-8")


def test_a_changed_file_beside_a_named_script_is_pending_again(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    _probe_with_helper(cat.root / "probes", marker)
    cat.tool(
        "widget",
        entry(
            "widget",
            install={
                "kind": "manual",
                "check": cmdline(PY, "probes/probe.py"),
                "instruction": "by hand",
            },
        ),
    )
    first = approve(cat)
    assert marker.exists(), first.stdout + first.stderr
    assert "it names a file in probes/, whose whole content is part of the approval" in (
        first.stdout
    ), first.stdout
    marker.unlink()

    # A pull request that edits only the helper the probe loads by itself.
    helper = cat.root / "probes" / "helper.py"
    helper.write_text("VERSION = 2\n", encoding="utf-8")
    edited = cat.run(*CHECK_RUN)
    assert not marker.exists(), "an edited helper ran on the approval of the old one"
    assert "[UNTRUSTED] widget: [install].check not run" in edited.stdout, edited.stdout

    # And one that adds a file beside it.
    approve(cat)
    marker.unlink()
    (cat.root / "probes" / "node_modules").mkdir()
    (cat.root / "probes" / "node_modules" / "evil.js").write_text("x\n", encoding="utf-8")
    added = cat.run(*CHECK_RUN)
    assert not marker.exists(), "a file added beside the script ran on the old approval"
    assert "[UNTRUSTED]" in added.stdout, added.stdout


def test_an_approved_script_that_imports_its_helper_stays_approved(tmp_path: Path):
    # With PYTHONSAFEPATH set, a script puts its own directory on sys.path to import a
    # helper. Python would then write __pycache__ into the directory the approval hashes,
    # and the approval would lapse on its own after the first run.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    probes = cat.root / "probes"
    probes.mkdir()
    (probes / "probe.py").write_text(
        "import pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
        "import helper\n"
        f"pathlib.Path({marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n"
        "print('widget', helper.VERSION)\n",
        encoding="utf-8",
    )
    (probes / "helper.py").write_text("VERSION = '1.0.0'\n", encoding="utf-8")
    _widget_check(cat, cmdline(PY, "probes/probe.py"))
    writes_bytecode = {"PYTHONDONTWRITEBYTECODE": ""}
    cat.run(*CHECK_RUN, extra=writes_bytecode)
    first = cat.run(*CHECK_RUN, "--trust-commands", extra=writes_bytecode)
    assert marker.exists(), first.stdout + first.stderr
    marker.unlink()

    again = cat.run(*CHECK_RUN, extra=writes_bytecode)
    assert marker.exists(), again.stdout + again.stderr
    assert "[UNTRUSTED]" not in again.stdout, again.stdout
    assert not (probes / "__pycache__").exists()


def test_a_script_directly_in_the_data_root_says_what_is_not_covered(tmp_path: Path):
    cat = Catalogue(tmp_path)
    probe_script(cat.root / "probe.py", cat.markers / "probe-ran")
    cat.tool(
        "widget",
        entry(
            "widget",
            install={"kind": "manual", "check": cmdline(PY, "probe.py"), "instruction": "h"},
        ),
    )
    done = cat.run(*CHECK_RUN)
    assert (
        "probe.py sits directly in the data root, so the files beside it are not part of "
        "the approval"
    ) in done.stdout, done.stdout


def test_a_script_elsewhere_in_the_repository_is_part_of_the_approval(tmp_path: Path):
    # The data root is a subdirectory of the repository, and the probe lives beside it:
    # a pull request to the same repository can edit it, so the approval must cover it.
    cat = Catalogue(tmp_path, under_git=False)
    git(cat.base, cat.home, "init", "-q")
    marker = cat.markers / "probe-ran"
    probe_script(cat.base / "scripts" / "probe.py", marker)
    cat.tool(
        "widget",
        entry(
            "widget",
            install={
                "kind": "manual",
                "check": cmdline(PY, "../scripts/probe.py"),
                "instruction": "by hand",
            },
        ),
    )
    first = approve(cat)
    assert marker.exists(), first.stdout + first.stderr
    assert "it names ../scripts/probe.py, whose content is part of the approval" in (
        first.stdout
    ), first.stdout
    marker.unlink()

    script = cat.base / "scripts" / "probe.py"
    script.write_text(script.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    done = cat.run(*CHECK_RUN)
    assert not marker.exists(), "a script outside the data root ran on its old approval"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout


def _directory_command(cat: Catalogue, marker: Path) -> None:
    """widget's probe names the directory checks/, which Python runs by its __main__.py."""
    checks = cat.root / "checks"
    checks.mkdir()
    (checks / "__main__.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n"
        "print('widget 1.0.0')\n",
        encoding="utf-8",
    )
    cat.tool(
        "widget",
        entry(
            "widget",
            install={"kind": "manual", "check": cmdline(PY, "checks/"), "instruction": "h"},
        ),
    )


def test_a_directory_link_inside_a_named_directory_is_part_of_the_approval(tmp_path: Path):
    from test_write_confinement import link_dir  # noqa: PLC0415 - shared helper

    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    _directory_command(cat, marker)
    for version in ("lib_v1", "lib_v2"):
        (cat.root / version).mkdir()
        (cat.root / version / "code.py").write_text("# same\n", encoding="utf-8")
    link_dir(cat.root / "checks" / "lib", cat.root / "lib_v1")
    approve(cat)
    assert marker.exists()
    marker.unlink()

    # What the link leads to changes, the link itself does not.
    (cat.root / "lib_v1" / "code.py").write_text("# changed\n", encoding="utf-8")
    changed = cat.run(*CHECK_RUN)
    assert not marker.exists(), "a change behind a directory link kept its approval"
    assert "[UNTRUSTED]" in changed.stdout, changed.stdout

    # The link is retargeted to a directory with the same content: only the link's own
    # target text tells the two apart.
    approve(cat)
    marker.unlink()
    (cat.root / "lib_v2" / "code.py").write_text("# changed\n", encoding="utf-8")
    link = cat.root / "checks" / "lib"
    os.rmdir(link) if sys.platform == "win32" else link.unlink()
    link_dir(link, cat.root / "lib_v2")
    retargeted = cat.run(*CHECK_RUN)
    assert not marker.exists(), "a retargeted directory link kept its approval"
    assert "[UNTRUSTED]" in retargeted.stdout, retargeted.stdout


def test_a_link_cycle_inside_a_named_directory_ends(tmp_path: Path):
    from test_write_confinement import link_dir  # noqa: PLC0415 - shared helper

    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    _directory_command(cat, marker)
    link_dir(cat.root / "checks" / "loop", cat.root / "checks")
    done = approve(cat)
    assert marker.exists(), done.stdout + done.stderr


def _retarget(link: Path, target: Path) -> None:
    """Point the directory link `link` at `target` instead."""
    from test_write_confinement import link_dir  # noqa: PLC0415 - shared helper

    os.rmdir(link) if sys.platform == "win32" else link.unlink()
    link_dir(link, target)


def test_a_link_reached_through_another_link_on_the_way_to_a_script_is_covered(tmp_path: Path):
    # `lnk1` leads to `lnk2`, which leads outside the repository. Following `lnk1` to the
    # end of its chain in one step never meets `lnk2`, so retargeting `lnk2` would swap the
    # script an approved command runs while the approval still matched.
    from test_write_confinement import link_dir  # noqa: PLC0415 - shared helper

    cat = Catalogue(tmp_path)
    for side in ("a", "b"):
        probe_script(cat.base / "outside" / side / "probe.py", cat.markers / side)
    link_dir(cat.root / "lnk2", cat.base / "outside" / "a")
    link_dir(cat.root / "lnk1", cat.root / "lnk2")
    _widget_check(cat, cmdline(PY, "lnk1/probe.py"))
    first = approve(cat)
    assert (cat.markers / "a").exists(), first.stdout + first.stderr

    _retarget(cat.root / "lnk2", cat.base / "outside" / "b")
    again = cat.run(*CHECK_RUN)
    assert not (cat.markers / "b").exists(), (
        "a link reached through another link was retargeted and the approval held:\n" + again.stdout
    )
    assert "[UNTRUSTED] widget: [install].check not run" in again.stdout, again.stdout


def test_a_link_reached_through_another_link_inside_a_hashed_tree_is_covered(tmp_path: Path):
    # The same chain inside the tree the approval hashes: the script loads its helper
    # through `scripts/lib`, which leads to `vendor`, which leads outside the repository.
    from test_write_confinement import link_dir  # noqa: PLC0415 - shared helper

    cat = Catalogue(tmp_path)
    scripts = cat.root / "scripts"
    scripts.mkdir()
    (scripts / "probe.py").write_text(
        "import pathlib\n"
        "here = pathlib.Path(__file__).parent\n"
        "exec((here / 'lib' / 'helper.py').read_text(encoding='utf-8'))\n"
        "print('widget 1.0.0')\n",
        encoding="utf-8",
    )
    for side in ("a", "b"):
        helper = cat.base / "outside" / side / "helper.py"
        helper.parent.mkdir(parents=True)
        helper.write_text(
            f"pathlib.Path({(cat.markers / side).as_posix()!r}).write_text('ran')\n",
            encoding="utf-8",
        )
    link_dir(cat.root / "vendor", cat.base / "outside" / "a")
    link_dir(scripts / "lib", cat.root / "vendor")
    _widget_check(cat, cmdline(PY, "scripts/probe.py"))
    first = approve(cat)
    assert (cat.markers / "a").exists(), first.stdout + first.stderr

    _retarget(cat.root / "vendor", cat.base / "outside" / "b")
    again = cat.run(*CHECK_RUN)
    assert not (cat.markers / "b").exists(), (
        "a link reached through another link inside a hashed tree was retargeted and the "
        "approval held:\n" + again.stdout
    )
    assert "[UNTRUSTED] widget: [install].check not run" in again.stdout, again.stdout


# ----------------------------------------------------------------------- store


def test_the_store_lives_in_the_git_directory_and_is_never_tracked(tmp_path: Path):
    cat = Catalogue(tmp_path)
    widget_with_probe(cat)
    cat.commit()
    approve(cat)
    assert cat.store().is_file(), "no approval was recorded in the git common directory"
    status = git(cat.root, cat.home, "status", "--porcelain", "--untracked-files=all")
    assert status.stdout == "", f"approving left the working tree dirty:\n{status.stdout}"


def test_a_second_worktree_shares_the_approvals(tmp_path: Path):
    cat = Catalogue(tmp_path)
    _, marker = widget_with_probe(cat)
    cat.commit()
    review = tmp_path / "review"
    git(cat.root, cat.home, "worktree", "add", "-q", str(review))
    approve(cat)
    marker.unlink()

    done = cat.run("apply", "--env", "default", "--check", cwd=review)
    assert marker.exists(), done.stdout + done.stderr
    assert "[UNTRUSTED]" not in done.stdout, done.stdout
    assert trust.git_common_dir(review) == trust.git_common_dir(cat.root)


def test_a_data_root_outside_git_runs_nothing_even_when_asked(tmp_path: Path):
    if trust.git_common_dir(tmp_path) is not None:
        pytest.skip("the temporary directory is itself inside a git repository")
    cat = Catalogue(tmp_path, under_git=False)
    _, marker = widget_with_probe(cat)
    done = cat.run("apply", "--env", "default", "--check", "--trust-commands")
    assert not marker.exists(), "a probe ran with nowhere to record its approval"
    assert "[FAIL] trust:" in done.stdout and "not inside a git repository" in done.stdout
    assert "trusted-commands.json" in done.stdout, done.stdout
    assert done.returncode != 0


def test_the_summary_counts_the_trust_fail(tmp_path: Path):
    if trust.git_common_dir(tmp_path) is not None:
        pytest.skip("the temporary directory is itself inside a git repository")
    cat = Catalogue(tmp_path, under_git=False)
    widget_with_probe(cat)
    done = cat.run("apply", "--env", "default", "--check")
    summary = next(line for line in done.stdout.splitlines() if " wanted - " in line)
    assert "- 1 FAIL" in summary, done.stdout


# ------------------------------------------------------------------ the gate


def test_the_gate_runs_no_probe(radar):
    marker = radar.root.parent / "gate-probe-ran"
    cmd = probe_script(radar.root.parent / "probes" / "probe.py", marker)
    radar.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )
    radar.gate()
    assert not marker.exists(), "radar gate ran a catalogue probe"


# ------------------------------------------------------------------ the parts


class TestGitCommonDir:
    def test_a_plain_repository(self, tmp_path: Path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        found = trust.git_common_dir(tmp_path / "repo" / "sub")
        assert found == (tmp_path / "repo" / ".git").resolve()

    def test_a_linked_worktree_reaches_the_shared_directory(self, tmp_path: Path):
        private = tmp_path / "main" / ".git" / "worktrees" / "wt"
        private.mkdir(parents=True)
        (private / "commondir").write_text("../..\n", encoding="utf-8")
        (tmp_path / "wt").mkdir()
        (tmp_path / "wt" / ".git").write_text(
            "gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8"
        )
        assert trust.git_common_dir(tmp_path / "wt") == (tmp_path / "main" / ".git").resolve()

    def test_nothing_outside_a_repository(self, tmp_path: Path):
        if trust.git_common_dir(tmp_path) is not None:
            pytest.skip("the temporary directory is itself inside a git repository")
        assert trust.git_common_dir(tmp_path / "plain") is None


class TestSplitAndFind:
    def test_a_quoted_argument_stays_whole(self):
        assert trust.split_command('widget --name "a b" C:/tools/x.toml') == [
            "widget",
            "--name",
            "a b",
            "C:/tools/x.toml",
        ]

    def test_a_program_in_the_working_directory_is_never_found(self, tmp_path, monkeypatch):
        here = tmp_path / "catalogue"
        here.mkdir()
        name = "widget.cmd" if sys.platform == "win32" else "widget"
        planted = here / name
        planted.write_text("", encoding="utf-8")
        planted.chmod(0o755)
        monkeypatch.chdir(here)
        empty = tmp_path / "bin"
        empty.mkdir()
        assert trust.find_executable("widget", os.pathsep.join([str(empty), ".", ""])) is None

    def test_a_program_on_path_is_found(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        name = "widget.cmd" if sys.platform == "win32" else "widget"
        (bin_dir / name).write_text("", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
        found = trust.find_executable("widget", str(bin_dir))
        assert found is not None and Path(found).name.lower() == name


class TestTrustObject:
    def test_approval_covers_text_and_named_files(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "scripts" / "p.py").write_text("print(1)\n", encoding="utf-8")
        text = "widget scripts/p.py --config=scripts/p.py"

        pending = trust.Trust(root)
        cmd = pending.command("widget", trust.CHECK, text)
        assert [p for p, _ in cmd.files] == ["scripts/ (directory)", "scripts/p.py"]
        assert not pending.allows(cmd) and pending.skipped == 1

        # Listed by the run above, so the flag approves it.
        assert trust.Trust(root, approve=True).allows(cmd)
        later = trust.Trust(root)
        assert later.allows(later.command("widget", trust.CHECK, text))
        assert not later.allows(later.command("widget", trust.CHECK, text + " -v"))
        assert not later.allows(later.command("other", trust.CHECK, text))

        (root / "scripts" / "p.py").write_text("print(2)\n", encoding="utf-8")
        edited = trust.Trust(root)
        assert not edited.allows(edited.command("widget", trust.CHECK, text))

    def test_a_path_written_inside_a_longer_argument_is_named_as_not_covered(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "scripts" / "p.py").write_text("print(1)\n", encoding="utf-8")
        t = trust.Trust(root)

        def uncovered(text: str) -> list[str]:
            return [n for n in t.command("widget", trust.CHECK, text).notes if "inside" in n]

        inner = uncovered('sh -c "python scripts/p.py --version"')
        assert len(inner) == 1 and "python scripts/p.py --version" in inner[0], inner
        assert "not part of the approval" in inner[0], inner
        # A path on its own, the value after '=', or a slash with no program text around
        # it (a repository name) says nothing.
        assert uncovered("python scripts/p.py") == []
        assert uncovered('widget "--config=scripts/p.py"') == []
        assert uncovered("gh repo view owner/repo") == []

    def test_a_run_replaces_only_its_own_verb_s_listing(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)

        def look(verb: str, *texts: str) -> None:
            run = trust.Trust(root, verb=verb)
            for text in texts:
                run.allows(run.command("widget", trust.CHECK, text))
            run.closing()

        look("versions", "widget --a")
        look("apply", "widget --b")
        # A run that asks about no command at all (sync-feedback --check, say) keeps it.
        look("apply")
        look("sync-feedback")
        flagged = trust.Trust(root, approve=True, verb="apply")
        assert flagged.allows(flagged.command("widget", trust.CHECK, "widget --b"))

        look("apply", "widget --c")
        look("apply", "widget --d")
        flagged = trust.Trust(root, approve=True, verb="versions")
        assert flagged.allows(flagged.command("widget", trust.CHECK, "widget --a")), (
            "a later apply run dropped what versions listed"
        )
        flagged = trust.Trust(root, approve=True, verb="apply")
        assert flagged.allows(flagged.command("widget", trust.CHECK, "widget --d"))
        assert not flagged.allows(flagged.command("widget", trust.CHECK, "widget --c")), (
            "a listing the verb's latest run did not show stayed approvable"
        )

    def test_the_flag_approves_only_what_its_own_verb_listed(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)

        def look(verb: str, *texts: str) -> None:
            run = trust.Trust(root, verb=verb)
            for text in texts:
                run.allows(run.command("widget", trust.CHECK, text))
            run.closing()

        look("reconcile", "widget --p1")
        look("apply", "widget --p1")
        look("apply", "widget --p2")
        flagged = trust.Trust(root, approve=True, verb="apply")
        assert not flagged.allows(flagged.command("widget", trust.CHECK, "widget --p1")), (
            "apply approved a command only reconcile's listing still held"
        )
        flagged = trust.Trust(root, approve=True, verb="versions")
        assert not flagged.allows(flagged.command("widget", trust.CHECK, "widget --p2")), (
            "versions approved a command only apply listed"
        )
        flagged = trust.Trust(root, approve=True, verb="reconcile")
        assert flagged.allows(flagged.command("widget", trust.CHECK, "widget --p1"))

    def test_a_retargeted_link_is_pending_again(self, tmp_path: Path):
        # A link inside the data root that points outside it adds no content hash, so
        # its target text is what the approval has to hold on to.
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        (root / "scripts").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.py").write_text("print('a')\n", encoding="utf-8")
        (outside / "b.py").write_text("print('b')\n", encoding="utf-8")
        link = root / "scripts" / "probe.py"
        try:
            link.symlink_to(outside / "a.py")
        except OSError:
            pytest.skip("this account cannot create symbolic links")
        text = "widget scripts/probe.py"

        trust.Trust(root).allows(trust.Trust(root).command("widget", trust.CHECK, text))
        first = trust.Trust(root, approve=True)
        assert first.allows(first.command("widget", trust.CHECK, text))

        link.unlink()
        link.symlink_to(outside / "b.py")
        later = trust.Trust(root)
        assert not later.allows(later.command("widget", trust.CHECK, text)), (
            "a link retargeted outside the data root kept its approval"
        )

    def test_a_link_s_target_text_is_part_of_the_approval(self, tmp_path: Path):
        # The same rule without needing the right to create links (Windows often lacks
        # it): the link is simulated, and only its target text changes between runs. Run
        # in a subprocess, so RADAR_ENGINE_SRC decides which engine is judged.
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        (root / "scripts").mkdir()
        code = "\n".join(
            [
                "from pathlib import Path",
                "from stack_radar import trust",
                f"root = Path({root.as_posix()!r})",
                "spelled = (root / 'scripts' / 'probe.py').resolve()",
                "target = {'now': '../../outside/a.py'}",
                "real = Path.is_symlink",
                "Path.is_symlink = lambda self: self == spelled or real(self)",
                "trust.os.readlink = lambda p: target['now']",
                "text = 'widget scripts/probe.py'",
                "listing = trust.Trust(root)",
                "listing.allows(listing.command('widget', trust.CHECK, text))",
                "first = trust.Trust(root, approve=True)",
                "cmd = first.command('widget', trust.CHECK, text)",
                "print('recorded', ('scripts/probe.py (link)', target['now']) in cmd.files)",
                "print('approved', first.allows(cmd))",
                "target['now'] = '../../outside/b.py'",
                "later = trust.Trust(root)",
                "print('retargeted', later.allows(later.command('widget', trust.CHECK, text)))",
            ]
        )
        done = engine_eval(code, tmp_path / "home", tmp_path)
        tail = [line for line in done.stdout.splitlines() if line.split(" ")[0] in WORDS]
        assert tail == ["recorded True", "approved True", "retargeted False"], (
            done.stdout + done.stderr
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Windows collapses `..` before it follows a link"
    )
    def test_a_link_followed_by_dotdot_is_part_of_the_approval(self, tmp_path: Path):
        # A POSIX kernel resolves `lnk/..` to the parent of the link's TARGET. A link that
        # leads outside the repository then decides which outside file runs, and nothing
        # of that file is hashed - so the link's target text must be.
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        for side in ("a", "b"):
            (tmp_path / "outside" / side / "sub").mkdir(parents=True)
            (tmp_path / "outside" / side / "probe.py").write_text(
                f"print({side!r})\n", encoding="utf-8"
            )
        link = root / "lnk"
        link.symlink_to("../outside/a/sub")
        text = "python3 lnk/../probe.py"

        listing = trust.Trust(root)
        listing.allows(listing.command("widget", trust.CHECK, text))
        first = trust.Trust(root, approve=True)
        cmd = first.command("widget", trust.CHECK, text)
        assert ("lnk (link)", "../outside/a/sub") in cmd.files, cmd.files
        assert first.allows(cmd)

        link.unlink()
        link.symlink_to("../outside/b/sub")
        later = trust.Trust(root)
        assert not later.allows(later.command("widget", trust.CHECK, text)), (
            "a link retargeted before a `..` kept its approval"
        )

    def test_a_link_before_dotdot_is_part_of_the_approval_wherever_it_runs(self, tmp_path: Path):
        # The same rule without the right to create links: the link is simulated, and only
        # its target text changes. Run in a subprocess, so RADAR_ENGINE_SRC decides which
        # engine is judged.
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        (root / "probe.py").write_text("print(1)\n", encoding="utf-8")
        code = "\n".join(
            [
                "from pathlib import Path",
                "from stack_radar import trust",
                f"root = Path({root.as_posix()!r}).resolve()",
                "spelled = root / 'lnk'",
                "target = {'now': '../outside/a/sub'}",
                "real = Path.is_symlink",
                "Path.is_symlink = lambda self: self == spelled or real(self)",
                "trust.os.readlink = lambda p: target['now']",
                "text = 'widget lnk/../probe.py'",
                "listing = trust.Trust(root)",
                "listing.allows(listing.command('widget', trust.CHECK, text))",
                "first = trust.Trust(root, approve=True)",
                "cmd = first.command('widget', trust.CHECK, text)",
                "print('recorded', ('lnk (link)', target['now']) in cmd.files)",
                "print('approved', first.allows(cmd))",
                "target['now'] = '../outside/b/sub'",
                "later = trust.Trust(root)",
                "print('retargeted', later.allows(later.command('widget', trust.CHECK, text)))",
            ]
        )
        done = engine_eval(code, tmp_path / "home", tmp_path)
        tail = [line for line in done.stdout.splitlines() if line.split(" ")[0] in WORDS]
        assert tail == ["recorded True", "approved True", "retargeted False"], (
            done.stdout + done.stderr
        )

    def test_two_commands_that_share_a_display_id_are_told_apart(self, tmp_path: Path):
        # The printed id is a prefix of the digest, short enough for someone who writes
        # both commands to find a pair that shares it. The flag has to hold the approving
        # run to the whole digest: here every command prints the same id, the listing
        # showed the first, and the second must not ride on it. Run in a subprocess, so
        # RADAR_ENGINE_SRC decides which engine is judged.
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        code = "\n".join(
            [
                "from pathlib import Path",
                "from stack_radar import trust",
                f"root = Path({root.as_posix()!r})",
                "trust.Command.id = property(lambda self: 'e685c4c3ea6e')",
                "listing = trust.Trust(root, verb='apply')",
                "listing.allows(listing.command('widget', trust.CHECK, 'widget --shown'))",
                "listing.closing()",
                "flagged = trust.Trust(root, approve=True, verb='apply')",
                "print('swapped', flagged.allows(flagged.command('widget', trust.CHECK, "
                "'widget --swapped')))",
                "print('shown', flagged.allows(flagged.command('widget', trust.CHECK, "
                "'widget --shown')))",
            ]
        )
        done = engine_eval(code, tmp_path / "home", tmp_path)
        tail = [line for line in done.stdout.splitlines() if line.split(" ")[0] in WORDS]
        assert tail == ["swapped False", "shown True"], done.stdout + done.stderr

    def test_a_listing_kept_under_the_display_id_approves_nothing(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        (root / ".git").mkdir(parents=True)
        cmd = trust.Trust(root).command("widget", trust.CHECK, "widget --a")
        store = root / ".git" / trust.STORE_DIR / trust.STORE_FILE
        store.parent.mkdir()
        listed = {cmd.id: {"on": "2000-01-01", "by": ["apply"]}}
        store.write_text(
            json.dumps({"schema": trust.SCHEMA, "approvals": [], "listed": listed}),
            encoding="utf-8",
        )
        flagged = trust.Trust(root, approve=True, verb="apply")
        assert not flagged.allows(cmd), "a listing keyed by the short id approved a command"


WORDS = {"recorded", "approved", "retargeted", "swapped", "shown"}


# ------------------------------------------------------------ where a command runs


def _approve_then_plant(cat: Catalogue, cmd: str, planted: Path, body: str) -> str:
    """Approve `cmd` for widget, then change `planted` - a file inside the data root that
    no argument of `cmd` names - and run --check again without the flag."""
    cat.tool(
        "widget",
        entry("widget", install={"kind": "manual", "check": cmd, "instruction": "by hand"}),
    )
    # The engine itself runs from outside the data root, the way the `radar` script does
    # not put its working directory on the import path: `python -m stack_radar...` from
    # inside the catalogue would import a planted json.py into the ENGINE, which is the
    # test harness's doing and not what is under test.
    args = ("apply", "--data-root", str(cat.root), "--env", "default", "--check")
    first = approve(cat, *args, cwd=cat.base)
    assert "[TRUSTED] widget" in first.stdout, first.stdout + first.stderr
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(body, encoding="utf-8")
    again = cat.run(*args, cwd=cat.base)
    return again.stdout + again.stderr


def _writes(marker: Path) -> str:
    return f"import pathlib\npathlib.Path({marker.as_posix()!r}).write_text('ran')\n"


def test_a_module_the_approval_did_not_hash_is_never_imported(tmp_path: Path):
    # `python -m <module>` names a module, not a file: were the data root the working
    # directory, editing <module>.py there would change what the approved command runs.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "module-ran"
    module = cat.root / "widgetprobe.py"
    module.write_text("print('widget 1.0.0')\n", encoding="utf-8")
    out = _approve_then_plant(cat, cmdline(PY, "-m", "widgetprobe"), module, _writes(marker))
    assert not marker.exists(), f"an edited module ran on the old approval:\n{out}"


def test_a_file_that_shadows_a_standard_module_is_never_imported(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "shadow-ran"
    cmd = cmdline(PY, "-c", "import json; print('widget', json.dumps(1))")
    out = _approve_then_plant(cat, cmd, cat.root / "json.py", _writes(marker))
    assert not marker.exists(), f"a json.py added to the catalogue was imported:\n{out}"


def test_a_module_beside_an_approved_script_is_not_on_its_import_path(tmp_path: Path):
    # The approval hashes scripts/probe.py; scripts/helper.py is not named by the command,
    # so it is not part of the approval and must not be what the script imports.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "helper-ran"
    scripts = cat.root / "scripts"
    scripts.mkdir()
    (scripts / "probe.py").write_text(
        "try:\n    import helper\nexcept ImportError:\n    pass\nprint('widget 1.0.0')\n",
        encoding="utf-8",
    )
    (scripts / "helper.py").write_text(_writes(marker), encoding="utf-8")
    cat.tool(
        "widget",
        entry(
            "widget",
            install={
                "kind": "manual",
                "check": cmdline(PY, "scripts/probe.py"),
                "instruction": "by hand",
            },
        ),
    )
    done = approve(cat)
    assert not marker.exists(), "a module the script's import path should not reach ran"
    row = next(line for line in done.stdout.splitlines() if line.startswith("widget "))
    assert row.split()[3] == "yes", done.stdout  # the named script itself still ran


def test_a_command_does_not_run_in_the_data_root(tmp_path: Path):
    cat = Catalogue(tmp_path)
    seen = cat.markers / "cwd"
    code = f"import os, pathlib; pathlib.Path({seen.as_posix()!r}).write_text(os.getcwd())"
    cat.tool(
        "widget",
        entry(
            "widget",
            install={"kind": "manual", "check": cmdline(PY, "-c", code), "instruction": "h"},
        ),
    )
    approve(cat)
    ran_in = Path(seen.read_text(encoding="utf-8"))
    assert not trust.inside(ran_in, cat.root), ran_in


class TestCatalogueArgv:
    def test_paths_under_the_data_root_become_absolute(self, tmp_path: Path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "probe.py").write_text("", encoding="utf-8")
        (tmp_path / "probe.toml").write_text("", encoding="utf-8")
        argv = trust.catalogue_argv(
            "python scripts/probe.py --config=probe.toml --name widget", tmp_path
        )
        assert argv == [
            "python",
            str(tmp_path / "scripts" / "probe.py"),
            f"--config={tmp_path / 'probe.toml'}",
            "--name",
            "widget",
        ]

    def test_a_bare_name_that_is_only_a_directory_stays_a_name(self, tmp_path: Path):
        (tmp_path / "tools").mkdir()
        assert trust.catalogue_argv("widget list tools", tmp_path) == ["widget", "list", "tools"]

    def test_a_bare_program_stays_bare(self, tmp_path: Path):
        (tmp_path / "widget").write_text("", encoding="utf-8")
        assert trust.catalogue_argv("widget --version", tmp_path)[0] == "widget"


# ------------------------------------------------------------- batch files on Windows


class TestBatchRefusal:
    def test_an_argument_cmd_exe_reads_as_syntax_is_refused(self):
        for arg in ('x" & echo hi', "a&b", "%PATH%", "a|b", "a>b", "a<b", "a^b", "!x!", "(a)"):
            assert trust.batch_refusal("C:/bin/npm.cmd", ["install", arg], "win32"), arg
        assert trust.batch_refusal("C:/bin/probe.BAT", ["a\nb"], "win32")

    def test_plain_arguments_run(self):
        plain = ["install", "-g", "@scope/pkg@1.2.3", "--registry=https://r.example/x", "a b"]
        assert trust.batch_refusal("C:/bin/npm.CMD", plain, "win32") is None

    def test_only_a_batch_file_on_windows_is_judged(self):
        assert trust.batch_refusal("C:/bin/npm.cmd", ["a&b"], "linux") is None
        assert trust.batch_refusal("C:/bin/npm.exe", ["a&b"], "win32") is None

    def test_a_program_path_cmd_exe_reads_as_syntax_is_refused(self):
        # A path without a space is passed bare, and cmd.exe ends or splits the name at
        # these characters and runs whatever follows, or another file.
        for bad in "&^(,;=%!":
            exe = f"C:/repo/scripts/x{bad}evil.cmd"
            assert trust.batch_refusal(exe, ["--version"], "win32"), exe
        # In quotes cmd.exe still expands a variable.
        assert trust.batch_refusal("C:/my repo/x%PATH%.cmd", [], "win32")
        assert trust.batch_refusal("C:/my repo/x!PATH!.cmd", [], "win32")

    def test_a_batch_file_is_known_by_its_name_as_windows_reads_it(self):
        # Windows drops trailing dots and spaces from a file name, so it opens `p.cmd.` as
        # `p.cmd`, and CreateProcess hands it to cmd.exe as it does `p.cmd`.
        for exe in ("C:/repo/p.cmd.", "C:/repo/p.cmd ", "C:/repo/p.CMD. .", "C:/repo/q.bat..."):
            assert trust.is_batch(exe, "win32"), exe
            assert trust.batch_refusal(exe, ["a&evil"], "win32"), exe
        for exe in ("C:/repo/p.cmdx", "C:/repo/p.cmd.txt"):
            assert not trust.is_batch(exe, "win32"), exe
        assert not trust.is_batch("C:/repo/p.cmd.", "linux")

    def test_a_program_path_passed_in_quotes_may_hold_brackets(self):
        # A path holding a space is passed in quotes, where only `%` and `!` mean anything.
        for exe in ("C:/Program Files (x86)/nodejs/npm.cmd", "C:/R&D tools/probe.cmd"):
            assert trust.batch_refusal(exe, ["--version"], "win32") is None, exe
        assert trust.batch_refusal("C:/tools/node-v20.1/bin/npm.cmd", [], "win32") is None


ON_WINDOWS = pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows runs a .cmd program through cmd.exe"
)


def _batch_probe(cat: Catalogue) -> tuple[Path, dict[str, str]]:
    """A `probe` program on PATH, a `.cmd` on Windows, that leaves a marker when it runs.
    Returns (marker, the environment that puts it on PATH)."""
    marker = cat.markers / "probe-ran"
    fake_program(cat.base / "bin", "probe", _writes(marker) + "print('probe 1.0')\n")
    return marker, {"PATH": f"{cat.base / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}


@ON_WINDOWS
def test_a_batch_file_is_not_run_with_an_argument_cmd_exe_reads_as_syntax(tmp_path: Path):
    # The quoted argument is one literal to the splitter, the listing and the approval, but
    # cmd.exe would end its quoting at the `"` and run `echo` as a command of its own.
    cat = Catalogue(tmp_path)
    marker, path = _batch_probe(cat)
    pwned = tmp_path / "pwned.txt"
    _widget_check(cat, f"probe 'x\" & echo pwned>{pwned} & \"'")
    cat.run(*CHECK_RUN, extra=path)
    done = cat.run(*CHECK_RUN, "--trust-commands", extra=path)
    assert not pwned.exists(), "cmd.exe ran a command written inside a quoted argument"
    assert not marker.exists(), done.stdout + done.stderr
    assert "[FAIL] widget: [install].check not run - probe." in done.stdout, done.stdout
    assert "is a batch file, which Windows runs through cmd.exe" in done.stdout, done.stdout
    assert done.returncode != 0


@ON_WINDOWS
def test_a_batch_file_with_plain_arguments_runs_and_is_named(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker, path = _batch_probe(cat)
    _widget_check(cat, "probe --version 'a b' --name=widget")
    listing = cat.run(*CHECK_RUN, extra=path)
    # Named as the catalogue spells it, then once as the file found on PATH, whose
    # extension is spelled as PATHEXT spells it.
    out = listing.stdout.lower()
    resolved = (cat.base / "bin" / "probe.cmd").as_posix().lower()
    assert f"probe ({resolved}) is a batch file, which windows runs through cmd.exe" in out, (
        listing.stdout
    )
    assert str(cat.base / "bin" / "probe.cmd").lower() not in out, listing.stdout
    done = cat.run(*CHECK_RUN, "--trust-commands", extra=path)
    assert marker.exists(), done.stdout + done.stderr
    assert done.returncode == 0, done.stdout + done.stderr


def _batch_named_like_syntax(cat: Catalogue) -> tuple[Path, dict[str, str]]:
    """A tracked `scripts/x&evil&.cmd`, and an `evil` on PATH that leaves a marker: cmd.exe,
    handed the first one's path bare, would run the second. Returns (marker, environment)."""
    marker = cat.markers / "evil-ran"
    fake_program(cat.base / "bin", "evil", _writes(marker))
    (cat.root / "scripts").mkdir(exist_ok=True)
    (cat.root / "scripts" / "x&evil&.cmd").write_text("@echo x 1.0\r\n", encoding="utf-8")
    cat.commit()
    return marker, {"PATH": f"{cat.base / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}


@ON_WINDOWS
def test_a_batch_file_whose_path_cmd_exe_reads_as_syntax_is_not_run(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker, path = _batch_named_like_syntax(cat)
    _widget_check(cat, "scripts/x&evil&.cmd --version")
    cat.run(*CHECK_RUN, extra=path)
    done = cat.run(*CHECK_RUN, "--trust-commands", extra=path)
    assert not marker.exists(), "cmd.exe ran a program named inside the batch file's path"
    assert "[FAIL] widget: [install].check not run - x&evil&.cmd is a batch file" in (
        done.stdout
    ), done.stdout
    assert "[TRUSTED]" not in done.stdout, done.stdout
    assert done.returncode != 0


@ON_WINDOWS
@pytest.mark.parametrize("program", ["scripts/p.cmd.", "'scripts/p.cmd '"])
def test_a_batch_file_spelled_with_a_trailing_dot_or_space_is_a_batch_file(
    tmp_path: Path, monkeypatch, program: str
):
    # Windows opens `scripts/p.cmd.` as `scripts/p.cmd` and runs it through cmd.exe, which
    # would read the `&` in the argument as its own syntax and run `evil`.
    from stack_radar import apply, versions

    cat = Catalogue(tmp_path)
    marker = cat.markers / "evil-ran"
    fake_program(cat.base / "bin", "evil", _writes(marker))
    (cat.root / "scripts").mkdir(exist_ok=True)
    (cat.root / "scripts" / "p.cmd").write_text("@echo p 1.0\r\n", encoding="utf-8")
    cat.commit()
    path = {"PATH": f"{cat.base / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    _widget_check(cat, f"{program} a&evil")
    listing = cat.run(*CHECK_RUN, extra=path)
    done = cat.run(*CHECK_RUN, "--trust-commands", extra=path)
    assert not marker.exists(), "cmd.exe ran a command written inside an argument"
    for out in (listing.stdout, done.stdout):
        assert "is a batch file, which Windows runs through cmd.exe, and the argument " in out
    assert "[TRUSTED]" not in done.stdout, done.stdout
    assert done.returncode != 0
    monkeypatch.setenv("PATH", path["PATH"])
    command = f"{program} a&evil"
    assert apply.run_command(command, 30, cat.root)[0] == "error"
    assert versions.run(command, cat.root) is None
    assert not marker.exists(), "a runner handed cmd.exe the batch file on its own"


@ON_WINDOWS
def test_the_runners_refuse_such_a_batch_file_on_their_own(tmp_path: Path, monkeypatch):
    # The gate refuses first; each runner refuses again, so neither hands cmd.exe such a
    # path whoever calls it.
    from stack_radar import apply, versions

    cat = Catalogue(tmp_path)
    marker, path = _batch_named_like_syntax(cat)
    monkeypatch.setenv("PATH", path["PATH"])
    outcome, _, detail = apply.run_command("scripts/x&evil&.cmd --version", 30, cat.root)
    assert (outcome, "cmd.exe" in detail) == ("error", True), (outcome, detail)
    assert versions._exec(["scripts/x&evil&.cmd", "--version"], cat.root) is None
    assert not marker.exists(), "cmd.exe ran a program named inside the batch file's path"
