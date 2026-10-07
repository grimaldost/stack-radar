"""Red proofs for the record-or-declare gate.

`evaluate` is pure - the tests up top feed it synthetic diffs and prove each arm can
fail, plus the one non-vacuity guard the advisory arm needs: the contract-surface list
must keep naming files that exist, or a rename silently retires the warning while the
workflow stays green. What `evaluate` is *given* - which commits are merges and what
each one contributed - is `main`'s half of the decision and the harder one to get right,
so the record-or-declare and merge cases are proven end to end instead: a real git
repository in `tmp_path` and the real script as a subprocess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from engine_run import run_engine

from stack_radar.changelog_gate import (
    CONTRACT_SURFACES,
    ENGINE_PREFIXES,
    MARKER,
    SECTION_VOCABULARY,
    DeclarationRefused,
    evaluate,
    watched,
)

_ROOT = Path(__file__).resolve().parent.parent
_SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (_ROOT / "src"))
# Reached by MODULE NAME, the way the `radar changelog-gate` verb reaches it. Running the
# file by path would import it as a top-level module, and a package module imported that
# way has no `__package__` to resolve its siblings against.
_COMMAND = ["-m", "stack_radar.changelog_gate"]


def _evaluate(
    changed: list[str],
    messages: str = "fix: something\n",
    changelog_added: str = "",
    changelog_removed: str = "",
    base_version: tuple[int, int, int] | None = (0, 2, 0),
    head_version: tuple[int, int, int] | None = (0, 2, 0),
) -> tuple[list[str], list[str]]:
    """Single-commit convenience: `changed`/`messages` are that one commit's own diff
    and message, which is also the whole range's aggregate for the other checks."""
    commits = [(changed, messages)]
    return evaluate(
        changed, commits, changelog_added, changelog_removed, base_version, head_version
    )


class TestRecordOrDeclare:
    def test_a_code_change_without_a_changelog_entry_fails(self):
        errors, _ = _evaluate(["src/stack_radar/field_report.py"])
        assert len(errors) == 1
        assert "CHANGELOG.md" in errors[0]

    def test_a_code_change_with_a_changelog_entry_passes(self):
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py", "CHANGELOG.md"], changelog_added="- a line\n"
        )
        assert errors == []

    def test_the_declared_exemption_carries_the_change(self):
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py"],
            messages="refactor: rename a local\n\nChangelog: none (an internal rename)\n",
        )
        assert errors == []

    def test_a_bare_declaration_without_a_reason_does_not_count(self):
        # The parenthesis is the reviewable half. A bare `Changelog: none` records that
        # the rule was skipped and nothing about why, which is an opt-out with no cost.
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py"], messages="refactor: x\n\nChangelog: none\n"
        )
        assert len(errors) == 1

    def test_the_declared_exemption_is_case_insensitive(self):
        # A casing typo states the same decision; rejecting it would mislead, not protect.
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py"],
            messages="refactor: x\n\nchangelog: None (an internal rename)\n",
        )
        assert errors == []

    def test_content_carries_no_recording_obligation(self):
        # A ring change records itself in the entry's own dated [[history]] with a reason
        # and evidence, and gate.py checks that. Demanding a CHANGELOG line as well would
        # put one fact in two places.
        errors, _ = _evaluate(["tools/ruff.toml", "field/ruff/2026-09-03.json", "snapshots/x.json"])
        assert errors == []

    def test_docs_and_workflows_carry_no_obligation(self):
        errors, _ = _evaluate(["docs/concepts.md", ".github/workflows/ci.yml", "CONTRIBUTING.md"])
        assert errors == []

    def test_windows_path_separators_are_normalized(self):
        errors, _ = _evaluate(["src\\stack_radar\\field_report.py"])
        assert len(errors) == 1

    def test_a_whitespace_only_changelog_edit_does_not_record_the_change(self):
        # A trailing-space edit to a blank line, or an appended blank line, produces a
        # non-empty added diff that carries no content at all.
        errors, _ = _evaluate(["src/stack_radar/field_report.py"], changelog_added="   \n\n")
        assert len(errors) == 1
        assert "CHANGELOG.md" in errors[0]

    def test_an_added_line_the_diff_also_removes_does_not_record_the_change(self):
        # Red proof for the harder half: a trailing space on an *existing* line adds a
        # line that survives strip(). It is a record only if the same diff does not
        # also remove it - which, in collapsed form, it does.
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py"],
            changelog_added="- an old line \n",
            changelog_removed="- an old line\n",
        )
        assert len(errors) == 1
        assert "CHANGELOG.md" in errors[0]

    def test_one_new_line_among_re_added_ones_records_the_change(self):
        # The positive counterpart: churn around a genuinely new line is still a record.
        errors, _ = _evaluate(
            ["src/stack_radar/field_report.py"],
            changelog_added="  - an old line\n- a new line\n",
            changelog_removed="- an old line\n",
        )
        assert errors == []


