"""The root is resolved from a marker, and every consumer still imports.

TWO PROOFS, and they fail in different ways on purpose.

1. THE MARKER IS REQUIRED. A command that derives the radar root from its own
   `__file__` judges the tree the file happens to sit in, from wherever it is run. That
   stops working once the engine is installed rather than cloned: an installed `radar`
   command has no `__file__` near the catalogue, so the root has to come from a marker
   the caller stands inside. `test_a_command_without_a_marker_above_exits_2_naming_it`
   holds this: a resolver that follows `__file__` reaches the repository and exits 0 or
   1 with a report, never 2, and never says the word `radar.toml`.

2. NOTHING IMPORTS A NAME THAT NO LONGER EXISTS. Some of these modules are exercised
   by no test, and `ruff` does not resolve a symbol across modules, so a stale
   `from .radar_lib import ROOT` passes every check green and only raises the day
   somebody runs the command. An import is the only thing that catches it, so the import
   is the test - and a package has a second way for it to happen, a name that moved from
   one module to another, so the list is every module there is rather than the ones with
   a `main()`. A script nothing runs accumulates whatever nothing catches.

   It is a trap set for the change that removes or moves a name, not a statement about
   the tree it lands on, so it is green whenever nothing is broken.

The two classes between them cover the resolver itself: the order the three ways in are
consulted, and the `requires_framework` check every command runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stack_radar import __version__, paths, radar_lib

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))

MARKER = "radar.toml"


def marked(path: Path, *, requires: str | None = None) -> Path:
    """A directory that is a data root because it carries the marker, and nothing else."""
    path.mkdir(parents=True, exist_ok=True)
    body = 'schema = "radar-data/v1"\n'
    if requires:
        body += f'requires_framework = "{requires}"\n'
    (path / MARKER).write_text(body, encoding="utf-8")
    return path


# EVERY module in the package, not just the ones with a `main()`. A package has one
# more way to go wrong than a flat directory of scripts - a name that moved between
# modules - and the cheapest guard against it is importing the whole thing. Some of these
# are exercised by no test, and `ruff` does not resolve a symbol across modules, so a
# dangling import passes every other check green and only raises on the day someone runs
# the command.
SMOKE_MODULES = ("stack_radar",) + tuple(
    f"stack_radar.{path.stem}"
    for path in sorted((SRC / "stack_radar").glob("*.py"))
    if path.stem != "__init__"
)


def child_env(**extra: str) -> dict[str, str]:
    """The parent environment with the root override removed.

    `RADAR_DATA_ROOT` is the escape hatch the resolver reads before it searches, so a
    machine that happens to export it would point these subprocesses at a real tree and
    the marker-less case would silently stop being tested.
    """
    env = {**os.environ, **extra}
    env.pop("RADAR_DATA_ROOT", None)
    return env


def run(args: list[str], cwd: Path, *, timeout: int = 300, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_env(**kwargs),
    )


# Both invocations of the same command, because a `__file__` resolver fails differently
# in each and only one of them is quick. Bare, a resolver that followed `__file__` would
# reach the real repository and start the complete invariant scan over the machine's
# whole data plane, which can take minutes on a large one; with `--no-data-plane` it skips
# that half and reports on the catalogue in a second. Once the root comes from the
# marker, both fail before either code path is reached, so the slow case costs nothing on a
# correct tree - which is why the timeout below is short rather than generous.
GATE_INVOCATIONS = ((), ("--no-data-plane",))


@pytest.mark.parametrize("flags", GATE_INVOCATIONS, ids=["bare", "no-data-plane"])
def test_a_command_without_a_marker_above_exits_2_naming_it(tmp_path: Path, flags: tuple[str, ...]):
    """The root comes from a marker the caller stands inside, and this is its red proof.

    Exit 2, not 1: the gate spends 1 on FAIL findings, so a missing data root has to be
    distinguishable from a gate that ran and bit. And the marker is NAMED, because
    "no data root" without the filename is unactionable - the fix is to create or stand
    inside a `radar.toml`, and nothing else in the message says so.
    """
    # If the temporary directory ever sat under a radar root, the case under test would
    # not exist and the assertion would be about something else entirely.
    assert not any((p / MARKER).is_file() for p in (tmp_path, *tmp_path.parents)), (
        f"a {MARKER} above {tmp_path} makes this proof vacuous"
    )
    done = run(["-m", "stack_radar.gate", *flags], cwd=tmp_path, timeout=120, PYTHONPATH=str(SRC))
    output = done.stdout + done.stderr
    assert done.returncode == 2, (
        f"expected exit 2 from a directory with no {MARKER} above it, got "
        f"{done.returncode}:\n{output}"
    )
    assert MARKER in output, f"the failure never names {MARKER}:\n{output}"


@pytest.mark.parametrize(
    "argv",
    [
        ("-m", "stack_radar.gate", "--no-data-plane"),
        ("-m", "stack_radar.cli", "redact-backfill", "--check"),
        ("-m", "stack_radar.cli", "changelog-gate", "HEAD"),
    ],
    ids=["gate", "redact-backfill", "changelog-gate"],
)
def test_a_marker_that_is_not_valid_toml_exits_2_naming_it(tmp_path: Path, argv: tuple[str, ...]):
    # A table declared twice is a TOML error. It is named as a FAIL on exit 2, the exit a
    # run that never read its catalogue takes, rather than leaving as a traceback.
    root = marked(tmp_path / "radar")
    (root / MARKER).write_text(
        'schema = "radar-data/v1"\n[publish]\na = 1\n[publish]\nb = 2\n', encoding="utf-8"
    )
    done = run(list(argv), cwd=root, timeout=120, PYTHONPATH=str(SRC))
    output = done.stdout + done.stderr
    assert done.returncode == 2, output
    assert "Traceback" not in output, output
    assert "[FAIL]" in output or "::error::" in output, output
    assert MARKER in output and "not valid TOML" in output, output


class TestPrecedence:
    """`--data-root`, then RADAR_DATA_ROOT, then the search. Both overrides need a marker.

    The order is not arbitrary and it is not a convenience: the two overrides exist for CI
    and for nothing else, so the flag - the one a human typed on the
    command line just now - has to beat an environment variable that may have been
    exported by something else entirely.
    """

    def test_the_flag_wins_over_the_variable(self, tmp_path: Path):
        flagged, exported = marked(tmp_path / "flagged"), marked(tmp_path / "exported")
        os.environ[paths.ENV_VAR] = str(exported)
        try:
            found = paths.resolve_data_root(str(flagged), start=tmp_path)
        finally:
            del os.environ[paths.ENV_VAR]
        assert found == (flagged.resolve(), paths.FLAG)

    def test_the_variable_wins_over_the_search(self, tmp_path: Path):
        exported = marked(tmp_path / "exported")
        inside = marked(tmp_path / "inside") / "deep"
        inside.mkdir()
        os.environ[paths.ENV_VAR] = str(exported)
        try:
            found = paths.resolve_data_root(start=inside)
        finally:
            del os.environ[paths.ENV_VAR]
        assert found == (exported.resolve(), paths.ENV_VAR)

    def test_the_search_climbs_to_the_nearest_marker(self, tmp_path: Path):
        root = marked(tmp_path / "root")
        deep = root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert paths.resolve_data_root(start=deep) == (root.resolve(), MARKER)

    def test_an_override_without_a_marker_is_refused(self, tmp_path: Path):
        # The alternative is worse than it looks: an override that skipped the check would
        # let a mistyped path resolve to a real, empty directory, and the run would report
        # a catalogue of nothing rather than a mistake.
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(paths.DataRootNotFound) as raised:
            paths.resolve_data_root(str(bare))
        assert MARKER in str(raised.value)


class TestRequiresFramework:
    """The range is checked on every command, and the failure names both versions."""

    def test_a_version_inside_the_range_passes(self):
        assert radar_lib.framework_errors("0.2.0", ">=0.2.0,<0.3.0") == []

    def test_a_version_below_the_floor_is_named_with_the_clause(self):
        errs = radar_lib.framework_errors("0.1.0", ">=0.2.0,<0.3.0")
        assert errs == ["engine 0.1.0 does not satisfy >=0.2.0"]

    def test_an_unreadable_clause_is_refused_rather_than_ignored(self):
        # A range nobody can parse constrains nothing, and the silent reading of it is
        # indistinguishable from a range that holds.
        assert radar_lib.framework_errors("0.2.0", "~=0.2")

    def test_the_check_names_both_versions(self, tmp_path: Path):
        root = marked(tmp_path / "root", requires=">=9.0.0")
        with pytest.raises(radar_lib.FrameworkMismatch) as raised:
            radar_lib.check_framework(root)
        assert ">=9.0.0" in str(raised.value)
        assert __version__ in str(raised.value)

    def test_the_range_constrains_the_engine_not_the_catalogue(self, tmp_path: Path):
        # The catalogue's own version is its business. Read from the catalogue's
        # pyproject.toml, the check compared a catalogue with itself: this one would have
        # been refused for being 0.0.1, although the engine reading it is in range.
        root = marked(tmp_path / "root", requires=f">={__version__}")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "mini"\nversion = "0.0.1"\n', encoding="utf-8"
        )
        radar_lib.check_framework(root)


class TestDefaultEnvironment:
    """`--env` falls back to the marker's `default_environment`, and without the key to
    `default` - the profile `radar init` writes, so the fallback names one that exists."""

    def test_an_undeclared_default_is_the_one_init_writes(self, tmp_path: Path):
        root = marked(tmp_path / "root")
        assert radar_lib.default_environment(root) == "default"

    def test_a_declared_default_wins(self, tmp_path: Path):
        root = marked(tmp_path / "root")
        with (root / MARKER).open("a", encoding="utf-8") as fh:
            fh.write('default_environment = "laptop"\n')
        assert radar_lib.default_environment(root) == "laptop"


def test_every_script_still_imports():
    """The import smoke proof: `ruff` cannot see a symbol that left another module.

    Run as one interpreter over the whole list rather than one per module: that is the
    whole check in one command, and a single ImportError names the module it broke.
    """
    done = run(
        ["-c", "import " + ", ".join(SMOKE_MODULES)],
        cwd=REPO,
        PYTHONPATH=str(SRC),
    )
    assert done.returncode == 0, (
        "a module in the package no longer imports - a stale name survives every other "
        f"check green:\n{done.stdout}{done.stderr}"
    )
