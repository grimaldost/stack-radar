"""`radar apply`: the policy flags default to off, and an install runs only once approved.

A profile's `[flags]` decide whether apply may install over the network and register
external MCP servers. The starter profile writes both as false; a profile written by hand
that leaves the table (or one key) out must get the same answer, because the permissive
reading is the one that has to be asked for. The header prints the values the plan
actually used.

An install command is catalogue text, like a probe, so `--apply --yes` runs it only once
it is approved - and `--trust-commands` is what approves it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from engine_run import Catalogue, entry, fake_program, probe_script

NO_FLAGS = 'name = "default"\nrings = ["adopt"]\n'


def test_a_profile_without_flags_installs_nothing_over_the_network(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    cat.profile(NO_FLAGS)
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "apply": "uv tool install w"}))
    cat.tool(
        "docs-server",
        entry(
            "docs-server",
            artifact="mcp-server",
            install={"kind": "mcp-server", "apply": "claude mcp add docs-server"},
        ),
    )
    done = cat.run("apply", "--env", "default", "--check")
    assert "allow_network_install=False | allow_external_mcp=False" in done.stdout, done.stdout
    rows = {line.split()[0]: line for line in done.stdout.splitlines() if line[:1].isalpha()}
    assert "manual (no network)" in rows["widget"], done.stdout
    assert "skip: policy" in rows["docs-server"], done.stdout
    assert "install  widget" not in done.stdout, done.stdout


def test_a_missing_key_is_off_and_a_true_one_is_on(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    cat.profile(NO_FLAGS + "\n[flags]\nallow_network_install = true\n")
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "apply": "uv tool install w"}))
    done = cat.run("apply", "--env", "default", "--check")
    assert "allow_network_install=True | allow_external_mcp=False" in done.stdout, done.stdout
    assert "install  widget: uv tool install w" in done.stdout, done.stdout


def test_an_install_runs_only_once_approved(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.profile(NO_FLAGS + "\n[flags]\nallow_network_install = true\n")
    probe_marker = cat.markers / "probe-ran"
    install_marker = cat.markers / "install-ran"
    check = probe_script(cat.base / "probes" / "absent.py", probe_marker, code=1)
    install = probe_script(cat.base / "probes" / "install.py", install_marker)
    cat.tool(
        "widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": install})
    )

    # Approve the probe only: a plan run cannot approve an install it does not run. An
    # approval takes two runs, one that lists the command and one that approves it.
    cat.run("apply", "--env", "default", "--check")
    cat.run("apply", "--env", "default", "--check", "--trust-commands")
    assert probe_marker.exists()
    planned = cat.run("apply", "--env", "default", "--apply")
    assert "re-run with --yes --trust-commands" in planned.stdout, planned.stdout
    assert "(not yet approved, id " in planned.stdout, planned.stdout
    assert not install_marker.exists()

    refused = cat.run("apply", "--env", "default", "--apply", "--yes")
    assert not install_marker.exists(), "an install ran without an approval"
    assert "[UNTRUSTED] widget: [install].apply not run" in refused.stdout, refused.stdout
    assert "1 not run (not approved)" in refused.stdout, refused.stdout
    assert refused.returncode != 0

    approved = cat.run("apply", "--env", "default", "--apply", "--yes", "--trust-commands")
    assert "[TRUSTED] widget: [install].apply approved and recorded" in approved.stdout
    assert install_marker.exists(), approved.stdout + approved.stderr
    # Every install it ran succeeded and nothing else needs action: a script that chains
    # on the exit code reads the converge step as done.
    assert approved.returncode == 0, approved.stdout + approved.stderr


def _install_through(cat: Catalogue, install: str) -> Path:
    """`widget`, absent by an approved probe, whose install is `install`. Returns the
    marker the approved probe leaves."""
    cat.profile(NO_FLAGS + "\n[flags]\nallow_network_install = true\n")
    probe_marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "absent.py", probe_marker, code=1)
    cat.tool(
        "widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": install})
    )
    cat.run("apply", "--env", "default", "--check")
    cat.run("apply", "--env", "default", "--check", "--trust-commands")
    return probe_marker


def test_the_apply_listing_says_what_an_approval_covers(tmp_path: Path):
    cat = Catalogue(tmp_path)
    install = probe_script(cat.root / "scripts" / "install.py", cat.markers / "install-ran")
    cat.commit()
    _install_through(cat, install)
    planned = cat.run("apply", "--env", "default", "--apply")
    assert "(not yet approved, id " in planned.stdout, planned.stdout
    assert "it names scripts/install.py, whose content is part of the approval" in (
        planned.stdout
    ), planned.stdout


@pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows runs a .cmd program through cmd.exe"
)
def test_an_install_the_machine_refuses_is_named_as_refused(tmp_path: Path):
    # A batch-file install with an argument cmd.exe reads as syntax is never run, approved
    # or not: the listing says so, and gives it no id to approve.
    cat = Catalogue(tmp_path)
    install_marker = cat.markers / "install-ran"
    fake_program(
        cat.base / "bin",
        "inst",
        f"import pathlib\npathlib.Path({install_marker.as_posix()!r}).write_text('ran')\n",
    )
    path = {"PATH": f"{cat.base / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    _install_through(cat, "inst a&b")
    planned = cat.run("apply", "--env", "default", "--apply", extra=path)
    assert "not yet approved" not in planned.stdout, planned.stdout
    assert "--trust-commands" not in planned.stdout.split("refused")[0], planned.stdout
    assert "refused, and not run with --yes either:" in planned.stdout, planned.stdout
    # The file found on PATH is spelled as PATHEXT spells its extension.
    assert "inst.cmd is a batch file, which windows runs through cmd.exe, and the " in (
        planned.stdout.lower()
    ), planned.stdout

    done = cat.run("apply", "--env", "default", "--apply", "--yes", "--trust-commands", extra=path)
    assert not install_marker.exists(), done.stdout + done.stderr
    assert "[fail] widget: [install].apply not run - inst.cmd is a batch file" in (
        done.stdout.lower()
    ), done.stdout
    assert "not approved" not in done.stdout, done.stdout
    assert "0/1 install(s) succeeded, 1 refused" in done.stdout, done.stdout
    assert done.returncode != 0


def test_the_header_names_plan_when_plan_is_the_spelling_used(tmp_path: Path):
    # `--plan` is an alias for `--check`. The header echoes the spelling the operator ran,
    # not "check", an internal name the docs never use for it.
    cat = Catalogue(tmp_path, under_git=False)
    cat.profile(NO_FLAGS)
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "apply": "uv tool install w"}))
    done = cat.run("apply", "--env", "default", "--plan")
    assert "mode plan" in done.stdout, done.stdout
    assert "mode check" not in done.stdout, done.stdout
    check = cat.run("apply", "--env", "default", "--check")
    assert "mode check" in check.stdout, check.stdout