class TestRecordOrDeclareIsPerCommit:
    def test_a_trailer_on_one_commit_does_not_exempt_a_different_untrailered_commit(self):
        # Commit A touches src/ with no changelog and no trailer; commit B is
        # unrelated and carries the trailer. The declaration is reviewable for B and
        # must not silently cover A.
        commits = [
            (["src/stack_radar/field_report.py"], "fix: change engine behavior\n"),
            (["docs/concepts.md"], "chore: tidy a comment\n\nChangelog: none (comment only)\n"),
        ]
        errors, _ = evaluate([], commits, "", "", (0, 2, 0), (0, 2, 0))
        assert len(errors) == 1
        assert "field_report.py" in errors[0]

    def test_a_commit_that_deletes_the_changelog_still_fails(self):
        # A commit touching src/ that only deletes or whitespace-edits
        # CHANGELOG.md must still fail - touching the file is not recording the change.
        commits = [
            (["src/stack_radar/field_report.py", "CHANGELOG.md"], "fix: change engine behavior\n")
        ]
        errors, _ = evaluate([], commits, "", "", (0, 2, 0), (0, 2, 0))
        assert len(errors) == 1

    def test_a_trailer_on_the_commit_that_touches_the_engine_passes(self):
        commits = [
            (
                ["src/stack_radar/field_report.py"],
                "fix: change engine behavior\n\nChangelog: none (internal only)\n",
            ),
            (["docs/concepts.md"], "chore: tidy a comment\n"),
        ]
        errors, _ = evaluate([], commits, "", "", (0, 2, 0), (0, 2, 0))
        assert errors == []

    def test_an_added_changelog_line_exempts_every_engine_commit_in_the_range(self):
        commits = [
            (["src/stack_radar/field_report.py"], "fix: change engine behavior\n"),
            (["src/stack_radar/gate.py"], "fix: another engine change\n"),
        ]
        errors, _ = evaluate([], commits, "- Recorded the change.\n", "", (0, 2, 0), (0, 2, 0))
        assert errors == []


# A merge commit is not skipped - main charges it with its resolution, the content an
# automatic merge of its parents would not have produced. Which commits are merges and
# what they resolved is main's job, not evaluate's, so it is proven end to end below (a
# real repo, real merges, the real script as a subprocess) rather than through evaluate
# directly: see the test_end_to_end_*merge* cases.


class TestMergeChargeRefusesToGuess:
    def test_a_merge_tree_that_wrote_no_tree_fails_naming_the_git_version(self):
        # Anything that is not a tree and not the one known-safe refusal raises, naming
        # the git it ran. This fixture is the wrong-argument-count shape, which is a
        # usage error on stdout - kept because it exercises the raise path through a
        # second door, not because it is what a pre-2.38 git prints for this argv: that
        # is `fatal: unknown rev --write-tree`, and the test below pins it. The one
        # exception is `refusing to merge unrelated histories`, which is a fact about
        # the merge rather than about the machine and is deliberately answered with the
        # combined diff.
        from stack_radar.changelog_gate import _auto_merge_tree

        refused = subprocess.CompletedProcess(
            args=["git", "merge-tree", "--write-tree", "a", "b"],
            returncode=129,
            stdout="usage: git merge-tree [--trivial-merge] <base-tree> <branch1> <branch2>\n",
            stderr="",
        )
        with pytest.raises(RuntimeError) as failure:
            _auto_merge_tree(refused, "git version 2.30.0\n")
        assert "git version 2.30.0" in str(failure.value)
        assert "2.38" in str(failure.value)

    def test_a_conflicted_merge_tree_is_still_a_tree(self):
        # Exit 1 is a *conflicted* automatic merge, not a failure: merge-tree writes the
        # tree with the conflict markers in it, and diffing that against the merge
        # commit is exactly how a hand resolution is told from an automatic one.
        from stack_radar.changelog_gate import _auto_merge_tree

        conflicted = subprocess.CompletedProcess(
            args=["git", "merge-tree", "--write-tree", "a", "b"],
            returncode=1,
            stdout="0" * 40 + "\n\n100644 " + "1" * 40 + " 1\tsrc/engine.py\n",
            stderr="",
        )
        assert _auto_merge_tree(conflicted, "git version 9.9.9") == "0" * 40

    def test_a_pair_git_refuses_to_merge_is_no_tree_and_no_failure(self):
        # The boundary the old-git test does not cross. `refusing to merge unrelated
        # histories` is a statement about this *pair*, not about this *git*: there is no
        # automatic merge to compare against, so None is the answer and the caller
        # charges the combined diff. Raising here - which is what a version check alone
        # does, since the git is new enough - costs the verdict entirely: the gate would
        # exit on a traceback with no ::error:: line and tell the contributor to upgrade
        # a git that is already new enough for --write-tree.
        from stack_radar.changelog_gate import _auto_merge_tree

        refused = subprocess.CompletedProcess(
            args=["git", "merge-tree", "--write-tree", "a", "b"],
            returncode=128,
            stdout="",
            stderr="fatal: refusing to merge unrelated histories\n",
        )
        assert _auto_merge_tree(refused, "git version 9.9.9") is None

    def test_the_message_a_real_pre_2_38_git_prints_here_still_raises(self):
        # `git merge-tree --write-tree A B` is four arguments, which is exactly what
        # pre-2.38 cmd_merge_tree expects, so it never reaches its usage branch: it
        # calls get_tree_descriptor on `--write-tree` and dies `unknown rev
        # --write-tree`. Classifying by a pattern for the usage string would let this -
        # the message that actually occurs - fall through to the silent combined-diff
        # fallback on precisely the machines that cannot detect the bug. Only the
        # known-safe refusal returns None.
        from stack_radar.changelog_gate import _auto_merge_tree

        old = subprocess.CompletedProcess(
            args=["git", "merge-tree", "--write-tree", "a", "b"],
            returncode=128,
            stdout="",
            stderr="fatal: unknown rev --write-tree\n",
        )
        with pytest.raises(RuntimeError, match="2.38 or newer"):
            _auto_merge_tree(old, "git version 2.30.0")

    def test_a_parent_git_cannot_read_raises_rather_than_falling_back(self):
        # The other half of the same direction. A modern git that cannot read one of the
        # parents says so in its own words, and that is neither a tree nor a statement
        # about this merge - a shallow clone would produce it, and answering it with the
        # combined diff would charge content nobody authored while claiming the
        # resolution was read.
        from stack_radar.changelog_gate import _auto_merge_tree

        unreadable = subprocess.CompletedProcess(
            args=["git", "merge-tree", "--write-tree", "a", "b"],
            returncode=128,
            stdout="",
            stderr=(
                "merge-tree: 0123456789abcdef0123456789abcdef01234567 - "
                "not something we can merge\n"
            ),
        )
        with pytest.raises(RuntimeError, match="wrote no tree"):
            _auto_merge_tree(unreadable, "git version 9.9.9")


