"""The gate must say what it did NOT check.

Two of the gate's checks need a machine rather than a repository: the invariant
scan needs a data plane to grep, and the scope-leak scan needs the gitignored
`tools.local/` entries whose names it searches for. On a CI runner neither exists -
a committed profile points at paths that are absent there by construction - and a gate
that did not detect the absence would print, in substance,

    [NOTE] invariant: holds - 0 data-plane files scanned, 0 references

which is the wording of a real pass produced by scanning nothing. A gate that cannot
distinguish "checked and clean" from "had nothing to check" will eventually check
nothing and report success, which is the same decay an unverified
invariant undergoes.

These run gate.py as a subprocess against the miniature radar in tmp_path (conftest.py),
so they exercise argument parsing and the whole main() rather than a helper a refactor
could route around.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from stack_radar import __version__, gate
from stack_radar.init import compatible_range

ABSENT = "C:/definitely/absent"

VALID = """\
name = "ruff"
repo = "https://github.com/astral-sh/ruff"
axis = "lint-format"
artifact = "cli"
ring = "adopt"
license = "MIT"
visibility = "public"
note = "linter"

[telemetry]
match_command = ["ruff"]
since = "2026-08-01"

[[history]]
date = "2026-08-01"
ring = "adopt"
reason = "a fixture"
evidence = "e"
"""

# An `own` entry whose checkout is declared with the profile's `{documents}` placeholder,
# because one entry has to resolve on every machine. Private, as an own-ring entry with a
# local checkout has to be - a public entry's `repo` has to be a URL, and a checkout path
# is not one.
OWN = """\
name = "widget"
repo = "{documents}/widget"
axis = "lint-format"
artifact = "cli"
ring = "own"
license = "MIT"
visibility = "private"
note = "an own-ring tool whose checkout is declared with the profile's placeholder"

