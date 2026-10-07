"""Every verb answers `--help` and `-h` on its own, without a data root or git.

WHY THIS FILE. `radar <verb> --help` is read from a shell that carries no `radar.toml`
at least as often as from inside a data root - a locked-down machine still has to be
able to ask what a command takes before it decides whether to set one up. So `--help`
has to exit 0, print a usage line naming the verb, write nothing, and never resolve a
data root or run git first: a parser that resolves the root - or a hand-rolled `--help`
check that runs after the root search - answers a fresh, marker-less directory with a
traceback or a false root-not-found instead of the usage text a caller asked for.

`cli.VERBS` is iterated, not copied by hand: a verb the table gains later is covered the
day it lands, not the day someone remembers to update this file.

Run as a subprocess of the installed entry point. An in-process call would have to catch
the `SystemExit` that `--help` raises straight through `cli.main`'s own try/finally
(which restores `sys.argv` and lets the exception continue), and a subprocess is what an
operator actually runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from engine_run import run_engine

from stack_radar import cli

REPO = Path(__file__).resolve().parent.parent
ENGINE_SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))

VERBS = sorted(cli.VERBS)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # No RADAR_DATA_ROOT: a machine that exports it would point every one of these at a
    # real tree, and the whole point is that `--help` needs no tree at all.
    env = {**os.environ}
    env.pop("RADAR_DATA_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ENGINE_SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return run_engine("cli", args, cwd=cwd, env=env)


def test_help_answers_from_a_real_process(tmp_path: Path) -> None:
    # The tests below may run the launcher in this process (tests/engine_run.py). This one
    # starts a real interpreter, so `python -m stack_radar.cli` reaching the package by
    # name and exiting 0 on `--help` is proven at the process boundary too.
    env = {**os.environ}
    env.pop("RADAR_DATA_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ENGINE_SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    for args in (["--help"], ["gate", "--help"]):
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "stack_radar.cli", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert done.returncode == 0, (args, done.stdout, done.stderr)
        assert "usage: radar" in done.stdout, done.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("verb", VERBS)
def test_verb_help_exits_zero_with_usage_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, flag: str
) -> None:
    # An empty tmp_path with no radar.toml above it: the fixture this test needs is the
    # ABSENCE of a data root, so `--help` proves it never went looking for one.
    monkeypatch.chdir(tmp_path)
    result = _run(tmp_path, verb, flag)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert f"usage: radar {verb}" in result.stdout, result.stdout
    assert list(tmp_path.iterdir()) == []


def test_every_verb_covered_at_least_once() -> None:
    # A guard on the guard: an empty VERBS table would make the parametrised test above
    # pass zero times and report success.
    assert len(VERBS) >= 10, VERBS


class TestChangelogGateBaseRefRefusal:
    """`radar changelog-gate` on a base ref that looks like an option, and on a
    directory that is not a git repository at all - both fail loudly, never OK, never a
    traceback."""

    def _run_changelog_gate(
        self, cwd: Path, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ}
        env.pop("RADAR_DATA_ROOT", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ENGINE_SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        if extra_env:
            env.update(extra_env)
        return run_engine("changelog_gate", args, cwd=cwd, env=env)

    def test_a_base_ref_starting_with_a_dash_exits_2_without_ok(self, tmp_path: Path) -> None:
        result = self._run_changelog_gate(tmp_path, "--not-a-real-flag")
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "[FAIL]" in result.stdout
        assert "OK" not in result.stdout

    def test_a_non_git_directory_exits_2_with_no_traceback(self, tmp_path: Path) -> None:
        # GIT_CEILING_DIRECTORIES stops git from walking up past tmp_path into a real
        # repository above it - the fixture's whole point is a directory that is not one.
        result = self._run_changelog_gate(
            tmp_path, extra_env={"GIT_CEILING_DIRECTORIES": str(tmp_path.parent)}
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "[FAIL]" in result.stdout
        assert "OK" not in result.stdout
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize("verb", VERBS)
def test_every_verb_says_what_it_does(tmp_path: Path, verb: str) -> None:
    # The paragraph after the usage block is argparse's description. A verb without one
    # answers `--help` with its flags alone, and a reader who asked what it does learns
    # only how to call it.
    result = _run(tmp_path, verb, "--help")
    paragraphs = [p for p in result.stdout.split("\n\n") if p.strip()]
    assert len(paragraphs) > 1, result.stdout
    after_usage = paragraphs[1].lstrip()
    assert not after_usage.startswith(("options:", "positional arguments:")), result.stdout


def _help(tmp_path: Path, verb: str) -> str:
    return " ".join(_run(tmp_path, verb, "--help").stdout.split())


def test_the_help_states_what_the_verbs_do(tmp_path: Path) -> None:
    # Each help text states what the verb does, not less and not other.
    assert "for an entry, at any ring" in _help(tmp_path, "field")
    assert "resolved chain of executables" in _help(tmp_path, "add")
    gate = _help(tmp_path, "gate")
    assert "Normally not needed" in gate, gate
    assert "a fresh clone needs this" not in gate, gate


@pytest.mark.parametrize("verb", ["field", "reconcile", "versions", "sync-feedback"])
def test_the_env_help_names_the_marker_key_as_a_key(verb: str, tmp_path: Path) -> None:
    # `default_environment` is a top-level key of radar.toml. Written in brackets it reads
    # as a table header, and a table of that name is not what the verbs read.
    done = _run(tmp_path, verb, "--help")
    assert done.returncode == 0, done.stdout + done.stderr
    text = " ".join(done.stdout.split())
    assert "[default_environment]" not in text, text
    assert "default_environment" in text, text