class TestReleaseHeading:
    def test_a_heading_must_match_the_version_site(self):
        errors, _ = _evaluate(
            ["CHANGELOG.md"],
            changelog_added="## [0.3.0] - 2026-09-10\n",
            base_version=(0, 2, 0),
            head_version=(0, 2, 0),
        )
        assert any("pyproject.toml is at 0.2.0" in e for e in errors)

    def test_a_heading_must_move_the_version_forward(self):
        errors, _ = _evaluate(
            ["CHANGELOG.md"],
            changelog_added="## [0.2.0] - 2026-09-10\n",
            base_version=(0, 2, 0),
            head_version=(0, 2, 0),
        )
        assert any("does not move the version forward" in e for e in errors)

    def test_a_real_cut_passes(self):
        errors, _ = _evaluate(
            ["CHANGELOG.md", "pyproject.toml"],
            changelog_added="## [0.3.0] - 2026-09-10\n### Added\n- a thing\n",
            base_version=(0, 2, 0),
            head_version=(0, 3, 0),
        )
        assert errors == []


class TestSectionVocabulary:
    def test_a_heading_outside_the_vocabulary_fails(self):
        # This asserts what the message *says*, not only that it fired: the offending
        # heading by name, and the vocabulary to choose from. Both are what makes the
        # failure actionable, and asserting only the phrase would survive a message that
        # named neither.
        errors, _ = _evaluate(["CHANGELOG.md"], changelog_added="### Documentation\n- a line\n")
        assert len(errors) == 1, errors
        assert "outside the vocabulary" in errors[0]
        assert "Documentation" in errors[0]
        assert " / ".join(SECTION_VOCABULARY) in errors[0]

    def test_the_six_keep_a_changelog_headings_pass(self):
        added = "\n".join(f"### {h}" for h in SECTION_VOCABULARY)
        errors, _ = _evaluate(["CHANGELOG.md"], changelog_added=added + "\n")
        assert errors == []

    def test_deeper_and_shallower_headings_are_not_section_headings(self):
        # `## [0.3.0]` and `#### detail` are other grammar; only `### ` carries the
        # vocabulary.
        errors, _ = _evaluate(
            ["CHANGELOG.md"],
            changelog_added="## [0.3.0] - 2026-09-10\n#### a nested note\n- a bullet\n",
            base_version=(0, 2, 0),
            head_version=(0, 3, 0),
        )
        assert errors == []

    def test_the_vocabulary_check_reads_only_the_added_lines(self):
        # History is grandfathered: an ordinary bullet under an existing heading raises
        # nothing.
        errors, _ = _evaluate(
            ["CHANGELOG.md"], changelog_added="- an ordinary bullet under an existing heading\n"
        )
        assert errors == []