[telemetry]
match_command = ["widget"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "own"
reason = "a fixture"
evidence = "e"
"""

# The other shape an `own` entry takes: published, with no local checkout named at all:
# `repo` is a URL, so there is no worktree to scan and no placeholder to expand.
OWN_REMOTE = """\
name = "remote-widget"
repo = "https://github.com/example/remote-widget"
axis = "lint-format"
artifact = "cli"
ring = "own"
license = "MIT"
visibility = "public"
note = "an own-ring tool that names no local tree"

[telemetry]
match_command = ["remote-widget"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "own"
reason = "a fixture"
evidence = "e"
"""


def declare_documents(radar) -> Path:
    """Point the profile's `{documents}` at a directory inside the miniature radar.

    Written here rather than taught to conftest's `write_environment`: the fixture is
    the harness these tests are judged by, and it does not move in the change it judges.
    """
    docs = radar.root / "documents"
    docs.mkdir(exist_ok=True)
    (radar.root / "environments" / "default.toml").write_text(
        'name = "default"\n'
        'rings = ["own", "adopt", "pilot"]\n'
        "\n[paths]\n"
        f'claude_home = "{radar.home.as_posix()}"\n'
        f'documents = "{docs.as_posix()}"\n',
        encoding="utf-8",
    )
    return docs


def checkout(path: Path) -> Path:
    """A real git checkout. The scan enumerates a worktree by asking git, so a directory
    that is not one is a different case - and it has its own test below."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "init", "-q"], cwd=path, check=True, capture_output=True, timeout=60
    )
    return path


# --------------------------------------------------------------------- marker helpers
#
# The marker is REWRITTEN by the tests below rather than parameterised in conftest, for
# the same reason `declare_documents` writes the profile here: the fixture is the harness
# this change is judged by, and a harness that moves with the change it judges proves
# nothing. `requires_framework` is derived from the engine's own version
# (`compatible_range`, the same function `radar init` calls) rather than hand-pinned - the
# check compares against `stack_radar.__version__`, so a hand-pinned range would die on
# the version check before it ever reached the check under test, on the day the engine
# cuts a minor release rather than the day this fixture is touched.
MARKER = f"""\
schema = "radar-data/v1"
title = "mini-radar"
default_environment = "default"
requires_framework = "{compatible_range(__version__)}"

[invariant]
needles = [{{needles}}]
{{invariant_extra}}{{publish}}"""


def write_marker(
    radar,
    *,
    needles: tuple[str, ...] = ("mini-radar",),
    publish: str = "",
    exempt: dict[str, str] | None = None,
    claude_home_paths: tuple[str, ...] = (),
) -> None:
    """Rewrite the fixture's `radar.toml` with the needles, `[publish]` and the exemptions
    a test wants.

    A NEEDLE OTHER THAN THE FIXTURE'S DEFAULT is what most of these pass, so a test can
    tell a gate that read the marker it wrote from a gate that read something else. The
    same reasoning applies to `exempt`: the key a test declares is one nothing else in the
    fixture carries.
    """
    body = MARKER.format(
        needles=", ".join(f'"{n}"' for n in needles),
        publish=f"\n[publish]\n{publish}" if publish else "",
        invariant_extra=(
            "claude_home_paths = [" + ", ".join(f'"{c}"' for c in claude_home_paths) + "]\n"
            if claude_home_paths
            else ""
        ),
    )
    if exempt:
        rows = "\n".join(f'"{k}" = "{v}"' for k, v in exempt.items())
        body += f"\n[invariant.exempt]\n{rows}\n"
    (radar.root / "radar.toml").write_text(body, encoding="utf-8")


def own_entry(name: str, repo: str) -> str:
    """An `own` entry, the shape the engine's own takes: a local checkout in `repo`,
    written with the profile's placeholder, and private while the exception matches on
    that field."""
    return f"""\
name = "{name}"
repo = "{repo}"
axis = "lint-format"
artifact = "cli"
ring = "own"
license = "MIT"
visibility = "private"
note = "an own-ring entry, for the publication check"

[telemetry]
match_command = ["{name}"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "own"
reason = "a fixture"
evidence = "e"
"""


SCOPED = """\
name = "local-widget"
repo = "https://example.invalid/local-widget"
axis = "lint-format"
artifact = "cli"
ring = "adopt"
license = "MIT"
visibility = "private"
note = "a machine-local entry whose NAME must not travel"
redact_as = "local-lib"

[telemetry]
match_command = ["local-widget"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "adopt"
reason = "a fixture"
evidence = "e"
"""


def scoped_entry(radar) -> None:
    """Install a machine-local entry, and make the fixture root a checkout that ignores
    it - otherwise the scope-leak scan finds the name inside `tools.local/` itself and
    fails for a reason that has nothing to do with the check under test."""
    checkout(radar.root)
    (radar.root / ".gitignore").write_text("tools.local/\n", encoding="utf-8")
    (radar.root / "tools.local").mkdir(exist_ok=True)
    (radar.root / "tools.local" / "local-widget.toml").write_text(SCOPED, encoding="utf-8")


def engine(radar, name: str = "engine-radar") -> Path:
    """A checkout standing in for the engine's, inside the profile's `{documents}`.

    Named `engine-radar` rather than anything generic on purpose: the tests below plant
    it in data-plane files and read FAIL lines back, so the string has to be one no other
    part of the fixture can produce.
    """
    return checkout(declare_documents(radar) / name)


class TestAbsentDataPlane:
    def test_the_skipped_checks_are_named_one_by_one(self, radar):
        radar.tool("ruff", VALID)
        p = radar.gate("--claude-home", ABSENT)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "skipped: the invariant scan" in p.stdout
        assert "scope-leak scan" in p.stdout
        assert "machine probes" in p.stdout
        # The path it looked at is in the message: "no data plane" without saying where
        # it looked is unactionable on the machine where it is wrong.
        assert ABSENT.replace("/", "\\") in p.stdout or ABSENT in p.stdout
        assert "2 check(s) skipped without a data plane" in p.stdout

    def test_the_hints_name_verbs_not_module_files(self, radar):
        # An installed engine is run as `radar <verb>`; a hint naming a module file points
        # the reader at something they never type.
        radar.tool("ruff", VALID)
        p = radar.gate("--claude-home", ABSENT)
        assert "`radar apply`" in p.stdout and "`radar versions`" in p.stdout, p.stdout
        assert not re.search(r"\b[a-z_]+\.py\b", p.stdout), p.stdout

    def test_a_scan_of_nothing_does_not_claim_the_invariant_holds(self, radar):
        # Wording indistinguishable from a real pass is what this guards against.
        radar.tool("ruff", VALID)
        p = radar.gate("--claude-home", ABSENT)
        assert "invariant: holds" not in p.stdout

    def test_a_repo_only_failure_still_fails(self, radar):
        # Skipping the machine half must not soften the half that does run.
        radar.tool("ruff", VALID)
        radar.tool("broken", 'name = "broken"\nring = "adopt"\n')
        p = radar.gate("--claude-home", ABSENT)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] broken:" in p.stdout
        assert "skipped: the invariant scan" in p.stdout  # and it still says what it skipped

    def test_the_version_sites_are_still_compared(self, radar):
        # Version-site equality is a check a scheduled review runs; it reads two
        # tracked files, so an absent data plane is no excuse for it not to run.
        radar.tool("ruff", VALID)
        (radar.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [0.9.9] - 2026-09-03\n\nA heading nothing agrees with.\n",
            encoding="utf-8",
        )
        p = radar.gate("--claude-home", ABSENT)
        assert p.returncode == 1, p.stdout
        assert "[FAIL] version sites:" in p.stdout
        assert "0.9.9" in p.stdout


class TestNoDataPlaneRehearsal:
    """`--no-data-plane` answers as CI does, on a machine where CI's absences are false.

    `--claude-home <nonexistent>` alone does NOT reproduce a runner: the governed tool
    worktrees are still checked out on a developer machine and `tools.local/` is still
    there, so both machine-dependent checks keep running and the rehearsal proves
    nothing about what CI will see.
    """

    def test_it_skips_and_names_even_where_a_data_plane_exists(self, radar):
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate("--no-data-plane")
        assert p.returncode == 0, p.stdout + p.stderr
        assert "invariant: holds" not in p.stdout
        assert "--no-data-plane was asked for" in p.stdout
        assert "2 check(s) skipped without a data plane" in p.stdout