class TestConsumerAffectingMarker:
    def test_touching_a_contract_surface_without_the_marker_warns_only(self):
        errors, warnings = _evaluate(
            ["src/stack_radar/radar_lib.py", "CHANGELOG.md"], changelog_added="- a line\n"
        )
        assert errors == []
        assert len(warnings) == 1
        assert MARKER in warnings[0]

    def test_the_marker_silences_it(self):
        _, warnings = _evaluate(
            ["src/stack_radar/radar_lib.py", "CHANGELOG.md"], changelog_added=f"- a line {MARKER}\n"
        )
        assert warnings == []

    def test_an_ordinary_script_does_not_warn(self):
        _, warnings = _evaluate(
            ["src/stack_radar/render.py", "CHANGELOG.md"], changelog_added="- a line\n"
        )
        assert warnings == []

    def test_the_contract_surface_list_names_files_that_exist(self):
        # Non-vacuity: a renamed surface file would retire the advisory without a sound.
        from stack_radar.changelog_gate import CONTRACT_SURFACES

        missing = [path for path in CONTRACT_SURFACES if not (_ROOT / path).is_file()]
        assert not missing, f"CONTRACT_SURFACES names files that do not exist: {missing}"


class TestWhatIsWatchedIsDeclaredByTheRepository:
    """One engine, two kinds of repository, and each says what it ships.

    The defaults are the engine's own, so a repository with no marker - this one, and the
    engine's CI - keeps working with no declaration at all. A catalogue declares
    `[changelog]` instead, because inheriting `src/` into a tree that has none would leave
    the gate watching an empty set and passing every run in silence.
    """

    def test_no_marker_means_the_engine_defaults(self, tmp_path: Path):
        prefixes, surfaces = watched(tmp_path)
        assert prefixes == ENGINE_PREFIXES
        assert surfaces == CONTRACT_SURFACES

    def test_a_declaration_replaces_both(self, tmp_path: Path):
        (tmp_path / "radar.toml").write_text(
            '[changelog]\nprefixes = ["scripts/"]\ncontract_surfaces = ["scripts/radar_lib.py"]\n',
            encoding="utf-8",
        )
        assert watched(tmp_path) == (("scripts/",), ("scripts/radar_lib.py",))

    def test_a_marker_without_the_block_still_takes_the_defaults(self, tmp_path: Path):
        (tmp_path / "radar.toml").write_text('schema = "radar-data/v1"\n', encoding="utf-8")
        assert watched(tmp_path) == (ENGINE_PREFIXES, CONTRACT_SURFACES)

    def test_one_key_declared_leaves_the_other_at_its_default(self, tmp_path: Path):
        (tmp_path / "radar.toml").write_text(
            '[changelog]\nprefixes = ["scripts/"]\n', encoding="utf-8"
        )
        prefixes, surfaces = watched(tmp_path)
        assert prefixes == ("scripts/",)
        assert surfaces == CONTRACT_SURFACES

    def test_an_empty_declaration_is_refused_rather_than_obeyed(self, tmp_path: Path):
        # The silent-pass failure, made loud. `prefixes = []` charges no commit with
        # anything and every run reports OK; a repository that means "watch nothing" has
        # no business running this gate at all.
        (tmp_path / "radar.toml").write_text("[changelog]\nprefixes = []\n", encoding="utf-8")
        with pytest.raises(DeclarationRefused) as raised:
            watched(tmp_path)
        assert "empty" in str(raised.value)

    def test_an_unreadable_declaration_is_refused_rather_than_ignored(self, tmp_path: Path):
        (tmp_path / "radar.toml").write_text('[changelog]\nprefixes = "src/"\n', encoding="utf-8")
        with pytest.raises(DeclarationRefused):
            watched(tmp_path)

    def test_the_declared_prefix_is_what_evaluate_charges(self, tmp_path: Path):
        # The declaration reaches the decision, not just the reader: a catalogue path is
        # charged under the catalogue's own prefixes and the engine's default is not.
        errors, _ = evaluate([], [(["scripts/gate.py"], "fix: x\n")], "", "", (0, 2, 0), (0, 2, 0))
        assert errors == [], "src/ is the default and scripts/ must not match it"
        errors, _ = evaluate(
            [],
            [(["scripts/gate.py"], "fix: x\n")],
            "",
            "",
            (0, 2, 0),
            (0, 2, 0),
            prefixes=("scripts/",),
        )
        assert len(errors) == 1


# --- end to end: a real repo, a real merge, the real script as a subprocess --------
#
# `evaluate` is pure and the tests above drive it directly, but two things live only in
# `main`: which commits are merges (and what their own resolution diff is), and what the
# script actually prints and exits with. These run the real command unchanged,
# as CI does, against a repo built from real commits - which makes them the black-box
# counterpart of the record-or-declare red proofs: point the same test, run unchanged,
# at a copy of the script that lacks the rule (swapped in at the same path) and it fails
# for the rule's reason, not from a signature mismatch or a `changed=[]` such a copy
# could never fault.


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _wide(top: str = "top", bottom: str = "bottom") -> str:
    """An engine file long enough that an edit near its top and one near its bottom fall
    in separate diff hunks, so two branches can each edit it and still merge cleanly."""
    lines = [f"value_{n} = {n}" for n in range(40)]
    lines[1] = f"value_1 = {top!r}"
    lines[38] = f"value_38 = {bottom!r}"
    return "\n".join(lines) + "\n"


# The base repository, built once per module and copied into each test's tmp_path. Every
# end-to-end test starts from the same commit, and building it is a run of git processes
# that costs more than most of the tests themselves.
_BASE_REPO: list[Path] = []


@pytest.fixture(scope="module", autouse=True)
def _base_repo(tmp_path_factory: pytest.TempPathFactory):
    repo = tmp_path_factory.mktemp("changelog-gate-base")
    _build_base_repo(repo)
    _BASE_REPO.append(repo)
    yield
    _BASE_REPO.remove(repo)


def _init_repo(repo: Path) -> None:
    """One commit on branch `base`: two engine files, a docs file, an open
    `[Unreleased]` section, and pyproject.toml at 0.2.0 - a PR branch builds on top of
    this. A copy of the module's base repository, which is built by the same steps."""
    shutil.copytree(_BASE_REPO[-1], repo, dirs_exist_ok=True)