class TestPresentDataPlane:
    def test_a_reachable_data_plane_is_scanned_and_says_so(self, radar):
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "invariant: holds" in p.stdout
        assert "skipped: the invariant scan" not in p.stdout  # nothing was skipped

    def test_a_check_skipped_for_another_reason_is_not_blamed_on_the_data_plane(self, radar):
        # The invariant scan read the data plane; only the scope-leak scan was skipped, and
        # for want of tools.local/ entries. The lead-in and the summary say that, not that
        # the data plane was out of reach.
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "invariant: holds" in p.stdout, p.stdout
        assert "skipped: the scope-leak scan" in p.stdout, p.stdout
        assert "not reachable from here" not in p.stdout, p.stdout
        assert "without a data plane" not in p.stdout, p.stdout
        assert "1 check(s) did NOT run" in p.stdout, p.stdout

    def test_a_data_plane_reference_is_a_failure(self, radar):
        # The positive control: without it, "holds" only proves the scan ran, not that it
        # can bite. A data-plane file naming the control plane is the whole invariant.
        radar.tool("ruff", VALID)
        (radar.home / "CLAUDE.md").write_text(
            "See the mini-radar repo for the ring policy.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout

    def test_the_root_in_the_platform_s_own_spelling_is_a_failure(self, radar):
        # settings.json writes a Windows path with doubled backslashes, and CLAUDE.md with
        # single ones; both name the catalogue as surely as a forward-slash spelling does.
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text(
            json.dumps({"dir": str(radar.root.resolve() / "tools")}) + "\n", encoding="utf-8"
        )
        (radar.home / "CLAUDE.md").write_text(
            f"Read {radar.root.resolve() / 'tools'} first.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert p.stdout.count("[FAIL] invariant:") == 2, p.stdout

    @pytest.mark.skipif(os.name != "nt", reason="only Windows paths are case-insensitive")
    def test_the_root_in_another_case_is_a_failure_on_windows(self, radar):
        radar.tool("ruff", VALID)
        (radar.home / "CLAUDE.md").write_text(
            f"Read {radar.root.resolve().as_posix().upper()}/tools first.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout


class TestWorktreeRoots:
    """A governed worktree is declared with a placeholder, so it has to be expanded.

    Tested unexpanded, `Path("{documents}/widget").is_dir()` is false on every machine,
    so every entry declared with a placeholder would silently never become a root while
    the gate printed "holds" over the entries that happened to carry a literal path.
    Nothing would fail, and nothing would say so.
    """

    def test_a_worktree_declared_with_a_placeholder_is_scanned(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "notes.md").write_text(
            "see the mini-radar repo for the ring policy\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "notes.md" in p.stdout

    def test_a_root_git_cannot_enumerate_fails_instead_of_reporting_zero_of_zero(self, radar):
        # The hazard inherited from `tracked_files()`, which returns an empty list in
        # silence on a non-zero exit. An unenumerable root that contributes no files
        # prints the same "0 scanned / 0 eligible" a clean root does, so the failure to
        # look is indistinguishable from having looked.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        (declare_documents(radar) / "widget").mkdir()  # a directory, never a checkout
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "could not be enumerated" in p.stdout
        assert "NOT ENUMERABLE" in p.stdout
        assert "0 scanned / 0 eligible" not in p.stdout
        # git never ran, so the reason given is the rule that stopped it, not a git failure.
        assert "holds no .git" in p.stdout, p.stdout
        assert "`git ls-files` failed" not in p.stdout, p.stdout

    def test_a_checkout_in_a_subdirectory_of_a_repository_is_named_as_not_a_worktree_top(
        self, radar
    ):
        # A catalogue names the tree to enumerate, so the tree has to be the top of a
        # worktree: a directory inside one is refused, and the reason says so.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN.replace("{documents}/widget", "{documents}/mono/widget"))
        mono = checkout(declare_documents(radar) / "mono")
        (mono / "widget").mkdir()
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "holds no .git" in p.stdout, p.stdout
        assert "only the top level of a git worktree" in p.stdout, p.stdout

    def test_the_note_counts_scanned_and_eligible_under_each_root(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "a.md").write_text("clean\n", encoding="utf-8")
        (wt / "b.py").write_text("clean\n", encoding="utf-8")
        (wt / "c.bin").write_text("not an eligible suffix\n", encoding="utf-8")
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "widget: 2 scanned / 2 eligible" in p.stdout
        assert "<claude_home>/settings.json: 1 scanned / 1 eligible" in p.stdout

    def test_an_own_entry_that_never_becomes_a_root_is_named_with_its_reason(self, radar):
        # An absent root has to appear in the note rather than simply not exist, because
        # the roots are what the "holds" sentence is about.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        radar.tool("remote-widget", OWN_REMOTE)
        checkout(declare_documents(radar) / "widget")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "not a root: remote-widget" in p.stdout
        assert "remote URL only" in p.stdout
        assert "not a root: widget" not in p.stdout


class TestARelativeCheckoutPath:
    """A relative `repo` or `[publish].framework_worktree` names no checkout, wherever it
    points: it is refused, the refusal is named, and it identifies no engine entry."""

    def test_the_refusal_is_named_when_nothing_else_became_a_root(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN.replace("{documents}/widget", "../sibling"))
        p = radar.gate("--claude-home", ABSENT)
        assert "not a root: widget" in p.stdout, p.stdout
        assert "a relative path is refused" in p.stdout, p.stdout
        # `../sibling` points outside the data root, so the message must not say inside.
        assert "resolves inside" not in p.stdout, p.stdout

    def test_a_relative_framework_worktree_identifies_no_engine_entry(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN.replace("{documents}/widget", "vendor/engine"))
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "vendor/engine"\n',
        )
        p = radar.gate("--claude-home", ABSENT)
        assert "'widget' is a needle" not in p.stdout, p.stdout
        assert "the publication check did NOT run" in p.stdout, p.stdout
        assert "a relative path, and a relative path is refused" in p.stdout, p.stdout

    def test_the_refusal_is_named_when_no_profile_is_declared(self, radar):
        # With no environments/, no profile expands the value, and a relative path is
        # still refused by name rather than reported as tried nowhere.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN.replace("{documents}/widget", "vendor/engine"))
        write_marker(radar, publish='framework_worktree = "vendor/engine"\n')
        for f in (radar.root / "environments").iterdir():
            f.unlink()
        # No --claude-home either: that override is a profile of its own.
        p = radar.gate()
        assert "a relative path, and a relative path is refused" in p.stdout, p.stdout
        assert "not a root: widget" in p.stdout and "a relative path is refused" in p.stdout
        assert "(tried )" not in p.stdout, p.stdout

    def test_a_placeholder_no_profile_declares_is_not_called_relative(self, radar):
        # `{nowhere}/engine` is left as written when the profile has no `nowhere`. It is
        # unresolved, and the reason names what was tried, not a relative-path rule.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN.replace("{documents}/widget", "{nowhere}/widget"))
        write_marker(radar, publish='framework_worktree = "{nowhere}/engine"\n')
        p = radar.gate("--claude-home", ABSENT)
        assert "relative path" not in p.stdout, p.stdout
        assert "tried {nowhere}/widget" in p.stdout, p.stdout
        assert "tried {nowhere}/engine" in p.stdout, p.stdout


class TestTheDataRootInsideARepository:
    """The data root is the tree the gate is run in, chosen by whoever runs it, and it may
    be a subdirectory of a repository: its files are enumerated through that repository.
    Only a tree a CATALOGUE names has to be the top of a worktree."""

    def test_a_data_root_in_a_subdirectory_of_a_repository_is_scanned(self, radar):
        checkout(radar.root.parent)
        (radar.root / ".gitignore").write_text("tools.local/\n", encoding="utf-8")
        (radar.root / "tools.local").mkdir(exist_ok=True)
        (radar.root / "tools.local" / "local-widget.toml").write_text(SCOPED, encoding="utf-8")
        radar.tool("ruff", VALID)
        (radar.root / "notes.md").write_text("ported from local-widget\n", encoding="utf-8")
        p = radar.gate("--claude-home", ABSENT)
        assert "could not be enumerated" not in p.stdout, p.stdout
        assert "[FAIL] scope leak" in p.stdout, p.stdout
        assert "notes.md" in p.stdout, p.stdout

    def test_a_data_root_in_no_repository_names_why(self, radar):
        (radar.root / "tools.local").mkdir(exist_ok=True)
        (radar.root / "tools.local" / "local-widget.toml").write_text(SCOPED, encoding="utf-8")
        radar.tool("ruff", VALID)
        p = radar.gate("--claude-home", ABSENT)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] scope leak: the data root could not be enumerated" in p.stdout
        assert "not inside a git worktree" in p.stdout, p.stdout


class TestTheFeedbackExclusion:
    """`~/.claude/feedback` is excluded as ONE ROOT, never as a path component.

    The exclusion comes with a condition stated in the same breath: it is not to be
    written as an INVARIANT_SKIP_DIRS entry: that predicate matches a component at any
    depth, so the word would also delete `widget/docs/feedback/` from the scan, a
    directory the scan exists to reach.
    """

    def test_a_feedback_directory_inside_a_worktree_is_still_scanned(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "docs" / "feedback").mkdir(parents=True)
        (wt / "docs" / "feedback" / "report.md").write_text(
            "see the mini-radar repo\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout

    def test_the_claude_home_feedback_root_is_out_and_says_why(self, radar):
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        (radar.home / "feedback").mkdir()
        (radar.home / "feedback" / "report.md").write_text(
            "a mirrored report that names the mini-radar repo\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "<claude_home>/feedback is outside the scan by declaration" in p.stdout


class TestTheCeiling:
    """The file cap is `--fast`, not the default, and it has to admit when it cut.

    The cap is lowered for these tests, so a handful of files crosses it: what is under
    test is which run applies the cap and what it prints, not how long it takes to write
    six thousand files."""

    CAP = 5

    @staticmethod
    def _many(root: Path, n: int) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (root / f"f{i}.md").write_text("clean\n", encoding="utf-8")

    def test_the_cap_is_six_thousand_files_per_root(self):
        assert gate.INVARIANT_MAX_FILES == 6000

    def test_the_default_scan_has_no_ceiling(self, radar, monkeypatch):
        monkeypatch.setattr(gate, "INVARIANT_MAX_FILES", self.CAP)
        radar.tool("ruff", VALID)
        self._many(radar.home / "skills", self.CAP + 1)
        p = radar.call(gate.main)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "<claude_home>/skills: 6 scanned / 6 eligible" in p.stdout
        assert "TRUNCATED" not in p.stdout

    def test_fast_cuts_and_says_truncated(self, radar, monkeypatch):
        monkeypatch.setattr(gate, "INVARIANT_MAX_FILES", self.CAP)
        radar.tool("ruff", VALID)
        self._many(radar.home / "skills", self.CAP + 1)
        p = radar.call(gate.main, "--fast")
        assert p.returncode == 0, p.stdout + p.stderr
        assert "<claude_home>/skills: 5 scanned / 6 eligible" in p.stdout
        assert "TRUNCATED (--fast)" in p.stdout

    def test_the_fast_help_carries_no_measurement(self, radar):
        # The help describes the flag and carries no measured figure: a coverage
        # percentage is a property of a particular scan, not of the tool.
        p = radar.gate("--help")
        assert p.returncode == 0, p.stdout + p.stderr
        assert "TRUNCATED" in p.stdout
        assert "%" not in p.stdout


class TestTheDeclaredClaudeHomeFiles:
    """The claude_home allow-list is the engine's generic entries plus what the catalogue
    declares in `[invariant].claude_home_paths`. A file only one setup writes is named by
    that setup's catalogue, never by the engine.

    `tooling/overlay.toml` stands for any such file: a name the engine has no reason to
    know, so the scan can only reach it by reading the declaration.
    """

    DECLARED = "tooling/overlay.toml"

    def plant(self, radar) -> None:
        target = radar.home / self.DECLARED
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('note = "written from the mini-radar tree"\n', encoding="utf-8")

    def test_the_engine_names_only_generic_entries(self):
        from stack_radar.gate import CLAUDE_HOME_SUBPATHS

        assert set(CLAUDE_HOME_SUBPATHS) == {
            "settings.json",
            "CLAUDE.md",
            "skills",
            "plugins",
            "feedback-targets.toml",
        }

    def test_a_declared_sub_path_is_scanned_the_day_it_lands(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, claude_home_paths=(self.DECLARED,))
        self.plant(radar)
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "overlay.toml" in p.stdout
        assert f"<claude_home>/{self.DECLARED}: 1 scanned / 1 eligible" in p.stdout

    def test_an_undeclared_sub_path_is_not_scanned(self, radar):
        # The converse, which is what shows the list is read rather than hard-coded.
        radar.tool("ruff", VALID)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        self.plant(radar)
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FAIL] invariant:" not in p.stdout

    def test_a_declared_file_that_does_not_exist_yet_is_skipped_in_silence(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, claude_home_paths=(self.DECLARED,))
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[WARN]" not in p.stdout
        assert "overlay.toml" not in p.stdout

    def test_a_path_outside_claude_home_is_ignored_and_says_so(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, claude_home_paths=("../elsewhere.toml", "/etc/elsewhere.toml"))
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "'../elsewhere.toml' is not a path inside claude_home" in p.stdout
        assert "'/etc/elsewhere.toml' is not a path inside claude_home" in p.stdout


class TestAnExistingButEmptyClaudeHome:
    """A claude_home that exists and holds none of the scanned entries is not an absent
    claude_home, and the skip note says which of the two it is."""

    def test_it_says_none_of_the_scanned_files_exist_and_lists_them(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, claude_home_paths=("tooling/overlay.toml",))
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        out = p.stdout.replace("\\", "/")
        assert f"none of the scanned files exist under {radar.home.as_posix()}" in out
        assert "looked for: settings.json, CLAUDE.md, skills, plugins, " in out
        assert "tooling/overlay.toml" in out
        assert "no claude_home exists" not in out

    def test_an_absent_claude_home_is_still_called_absent(self, radar):
        radar.tool("ruff", VALID)
        p = radar.gate("--claude-home", ABSENT)
        assert "no claude_home exists at" in p.stdout
        assert "none of the scanned files exist" not in p.stdout


class TestTheNeedleComesFromTheMarker:
    """The needles are the catalogue's to declare, never a literal in the code.

    A needle written into the engine would scan every catalogue for a word it had not
    asked for and miss the word it had. Two failure modes, both silent and both printing
    "holds": a renamed catalogue whose real name is never searched for, and a needle that
    is also a published tool's name failing on every legitimate mention of that tool,
    which teaches the operator to skip the line.

    So the tests plant a needle only the marker declares, and the converse: a name the
    marker does not declare - here the engine's own published name - must not bite.
    """

    def test_a_needle_declared_in_the_marker_bites(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, needles=("private-catalogue",))
        (radar.home / "CLAUDE.md").write_text(
            "See the private-catalogue repo for the ring policy.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "private-catalogue" in p.stdout

    def test_a_name_the_marker_does_not_declare_is_not_a_needle(self, radar):
        # The other half, and the one only a marker-driven gate can pass: a catalogue
        # that does not declare `stack-radar` must not be scanned for it.
        radar.tool("ruff", VALID)
        write_marker(radar, needles=("private-catalogue",))
        (radar.home / "CLAUDE.md").write_text(
            "stack-radar is a published tool; naming it here is not a binding.\n",
            encoding="utf-8",
        )
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FAIL] invariant:" not in p.stdout

    def test_the_absolute_path_is_still_added_to_the_declared_needles(self, radar):
        # The data root's path is a needle in addition to the declared names. A needle
        # list is a list of NAMES, and a name alone is the weak half - radar.toml's own
        # comment says why: a generic one fires on ordinary prose, a stale one fires
        # nowhere, and both print the same word.
        radar.tool("ruff", VALID)
        write_marker(radar, needles=("private-catalogue",))
        (radar.home / "CLAUDE.md").write_text(
            f"cd {radar.root.as_posix()} && radar gate\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout

    def test_an_exemption_declared_in_the_marker_spares_the_file_and_is_printed(self, radar):
        # Exemptions are data for the same reason the needles are - plus one the needles
        # do not have: the key spells a path inside a repository that is not the engine's,
        # so as a constant it would put one catalogue's names into the engine's source.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "denylist.py").write_text('forbidden = ("private-catalogue",)\n', encoding="utf-8")
        write_marker(
            radar,
            needles=("private-catalogue",),
            exempt={"widget/denylist.py": "a negative assertion, not a dependency"},
        )
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FAIL] invariant:" not in p.stdout
        assert "invariant exemption in effect - widget/denylist.py" in p.stdout
        assert "a negative assertion, not a dependency" in p.stdout

    def test_the_same_file_fails_when_the_marker_declares_no_exemption(self, radar):
        # The converse, and the half that proves the exemption is READ rather than
        # inherited: the identical file, with the key absent, must bite.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "denylist.py").write_text('forbidden = ("private-catalogue",)\n', encoding="utf-8")
        write_marker(radar, needles=("private-catalogue",))
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "invariant exemption in effect" not in p.stdout

    def test_an_exemption_does_not_spare_a_file_its_key_does_not_name(self, radar):
        # THE CASE THE FIRST TWO MISS, and a one-line mutation proves it: replacing the
        # `endswith` lookup with `next(iter(exempt_map), None)` — exempt ANY file as soon as
        # any exemption is declared — leaves both of them green, and the whole suite with
        # them. With any `[invariant.exempt]` declared, that mutation switches the
        # invariant scan off entirely while still printing
        # "holds ... 1 exemption(s)": the same sentence a real pass produces, which is the
        # decay the module header exists to prevent. So a second file has to bite while the
        # first is spared, in the same run.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "denylist.py").write_text('forbidden = ("private-catalogue",)\n', encoding="utf-8")
        (wt / "notes.md").write_text("see the private-catalogue repo\n", encoding="utf-8")
        write_marker(
            radar,
            needles=("private-catalogue",),
            exempt={"widget/denylist.py": "a negative assertion, not a dependency"},
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "notes.md" in p.stdout
        # ...and the spared file is still spared, in the same run rather than another.
        assert "invariant exemption in effect - widget/denylist.py" in p.stdout
        assert "denylist.py" not in p.stdout.split("[FAIL] invariant")[1]

    def test_the_key_is_a_suffix_rather_than_a_substring(self, radar):
        # The docstrings and the catalogue's own comment say a key is an `endswith` suffix.
        # This is the test that fails if `posix.endswith(k)` becomes `k in posix`; every
        # other test here passes either way. A key that matches mid-path would spare files
        # nobody declared.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "denylist.py").write_text('forbidden = ("private-catalogue",)\n', encoding="utf-8")
        write_marker(
            radar,
            needles=("private-catalogue",),
            # A substring of the path, but not a suffix of it: `widget/deny` sits inside
            # `.../widget/denylist.py` and must NOT exempt it.
            exempt={"widget/deny": "a prefix, which is not what a suffix key means"},
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "invariant exemption in effect" not in p.stdout

    def test_a_malformed_exemption_is_ignored_and_says_so(self, radar):
        # The typo this catches parses as valid TOML: writing `[invariant.exempt.<key>]`
        # instead of a row INSIDE `[invariant.exempt]` gives the reader a nested table. A
        # reader that took every key would turn that into a LIVE exemption keyed by the
        # table name, whose printed "reason" is the repr of a dict - so the one
        # accountability mechanism the design has becomes noise, and a file nobody meant to
        # exempt is exempt.
        #
        # The rule: a declaration the code drops must not look like a declaration nobody
        # wrote. It is dropped AND named, and the scan stays stricter rather than looser.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        wt = checkout(declare_documents(radar) / "widget")
        (wt / "denylist.py").write_text('forbidden = ("private-catalogue",)\n', encoding="utf-8")
        body = (radar.root / "radar.toml").read_text(encoding="utf-8")
        (radar.root / "radar.toml").write_text(
            body.replace('needles = ["mini-radar"]', 'needles = ["private-catalogue"]')
            + '\n[invariant.exempt."widget/denylist.py"]\nreason = "written one level too deep"\n',
            encoding="utf-8",
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "IGNORED" in p.stdout
        assert "invariant exemption in effect" not in p.stdout

    def test_a_marker_that_declares_no_needle_says_the_scan_is_weaker(self, radar):
        # The failure radar.toml's own comment names: with no name needle the scan still
        # prints "holds", because the path needle alone carries the sentence. An
        # undeclared needle is reported rather than inferred.
        radar.tool("ruff", VALID)
        write_marker(radar, needles=())
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[invariant].needles" in p.stdout
        assert "only the data root's absolute path" in p.stdout


class TestTheEngineNameInExecutionFiles:
    """The engine's name is free in the data plane, three files apart.

    `settings.json`, any `hooks/hooks.json` and `CLAUDE.md` are the files that make a
    session RUN something, and they are what keeps the rule - "the radar is never an
    execution dependency" - checkable while the engine's name is also the name of a
    published tool. Mentioning the engine in a note is ordinary; wiring a hook to it is
    not.

    The engine is identified the way the gate identifies it everywhere else: the `own` entry
    whose `repo` resolves to `[publish].framework_worktree`. No second key declares the
    name, so the two readings cannot drift apart.
    """

    def _setup(self, radar) -> None:
        radar.tool("ruff", VALID)
        radar.tool("engine-radar", own_entry("engine-radar", "{documents}/engine-radar"))
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        engine(radar)

    def test_the_name_in_claude_md_is_a_failure(self, radar):
        self._setup(radar)
        (radar.home / "CLAUDE.md").write_text(
            "Always run engine-radar before committing.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "CLAUDE.md" in p.stdout

    def test_the_name_in_settings_json_is_a_failure(self, radar):
        self._setup(radar)
        (radar.home / "settings.json").write_text(
            '{"hooks": {"Stop": "engine-radar gate"}}\n', encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "settings.json" in p.stdout

    def test_the_name_in_any_plugin_hooks_file_is_a_failure(self, radar):
        self._setup(radar)
        hooks = radar.home / "plugins" / "some-plugin" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "hooks.json").write_text(
            '{"PostToolUse": [{"command": "engine-radar gate"}]}\n', encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] invariant:" in p.stdout
        assert "hooks.json" in p.stdout

    def test_the_name_anywhere_else_in_the_data_plane_is_free(self, radar):
        # The positive control the item turns on. If naming the engine failed everywhere,
        # the scan would be red the day somebody installs it - the outcome the needle
        # rules exist to avoid, because a needle that fires on a legitimate mention teaches
        # the operator to skip the FAIL line.
        self._setup(radar)
        (radar.home / "skills").mkdir()
        (radar.home / "skills" / "note.md").write_text(
            "engine-radar is one of the tools in the stack.\n", encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FAIL] invariant:" not in p.stdout

    def test_it_says_when_the_engine_has_no_name_yet(self, radar):
        # A checkout is declared, but until somebody writes its `own` entry there is
        # nothing to identify the engine BY, and a check that quietly stops looking is the
        # decay the gate keeps naming.
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        engine(radar)
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "the engine's name is NOT a needle" in p.stdout
        assert "no `own` entry's `repo` resolves to" in p.stdout


class TestThePublicationCheck:
    """`check_publishable`, the one check only the catalogue can run.

    The list of what must not travel - the catalogue's name and path, the machine-local
    tool names, the derived list of every `own` entry - exists only in the catalogue, so a
    public checkout's own CI checks FORMS and never names. The check reads `git ls-files`
    of the declared checkout and fails on any of them.

    `test_the_name_of_an_own_entry_fails` is the core case: a checkout carrying the name
    of an `own` entry.
    """

    OFF = "[NOTE] publication check: off ([publish].framework_worktree is not declared)"

    @staticmethod
    def about_publication(stdout: str) -> list[str]:
        return [
            ln
            for ln in stdout.splitlines()
            if "publication" in ln.lower() or "framework_worktree" in ln
        ]

    def test_an_undeclared_check_is_one_line_and_nothing_else(self, radar):
        # A catalogue that maintains no checkout for publication has not asked for the
        # check. One line says it is off and why; nothing else in the run mentions it, so
        # a stranger's first gate run is not a paragraph about a check they never wanted.
        radar.tool("ruff", VALID)
        write_marker(radar, needles=("private-catalogue",))
        (radar.home / "settings.json").write_text('{"ok": true}\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert self.about_publication(p.stdout) == [self.OFF], p.stdout
        assert "the engine's name is NOT a needle" not in p.stdout

    def test_the_rehearsal_says_the_same_single_line(self, radar):
        radar.tool("ruff", VALID)
        write_marker(radar, needles=("private-catalogue",))
        p = radar.gate("--no-data-plane")
        assert p.returncode == 0, p.stdout + p.stderr
        assert self.about_publication(p.stdout) == [self.OFF], p.stdout

    def test_the_name_of_an_own_entry_fails(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "README.md").write_text("Built alongside widget.\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] publication check" in p.stdout
        assert "widget" in p.stdout
        assert "README.md" in p.stdout

    def test_the_engine_s_own_entry_is_excepted_by_identity(self, radar):
        # The exception by identity. In this fixture the engine is an `own` entry named
        # after itself, so without the exception the check fails on every run, on the
        # engine's own pyproject and banners. It matches on `repo` AFTER expanding
        # `{documents}` and resolving both sides - a literal string compare never does.
        radar.tool("ruff", VALID)
        radar.tool("engine-radar", own_entry("engine-radar", "{documents}/engine-radar"))
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "pyproject.toml").write_text('[project]\nname = "engine-radar"\n', encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FAIL] publication check" not in p.stdout
        assert "is the engine's own entry" in p.stdout

    def test_a_sibling_own_entry_still_fails_beside_the_exception(self, radar):
        # The exception is for ONE entry, identified by its checkout. Every other `own`
        # name stays forbidden in the same run.
        radar.tool("ruff", VALID)
        radar.tool("engine-radar", own_entry("engine-radar", "{documents}/engine-radar"))
        radar.tool("widget", OWN)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "pyproject.toml").write_text('[project]\nname = "engine-radar"\n', encoding="utf-8")
        (wt / "docs.md").write_text("widget is in the same catalogue.\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "widget" in p.stdout

    def test_the_catalogue_s_name_and_path_fail(self, radar):
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "a.md").write_text("private-catalogue holds the catalogue.\n", encoding="utf-8")
        (wt / "b.md").write_text(f"cd {radar.root.as_posix()}\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "a.md" in p.stdout
        assert "b.md" in p.stdout

    def test_a_machine_local_tool_name_fails(self, radar):
        radar.tool("ruff", VALID)
        scoped_entry(radar)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "note.md").write_text("ported from local-widget\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] publication check" in p.stdout
        assert "local-widget" in p.stdout

    def test_one_string_forbidden_twice_is_one_finding(self, radar):
        # A machine-local entry at ring `own` is reached by two branches of the derived
        # list - it is a scoped name AND an `own` name - and the same leak reported twice
        # inflates the count the check ends on.
        radar.tool("ruff", VALID)
        scoped_entry(radar)
        (radar.root / "tools.local" / "local-widget.toml").write_text(
            SCOPED.replace('ring = "adopt"', 'ring = "own"'), encoding="utf-8"
        )
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "note.md").write_text("ported from local-widget\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        findings = [ln for ln in p.stdout.splitlines() if ln.startswith("[FAIL] publication check")]
        assert len(findings) == 1, findings
        assert "machine-local tool" in findings[0]

    def test_a_forbid_extra_term_fails(self, radar):
        # The operator's own list - an account name, a remote's host. A key rather than
        # a constant because only the catalogue knows the strings.
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish=(
                'framework_worktree = "{documents}/engine-radar"\nforbid_extra = ["example-org"]\n'
            ),
        )
        wt = engine(radar)
        (wt / "note.md").write_text("internal at example-org\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "forbid_extra" in p.stdout

    def test_a_forbidden_term_in_a_filename_fails(self, radar):
        # The same rule check_scope_leak follows: a name in a file's own name leaks from
        # a directory listing with the file unopened.
        radar.tool("ruff", VALID)
        radar.tool("widget", OWN)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "widget-port.md").write_text("nothing in the body.\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "FILENAME" in p.stdout
        assert "widget-port.md" in p.stdout

    def test_path_home_fails_outside_bootstrap_and_passes_inside_it(self, radar):
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "bootstrap.py").write_text("home = Path.home()\n", encoding="utf-8")
        clean = radar.gate()
        assert clean.returncode == 0, clean.stdout + clean.stderr
        (wt / "versions.py").write_text("home = Path.home()\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "Path.home()" in p.stdout
        assert "versions.py calls `Path.home()`" in p.stdout
        # The exempt module is named in the guidance the FAIL prints, so the assertion
        # has to be about the SUBJECT of a finding rather than about the word appearing.
        assert "bootstrap.py calls `Path.home()`" not in p.stdout

    def test_a_mention_of_the_call_is_not_the_call(self, radar):
        # The rule reads the parsed module: a comment, a docstring or a message that names
        # `Path.home()` finds no home; a text match would fire on every one of them.
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "versions.py").write_text(
            '"""Only bootstrap calls `Path.home()`."""\n'
            "# never Path.home() here\n"
            'HINT = "calls `Path.home()` - only bootstrap.py may"\n',
            encoding="utf-8",
        )
        clean = radar.gate()
        assert clean.returncode == 0, clean.stdout + clean.stderr
        (wt / "versions.py").write_text(
            'import os\nhome = os.environ["USERPROFILE"]\n', encoding="utf-8"
        )
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "versions.py calls `os.environ['USERPROFILE']` (line 2)" in p.stdout

    def test_a_real_user_path_fails_and_a_placeholder_does_not(self, radar):
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        wt = engine(radar)
        (wt / "docs.md").write_text(
            "Put it under C:/Users/{user}/Documents, or C:/Users/<you>/Documents.\n",
            encoding="utf-8",
        )
        clean = radar.gate()
        assert clean.returncode == 0, clean.stdout + clean.stderr
        # Concatenated, so this file carries no account literal of its own.
        leak = "C:" + "/Users/someone/Documents"
        (wt / "leak.md").write_text(f"Mine lives at {leak}\n", encoding="utf-8")
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "leak.md" in p.stdout
        assert "docs.md" not in p.stdout

    def test_a_checkout_git_cannot_enumerate_is_a_failure(self, radar):
        # The same rule the invariant roots follow: a root that could not be read prints
        # the wording of a clean one unless the failure to look is itself reported.
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        (declare_documents(radar) / "engine-radar").mkdir()  # a directory, never a checkout
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] publication check" in p.stdout
        assert "could not be enumerated" in p.stdout

    def test_a_declared_checkout_that_is_absent_is_named_rather_than_passed(self, radar):
        # CI has no engine checkout, and the key lives in the tracked marker. Absence is
        # a named non-run, never a FAIL and never a pass.
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        declare_documents(radar)
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "publication check did NOT run" in p.stdout
        assert "engine-radar" in p.stdout

    def test_the_rehearsal_does_not_pretend_to_have_run_it(self, radar):
        radar.tool("ruff", VALID)
        write_marker(
            radar,
            needles=("private-catalogue",),
            publish='framework_worktree = "{documents}/engine-radar"\n',
        )
        engine(radar)
        p = radar.gate("--no-data-plane")
        assert p.returncode == 0, p.stdout + p.stderr
        assert "publication check did NOT run" in p.stdout
        assert "--no-data-plane" in p.stdout


# A public entry pointing at a local checkout, never a URL.
PUBLIC_LOCAL_REPO = """\
name = "local-tool"
repo = "C:/Users/example/local-tool"
axis = "lint-format"
artifact = "cli"
ring = "adopt"
license = "MIT"
visibility = "public"
note = "a public entry whose repo is a local path, not a URL"

[telemetry]
match_command = ["local-tool"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "adopt"
reason = "a fixture"
evidence = "e"
"""

# The admitted exception: a phantom tool with no repository at all.
PUBLIC_SENTINEL_REPO = """\
name = "phantom-tool"
repo = "(repo does not exist)"
axis = "agent-plugins"
artifact = "unknown"
ring = "discard"
license = "n/a"
visibility = "public"
note = "a phantom entry, admitted by the sentinel"

[[history]]
date = "2026-09-01"
ring = "discard"
reason = "a fixture"
evidence = "e"
"""


class TestPublicVisibilityNeedsAURLOrTheSentinel:
    """render.py writes a public entry's `repo` as a Markdown link, so a public
    entry naming a local checkout would publish that path. The one admitted exception
    is the literal `(repo does not exist)` sentinel, for a tool that never had a
    repository."""

    def test_a_public_entry_with_a_local_repo_fails(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("local-tool", PUBLIC_LOCAL_REPO)
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] local-tool:" in p.stdout
        assert "visibility=public" in p.stdout

    def test_a_public_entry_with_the_sentinel_passes(self, radar):
        radar.tool("ruff", VALID)
        radar.tool("phantom-tool", PUBLIC_SENTINEL_REPO)
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "phantom-tool" not in p.stdout