def _build_base_repo(repo: Path) -> None:
    _git(["init", "-q", "-b", "main", "."], cwd=repo)
    _git(["config", "user.email", "t@t"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)
    _git(["config", "commit.gpgsign", "false"], cwd=repo)
    # Pinned, not inherited: the CRLF red proof below commits a file whose only change is
    # its line endings, and a machine whose global core.autocrlf is true would normalize
    # that change away before the gate ever saw it.
    _git(["config", "core.autocrlf", "false"], cwd=repo)
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- an old line\n- another old line\n",
        encoding="utf-8",
    )
    (repo / "src" / "engine.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "wide.py").write_text(_wide(), encoding="utf-8")
    (repo / "docs" / "backlog.md").write_text("docs\n", encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", "chore: base"], cwd=repo)
    _git(["branch", "base"], cwd=repo)


def _octopus(repo: Path, message: str, *branches: str) -> None:
    """Commit an octopus merge of `branches` into the current branch, as `git merge` would.

    Built with plumbing: the branches are merged one at a time to get the merged tree,
    and one commit with every head as a parent is written over it. The result is the
    commit `git merge <b> <c>` makes when nothing conflicts - the same parents, the same
    tree - without its octopus strategy, a shell script that starts many processes and
    takes seconds on some machines.
    """
    start = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    for branch in branches:
        _git(["merge", "-q", "--no-ff", "-m", f"step {branch}", branch], cwd=repo)
    tree = _git(["rev-parse", "HEAD^{tree}"], cwd=repo).strip()
    parents = [arg for b in (start, *branches) for arg in ("-p", b)]
    commit = _git(["commit-tree", tree, *parents, "-m", message], cwd=repo).strip()
    _git(["reset", "-q", "--hard", commit], cwd=repo)


def _commit(repo: Path, message: str, edits: dict[str, str]) -> None:
    """Write each `path -> content` edit and commit them with `message`."""
    for rel, content in edits.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", message], cwd=repo)


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the real command against `repo`, base ref `base` (tests/engine_run.py says when
    that is a subprocess)."""
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return run_engine(_COMMAND[1].removeprefix("stack_radar."), ["base"], cwd=repo, env=env)


class TestEndToEndRecordOrDeclare:
    def test_a_trailer_on_one_commit_does_not_exempt_another(self, tmp_path: Path) -> None:
        # Commit A touches the engine with no trailer; commit B is unrelated and carries
        # the trailer. A trailer search over every message in the range concatenated
        # would find B's trailer and exempt A.
        _init_repo(tmp_path)
        _commit(tmp_path, "fix: change engine behavior\n", {"src/engine.py": "x = 2\n"})
        _commit(
            tmp_path,
            "chore: tidy a comment\n\nChangelog: none (comment only)\n",
            {"docs/backlog.md": "docs, tidied\n"},
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "change engine behavior" in result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_deleting_the_changelog_does_not_record_the_change(self, tmp_path: Path) -> None:
        # A commit that changes the engine and deletes CHANGELOG.md must still fail. A
        # deleted CHANGELOG.md is still a touched path, so a check that only asked whether
        # the file was touched would accept this commit.
        _init_repo(tmp_path)
        (tmp_path / "CHANGELOG.md").unlink()
        _commit(tmp_path, "fix: change engine behavior\n", {"src/engine.py": "x = 2\n"})
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "CHANGELOG.md" in result.stdout

    def test_a_whitespace_only_changelog_edit_does_not_record_the_change(
        self, tmp_path: Path
    ) -> None:
        # Red proof: a commit that changes the engine and only appends blank lines to
        # CHANGELOG.md must still fail - a non-empty diff is not the same as a record.
        _init_repo(tmp_path)
        original = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {"src/engine.py": "x = 2\n", "CHANGELOG.md": original + "\n\n"},
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "CHANGELOG.md" in result.stdout

    def test_a_trailing_space_on_an_existing_changelog_line_is_not_a_record(
        self, tmp_path: Path
    ) -> None:
        # Red proof: adding a trailing space to an *existing* CHANGELOG line produces an
        # added line that is non-empty after strip(), so stripping the added blob is not
        # enough to tell churn from content. Nothing was recorded; the gate must still
        # say no.
        _init_repo(tmp_path)
        original = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {
                "src/engine.py": "x = 2\n",
                "CHANGELOG.md": original.replace("- an old line", "- an old line "),
            },
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_reindenting_an_existing_changelog_line_is_not_a_record(self, tmp_path: Path) -> None:
        # The same defect from the other side: leading whitespace also survives strip().
        _init_repo(tmp_path)
        original = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {
                "src/engine.py": "x = 2\n",
                "CHANGELOG.md": original.replace("- an old line", "  - an old line"),
            },
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_converting_the_changelog_to_crlf_is_not_a_record(self, tmp_path: Path) -> None:
        # A line-ending conversion re-adds every line of the file and records nothing.
        _init_repo(tmp_path)
        original = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {"src/engine.py": "x = 2\n", "CHANGELOG.md": original.replace("\n", "\r\n")},
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_reordering_existing_changelog_lines_is_not_a_record(self, tmp_path: Path) -> None:
        # Two existing bullets swapped: the diff adds lines, but every one of them it
        # also removes. Nothing a changelog reader could not already read is there.
        _init_repo(tmp_path)
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {
                "src/engine.py": "x = 2\n",
                "CHANGELOG.md": (
                    "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n"
                    "- another old line\n- an old line\n"
                ),
            },
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_a_new_bullet_among_whitespace_churn_is_a_record(self, tmp_path: Path) -> None:
        # The positive counterpart, so the check cannot pass by refusing everything: one
        # genuinely new bullet is a record even when the same commit reflows the lines
        # around it.
        _init_repo(tmp_path)
        _commit(
            tmp_path,
            "fix: change engine behavior\n",
            {
                "src/engine.py": "x = 2\n",
                "CHANGELOG.md": (
                    "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n"
                    "  - an old line\n- another old line \n- a genuinely new bullet\n\n"
                ),
            },
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout


class TestEndToEndMerges:
    def test_a_merge_resolution_that_touches_the_engine_needs_recording(
        self, tmp_path: Path
    ) -> None:
        # Red proof: a merge whose conflict resolution edits the engine, with neither a
        # changelog addition nor a trailer on the merge commit itself, must fail - the
        # merge is not exempt just because it is a merge. Both sides record their own
        # change with a trailer, so the only unrecorded change left is the merge's own
        # resolution.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "side"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: side engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2  # side\n"},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: main engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2  # main\n"},
        )
        subprocess.run(  # noqa: S603 - a real conflict; do not check=True
            ["git", "merge", "--no-ff", "side"], cwd=tmp_path, capture_output=True, text=True
        )
        # Resolve by hand with content matching neither parent verbatim - the merge's
        # own contribution, the thing the gate charges to the merge commit.
        (tmp_path / "src" / "engine.py").write_text("x = 2  # merged\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(["commit", "-q", "-m", "Merge branch side"], cwd=tmp_path)
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "Merge branch side" in result.stdout
        assert "CHANGELOG.md" in result.stdout

    def test_a_clean_merge_passes(self, tmp_path: Path) -> None:
        # A merge with no conflict-resolution content of its own (each side's own change
        # already recorded on its own commit) must not be charged a second time.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "side"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: side engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2\n"},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(tmp_path, "docs: unrelated tidy\n", {"docs/backlog.md": "docs, tidied\n"})
        _git(["merge", "--no-ff", "-m", "Merge branch side", "side"], cwd=tmp_path)
        result = _run_gate(tmp_path)
        assert result.returncode == 0

    def test_a_clean_automerge_of_the_same_file_is_not_charged(self, tmp_path: Path) -> None:
        # Red proof: two branches edit the same engine file in *different hunks*, git
        # merges them with no conflict, and the merge contributes nothing a reader could
        # review - so the merge must not be charged. `git diff-tree --cc --name-only`
        # charges it anyway: in `--name-only` mode `--cc` is `-c`, which lists every
        # file whose merge result differs from all parents, and a clean auto-merge of
        # two hunks differs from both.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "side"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: side engine tweak\n\nChangelog: none (internal only)\n",
            {"src/wide.py": _wide(top="side")},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: main engine tweak\n\nChangelog: none (internal only)\n",
            {"src/wide.py": _wide(bottom="main")},
        )
        _git(["merge", "--no-ff", "-m", "Merge branch side", "side"], cwd=tmp_path)
        assert (tmp_path / "src" / "wide.py").read_text(encoding="utf-8") == _wide(
            top="side", bottom="main"
        ), "the fixture must be a clean auto-merge, not a resolution"
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_a_github_merge_ref_over_a_moved_base_is_not_charged(self, tmp_path: Path) -> None:
        # Red proof for the shape CI actually runs on. actions/checkout on a
        # pull_request event checks out refs/pull/N/merge - a synthetic clean merge of
        # the pull request's head into the base branch - so HEAD is a merge nobody
        # authored. Charging it with --cc fails a legitimate pull request the moment the
        # base branch edits the same engine file, and the author has no remedy: no
        # trailer can be put on a commit GitHub generates.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "pr"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: the engine change this pull request carries\n\nChangelog: none (internal only)\n",
            {"src/wide.py": _wide(top="pr")},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(tmp_path, "fix: the base branch moved\n", {"src/wide.py": _wide(bottom="base")})
        _git(["branch", "-f", "base", "main"], cwd=tmp_path)
        # The merge ref's shape: base first, pull-request head second, message not the
        # author's.
        _git(["checkout", "-q", "--detach", "main"], cwd=tmp_path)
        _git(["merge", "--no-ff", "-m", "Merge pr into main", "pr"], cwd=tmp_path)
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_a_merge_resolution_outside_the_engine_is_not_charged(self, tmp_path: Path) -> None:
        # A conflict resolution is charged for what it resolves: a docs-only resolution
        # carries no recording obligation, exactly as a docs-only commit does.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "side"], cwd=tmp_path)
        _commit(tmp_path, "docs: side wording\n", {"docs/backlog.md": "docs, from the side\n"})
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(tmp_path, "docs: main wording\n", {"docs/backlog.md": "docs, from main\n"})
        subprocess.run(  # noqa: S603 - a real conflict on the docs file; do not check=True
            ["git", "merge", "--no-ff", "side"], cwd=tmp_path, capture_output=True, text=True
        )
        (tmp_path / "docs" / "backlog.md").write_text("docs, merged\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(["commit", "-q", "-m", "Merge branch side"], cwd=tmp_path)
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_a_merge_resolution_takes_the_trailer_like_any_commit(self, tmp_path: Path) -> None:
        # The remedy for a charged merge is the same one every other commit has: the
        # resolution below edits src/, and the trailer on the merge commit declares
        # it.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "side"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: side engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2  # side\n"},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: main engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2  # main\n"},
        )
        subprocess.run(  # noqa: S603 - a real conflict; do not check=True
            ["git", "merge", "--no-ff", "side"], cwd=tmp_path, capture_output=True, text=True
        )
        (tmp_path / "src" / "engine.py").write_text("x = 2  # merged\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(
            ["commit", "-q", "-m", "Merge branch side\n\nChangelog: none (resolution only)\n"],
            cwd=tmp_path,
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_an_octopus_merge_keeps_the_combined_diff(self, tmp_path: Path) -> None:
        # Three parents are out of git merge-tree --write-tree's two-parent scope, so an
        # octopus keeps the older --cc charge. This fixture separates the two: --cc
        # lists nothing (no file differs from *every* parent), while charging the first
        # two parents as if they were the whole merge would charge the third branch's
        # engine file to the octopus commit.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "-b", "b"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: branch b engine tweak\n\nChangelog: none (internal only)\n",
            {"src/engine.py": "x = 2\n"},
        )
        _git(["checkout", "-q", "-b", "c", "main"], cwd=tmp_path)
        _commit(
            tmp_path,
            "fix: branch c engine tweak\n\nChangelog: none (internal only)\n",
            {"src/other.py": "y = 1\n"},
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _octopus(tmp_path, "Merge branches b and c", "b", "c")
        assert len(_git(["log", "-1", "--format=%P"], cwd=tmp_path).split()) == 3
        result = _run_gate(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_a_clean_octopus_over_one_file_is_over_charged(self, tmp_path: Path) -> None:
        # Characterization, not a red proof: this documents a known over-charge rather
        # than fixing one. Two branches edit the same engine file in different hunks and
        # git merges all three heads at once with no conflict - the octopus tree is
        # byte-identical to the automatic merge of its parents, so nobody authored the
        # result. An octopus is outside git merge-tree --write-tree's two-parent scope,
        # so it keeps the combined diff and is charged anyway. Left that way
        # deliberately: a wrong charge has a remedy the author can apply, a missed one
        # does not, and an octopus merge is rare.
        _init_repo(tmp_path)
        for name, kwargs in (("b", {"top": "b"}), ("c", {"bottom": "c"})):
            _git(["checkout", "-q", "-b", name, "main"], cwd=tmp_path)
            _commit(
                tmp_path,
                f"fix: {name} engine tweak\n\nChangelog: none (internal only)\n",
                {"src/wide.py": _wide(**kwargs)},
            )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _octopus(tmp_path, "Merge branches b and c", "b", "c")
        assert len(_git(["log", "-1", "--format=%P"], cwd=tmp_path).split()) == 3
        assert (tmp_path / "src" / "wide.py").read_text(encoding="utf-8") == _wide(
            top="b", bottom="c"
        ), "the fixture must be a clean automatic octopus, not a resolution"
        result = _run_gate(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "Merge branches b and c" in result.stdout, result.stdout


class TestEndToEndUnrelatedHistories:
    def test_an_unrelated_histories_merge_is_judged_not_crashed(self, tmp_path: Path) -> None:
        # Red proof for the crash. A second root merged in with
        # --allow-unrelated-histories makes git merge-tree --write-tree exit 128 rather
        # than write a tree, and an unguarded gate would die on an uncaught
        # RuntimeError - no ::error::, no verdict, and advice to upgrade a git that was
        # already new enough. It must judge: the engine change rides in on the merge
        # with nothing recorded, so exit 1 and say which commit.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "--orphan", "stranger"], cwd=tmp_path)
        _git(["rm", "-rq", "--cached", "."], cwd=tmp_path)
        for rel in (
            "src/engine.py",
            "src/wide.py",
            "docs/backlog.md",
            "CHANGELOG.md",
            "pyproject.toml",
        ):
            (tmp_path / rel).unlink()
        (tmp_path / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(["commit", "-q", "-m", "feat: a root commit changing the engine"], cwd=tmp_path)
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _git(
            [
                "merge",
                "--no-ff",
                "--allow-unrelated-histories",
                "-m",
                "Merge the stranger",
                "stranger",
            ],
            cwd=tmp_path,
        )

        result = _run_gate(tmp_path)

        assert "Traceback" not in result.stderr, result.stderr
        assert result.returncode == 1, result.stdout
        assert "::error::" in result.stdout, result.stdout
        assert "src/other.py" in result.stdout, result.stdout

    def test_an_unrelated_histories_merge_takes_the_trailer(self, tmp_path: Path) -> None:
        # The remedy has to exist, or the previous test only moved the dead end. The
        # combined diff charges the merge, and a trailer on a commit inside it clears
        # the charge like any other.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "--orphan", "stranger"], cwd=tmp_path)
        _git(["rm", "-rq", "--cached", "."], cwd=tmp_path)
        for rel in (
            "src/engine.py",
            "src/wide.py",
            "docs/backlog.md",
            "CHANGELOG.md",
            "pyproject.toml",
        ):
            (tmp_path / rel).unlink()
        (tmp_path / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(
            [
                "commit",
                "-q",
                "-m",
                "feat: a root commit changing the engine\n\nChangelog: none (vendored tree)\n",
            ],
            cwd=tmp_path,
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _git(
            [
                "merge",
                "--no-ff",
                "--allow-unrelated-histories",
                "-m",
                "Merge the stranger\n\nChangelog: none (vendored tree)\n",
                "stranger",
            ],
            cwd=tmp_path,
        )

        result = _run_gate(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_an_unrelated_histories_resolution_is_charged_to_the_merge(
        self, tmp_path: Path
    ) -> None:
        # The two tests above assert that this range is judged rather than crashed, and
        # both survive a mutant that charges the merge nothing at all - the error they
        # see comes from the root commit's own diff. This one pins the merge's charge:
        # the root carries the trailer, so only the merge can fail, and its resolution
        # is a hand edit of an engine file that neither root contains.
        _init_repo(tmp_path)
        _git(["checkout", "-q", "--orphan", "stranger"], cwd=tmp_path)
        _git(["rm", "-rq", "--cached", "."], cwd=tmp_path)
        for rel in (
            "src/engine.py",
            "src/wide.py",
            "docs/backlog.md",
            "CHANGELOG.md",
            "pyproject.toml",
        ):
            (tmp_path / rel).unlink()
        (tmp_path / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(
            [
                "commit",
                "-q",
                "-m",
                "feat: a root commit changing the engine\n\nChangelog: none (vendored tree)\n",
            ],
            cwd=tmp_path,
        )
        _git(["checkout", "-q", "main"], cwd=tmp_path)
        _git(
            ["merge", "--no-ff", "--no-commit", "--allow-unrelated-histories", "stranger"],
            cwd=tmp_path,
        )
        (tmp_path / "src" / "other.py").write_text("y = 99\n", encoding="utf-8")
        _git(["add", "-A"], cwd=tmp_path)
        _git(["commit", "-q", "-m", "Merge the stranger"], cwd=tmp_path)

        result = _run_gate(tmp_path)

        assert result.returncode == 1, result.stdout
        assert "Merge the stranger" in result.stdout, result.stdout
