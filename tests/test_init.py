"""`radar init` makes a data root; `radar add` writes the first entry into one.

WHAT IS PROVED. An engine that can read a catalogue and judge it but cannot produce one
leaves every data root to be made by hand, shaped by whichever catalogue it was copied
from. So the first assertion is not a missing field somewhere - it is that `radar init`
and `radar add` are verbs - and the rest check what they write.

WHY THE WHOLE SEQUENCE IS ONE TEST rather than four. What `init` has to deliver is a
PATH - init, add, gate, render, in a directory that has never held a catalogue - and each
step's output is the next step's input. Asserting the four separately would pass on a tree
where `init` writes a marker the gate then refuses, which is exactly the failure the
path exists to catch: the engine's own fixtures (tests/conftest.py) hand-write a
minimal marker, so nothing else here ever reads a root that `init` produced.

THE SYNTHETIC `claude_home` is what keeps that path hermetic. The generated profile carries
DISCOVERED paths, never templated ones, so on a developer machine it points at
the real `~/.claude` and the gate would scan it. `--claude-home` overrides every profile's,
which is the flag the gate already has for exactly this reason, and it is pointed at an
empty directory: the invariant scan then reports that it had no data plane to read instead
of reading the operator's.

THE SYNTHETIC HOME does the same for discovery. `radar init` and `radar bootstrap` look
for directories under the home directory, so every subprocess here runs with HOME and
USERPROFILE pointed at an empty tree under the test's temporary directory, and with the
OneDrive variables removed: nothing any test runs looks at the operator's own home.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from engine_run import run_engine
from stack_radar import cli, radar_lib

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))

MARKER = "radar.toml"

# The tree `init` produces. Read as a list rather than checked one assertion at a time so
# a partial implementation reports everything it did not write: the failure that matters
# is "the stranger's tree is incomplete", and finding that out one missing file per run is
# the slow way to learn it.
REQUIRED_ARTEFACTS = (
    "radar.toml",
    "environments/default.toml",
    "tools",
    ".gitignore",
    ".gitattributes",
    ".githooks",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    ".github/dependabot.yml",
    "CHANGELOG.md",
    "pyproject.toml",
)

# The keys a generated marker carries. `[publish]`, `[changelog]` and `[render]` are
# tables; the rest are scalars.
MARKER_SCALARS = ("schema", "title", "default_environment", "requires_framework")
MARKER_TABLES = ("invariant", "publish", "changelog", "render")


def child_env(**extra: str) -> dict[str, str]:
    """The parent environment with the root override removed.

    `RADAR_DATA_ROOT` is read by the resolver BEFORE it searches, so a machine that exports
    it would point `radar add` at a real catalogue and this whole module would write into
    the operator's tree instead of into tmp_path.
    """
    env = {**os.environ, **extra}
    env.pop("RADAR_DATA_ROOT", None)
    return env


def radar(
    *args: str, cwd: Path, timeout: int = 180, real_process: bool = False
) -> subprocess.CompletedProcess:
    """`radar <verb> ...`, reached through the launcher.

    Through `cli` and not the module directly, because half of what `init` adds IS the
    verb: an `init.py` that works when imported and is not dispatched to is a command
    nobody can run, and a test that imported the module would not notice. `run_engine`
    decides whether that is a subprocess (tests/engine_run.py); `real_process` always
    starts one, for the end-to-end path.
    """
    env = child_env(PYTHONPATH=str(SRC))
    if not real_process:
        return run_engine("cli", args, cwd=cwd, env=env, timeout=timeout)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "stack_radar.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def outside(tmp_path: Path) -> Path:
    """A directory guaranteed to be no data root, with none above it.

    Guarded rather than assumed: if the temporary directory ever sat under a real
    catalogue, every "no marker" assertion here would be about something else.
    """
    assert not any((p / MARKER).is_file() for p in (tmp_path, *tmp_path.parents)), (
        f"a {MARKER} above {tmp_path} makes these proofs vacuous"
    )
    return tmp_path


# The variables through which the engine finds a home directory or a synced documents
# folder. Each is replaced or removed for every test in this module.
HOME_VARIABLES = ("HOME", "USERPROFILE")
ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")


@pytest.fixture(autouse=True)
def no_real_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Point every child process's home at an empty synthetic one."""
    home = tmp_path_factory.mktemp("empty-home")
    for name in HOME_VARIABLES:
        monkeypatch.setenv(name, str(home))
    for name in ONEDRIVE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    """An initialised data root in a directory that has never held one."""
    target = outside(tmp_path) / "a-fresh-radar"
    done = radar("init", str(target), cwd=tmp_path)
    assert done.returncode == 0, f"radar init failed:\n{done.stdout}{done.stderr}"
    return target


@pytest.fixture
def empty_claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "synthetic-claude-home"
    home.mkdir()
    return home


# A public entry that the gate has no finding about: `observe` needs no telemetry matcher
# and no pilot-exit block, and a URL is what `visibility = "public"` requires. The
# tool is a real published one so the entry is not a fiction, and it is one the engine's
# own documentation already uses as its neutral example.
PUBLIC_TOOL = (
    "add",
    "just",
    "--repo",
    "https://github.com/casey/just",
    "--axis",
    "lint-format",
    "--ring",
    "observe",
    "--license",
    "CC0-1.0",
    "--visibility",
    "public",
    "--artifact",
    "cli",
    "--note",
    "Command runner; a candidate for the task lane",
    "--reason",
    "Entered the catalogue on the day it was first looked at",
)


class TestTheVerbsExist:
    """The launcher dispatches them. This is the assertion that is red first."""

    def test_init_and_add_are_verbs(self):
        assert {"init", "add"} <= set(cli.VERBS), sorted(cli.VERBS)

    def test_each_new_verb_has_a_summary_line(self):
        # The list of verbs is printed from SUMMARY; a verb with no line raises a KeyError
        # inside `usage()`, which turns `radar --help` into a traceback.
        assert {"init", "add"} <= set(cli.SUMMARY), sorted(cli.SUMMARY)

    def test_usage_names_init_as_the_verb_that_creates_a_root(self):
        # Every other verb finds a root or judges a directory. `init` makes one, and a help
        # text that lumps it in with the finders tells the reader to stand somewhere they
        # cannot stand yet.
        assert "init" in cli.usage()


class TestTheTreeInitWrites:
    def test_every_required_artefact_is_written(self, fresh: Path):
        missing = [rel for rel in REQUIRED_ARTEFACTS if not (fresh / rel).exists()]
        assert not missing, f"radar init wrote no {', '.join(missing)}"

    def test_the_hooks_directory_carries_both_hooks(self, fresh: Path):
        # `.githooks/` is only armed as a whole: `git config core.hooksPath` points at the
        # directory, so a missing commit-msg hook is a stage that silently never runs.
        for hook in ("pre-commit", "commit-msg"):
            assert (fresh / ".githooks" / hook).is_file(), hook

    def test_every_uv_run_the_lane_makes_is_frozen(self, fresh: Path):
        # Without --frozen, `uv run` resolves a pyproject.toml that uv.lock no longer
        # matches and installs the result, so a pull request that changes only the
        # catalogue's pyproject.toml would run new code at the next commit.
        from test_commit_lane import unfrozen_uv_runs  # noqa: PLC0415 - shared helper

        hooks = (".githooks/pre-commit", ".githooks/commit-msg")
        for hook in hooks:
            assert "uv run --frozen" in (fresh / hook).read_text(encoding="utf-8"), hook
        found = unfrozen_uv_runs(fresh, (*hooks, ".pre-commit-config.yaml"))
        assert not found, "\n".join(found)

    def test_the_marker_carries_the_required_keys_and_tables(self, fresh: Path):
        marker = tomllib.loads((fresh / MARKER).read_text(encoding="utf-8"))
        assert [k for k in MARKER_SCALARS if not marker.get(k)] == []
        assert [k for k in MARKER_TABLES if not isinstance(marker.get(k), dict)] == []
        assert marker["schema"] == "radar-data/v1"

    def test_the_needle_is_the_data_root_s_directory_name(self, fresh: Path):
        marker = tomllib.loads((fresh / MARKER).read_text(encoding="utf-8"))
        assert marker["invariant"]["needles"] == [fresh.name]

    def test_the_needle_carries_the_reason_a_generic_name_is_weak(self, fresh: Path):
        """The needle is seeded with a comment, not just the key, and the comment says why.

        Without the key the scan runs on the absolute path alone and still prints `holds`,
        so the weakness is invisible in the output. A seeded needle nobody understands gets
        deleted the first time it is inconvenient; the comment is what argues against that.
        """
        text = (fresh / MARKER).read_text(encoding="utf-8")
        comments = "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith("#"))
        assert "holds" in comments, comments
        assert "absolute path" in comments.lower(), comments

    def test_the_boundary_note_is_seeded_with_the_engine_s_generic_default(self, fresh: Path):
        # render.py keeps this string as the fallback for a catalogue that declares none,
        # and says `radar init` seeds new catalogues with it. Two copies of one sentence
        # drift, so the seed has to BE that constant rather than resemble it.
        from stack_radar.render import DEFAULT_BOUNDARY_NOTE

        marker = tomllib.loads((fresh / MARKER).read_text(encoding="utf-8"))
        assert marker["render"]["boundary_note"] == DEFAULT_BOUNDARY_NOTE

    def test_the_marker_names_no_adr(self, fresh: Path):
        # A generated catalogue is a stranger's, and cites no decision record: the records
        # a catalogue keeps are its own, and a template cannot know them. The boundary
        # note's default is under the same rule. The prefix is spelled in two pieces so
        # that this file does not carry the form tests/test_self_contained.py forbids
        # anywhere in the tracked tree.
        assert "AD" + "R-" not in (fresh / MARKER).read_text(encoding="utf-8")


def engine_version() -> str:
    """The version of the engine the subprocesses run, as that engine reports it."""
    done = radar("--version", cwd=REPO)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


# The source a generated workflow installs the engine from. Spelled out here rather than
# imported, because it is the specification: the test has to be able to disagree with the
# constant it checks.
ENGINE_GIT_SOURCE = "git+https://github.com/grimaldost/stack-radar"


class TestTheGeneratedWorkflowInstallsTheEngineFromItsTag:
    """Every engine step in the generated CI names the tagged git source, never a bare name.

    A package name is resolved against whatever index the runner reaches, so the first
    party to publish under it decides what every catalogue's CI executes. A git URL pinned
    to the release tag names one repository and one version.
    """

    def test_each_engine_step_uses_the_tagged_git_source(self, fresh: Path):
        ci = (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        spec = f'uvx --from "{ENGINE_GIT_SOURCE}@v{engine_version()}" radar'
        for verb in ("gate", "check-version-sites", 'changelog-gate "origin/$BASE_REF"'):
            assert f"{spec} {verb}" in ci, (verb, ci)

    def test_no_step_resolves_the_engine_by_package_name(self, fresh: Path):
        ci = (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--from stack-radar" not in ci, ci
        assert "stack-radar==" not in ci, ci
        # Every `uvx` that runs the engine carries the git source; zizmor is the one
        # other `uvx` in the file and is pinned by version on purpose.
        engine_calls = [ln for ln in ci.splitlines() if "uvx" in ln and " radar " in ln]
        assert engine_calls, ci
        assert all(ENGINE_GIT_SOURCE in ln for ln in engine_calls), engine_calls

    def test_the_changelog_gate_takes_its_base_ref_from_the_environment(self, fresh: Path):
        ci = (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "BASE_REF: ${{ github.base_ref }}" in ci, ci
        assert '"origin/${{' not in ci, ci


SHA = "0123456789abcdef0123456789abcdef01234567"


def init_installed_as(tmp_path: Path, direct_url: dict | None) -> str:
    """The ci.yml `radar init` writes when the running engine's installation record says it
    came from `direct_url` (None: no record, as for an install from a wheel).

    The record is a synthetic dist-info directory put ahead of the real installation on
    the import path, which is where `importlib.metadata` finds a distribution first.
    """
    site = tmp_path / "site"
    dist = site / f"stack_radar-{engine_version()}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: stack-radar\nVersion: {engine_version()}\n",
        encoding="utf-8",
    )
    if direct_url is not None:
        (dist / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
    target = outside(tmp_path) / "pinned-radar"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "stack_radar.cli", "init", str(target)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        env=child_env(PYTHONPATH=os.pathsep.join([str(site), str(SRC)])),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return (target / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


class TestTheGeneratedWorkflowPinsTheEngineToACommitWhenItKnowsOne:
    """A tag can be moved to other code; a commit cannot. An engine installed from its own
    git source knows the commit its tag resolved to, and pins the generated CI to it."""

    def test_an_engine_installed_from_git_pins_its_commit(self, tmp_path: Path):
        ci = init_installed_as(
            tmp_path,
            {
                "url": "https://github.com/grimaldost/stack-radar.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": SHA,
                    "requested_revision": f"v{engine_version()}",
                },
            },
        )
        spec = f'uvx --from "{ENGINE_GIT_SOURCE}@{SHA}" radar'
        for verb in ("gate", "check-version-sites", 'changelog-gate "origin/$BASE_REF"'):
            assert f"{spec} {verb} # v{engine_version()}" in ci, (verb, ci)
        assert f"@v{engine_version()}" not in ci, ci
        prose = re.sub(r"\n#\s+", " ", ci)
        assert f"at the commit of v{engine_version()}" in prose, ci

    def test_a_commit_not_requested_as_the_release_tag_is_not_called_the_release(
        self, tmp_path: Path
    ):
        # Installed from a branch: the commit is pinned, but the engine does not know
        # whether it is the tag's, so neither the header nor a step says either way.
        ci = init_installed_as(
            tmp_path,
            {
                "url": "https://github.com/grimaldost/stack-radar",
                "vcs_info": {"vcs": "git", "commit_id": SHA, "requested_revision": "main"},
            },
        )
        assert f'{ENGINE_GIT_SOURCE}@{SHA}" radar gate\n' in ci, ci
        assert f"# v{engine_version()}" not in ci, ci
        prose = re.sub(r"\n#\s+", " ", ci)
        assert f"commit of v{engine_version()}" not in prose, ci
        assert "is not the commit of the release tag" not in prose, ci
        assert f"at the commit {SHA} the engine that wrote this file was installed from" in (
            prose
        ), ci
        assert f"did not ask for the release tag v{engine_version()}" in prose, ci
        assert "so this commit may differ from the release's" in prose, ci

    def test_an_engine_without_an_install_record_keeps_the_tag_and_says_how_to_pin(
        self, tmp_path: Path
    ):
        ci = init_installed_as(tmp_path, None)
        assert f'{ENGINE_GIT_SOURCE}@v{engine_version()}" radar gate' in ci, ci
        prose = re.sub(r"\n#\s+", " ", ci)  # the header comment, unwrapped
        pin = f"git ls-remote https://github.com/grimaldost/stack-radar v{engine_version()}"
        assert pin in prose, ci

    def test_a_commit_from_another_repository_is_not_named(self, tmp_path: Path):
        # A fork's commit may not exist in this repository, so the tag stays.
        ci = init_installed_as(
            tmp_path,
            {
                "url": "https://github.com/someone-else/stack-radar",
                "vcs_info": {"vcs": "git", "commit_id": SHA},
            },
        )
        assert SHA not in ci, ci
        assert f'{ENGINE_GIT_SOURCE}@v{engine_version()}" radar gate' in ci, ci

    def test_a_git_install_from_elsewhere_is_not_called_a_non_git_install(self, tmp_path: Path):
        # A local clone installed with git at the release tag: the tag stays, and the
        # header says what the engine does not know, not that git was not used.
        ci = init_installed_as(
            tmp_path,
            {
                "url": "file:///C:/clones/stack-radar",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": SHA,
                    "requested_revision": f"v{engine_version()}",
                },
            },
        )
        prose = re.sub(r"\n#\s+", " ", ci)
        assert "was not installed from git" not in prose, ci
        assert (
            "was not installed with git from https://github.com/grimaldost/stack-radar" in prose
        ), ci

    def test_an_ssh_install_of_this_repository_pins_its_commit(self, tmp_path: Path):
        ci = init_installed_as(
            tmp_path,
            {
                "url": "ssh://git@github.com/grimaldost/stack-radar.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": SHA,
                    "requested_revision": f"v{engine_version()}",
                },
            },
        )
        assert f'{ENGINE_GIT_SOURCE}@{SHA}" radar gate # v{engine_version()}' in ci, ci


_USES = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)\s*#\s*(\S+)")


def action_pins(text: str) -> dict[str, tuple[str, str]]:
    """`owner/action -> (ref, version comment)` for every `uses:` line in a workflow."""
    return {m.group(1): (m.group(2), m.group(3)) for m in _USES.finditer(text)}


class TestThirdPartyCodeIsPinnedToACommit:
    """What a catalogue runs from other repositories is pinned to a commit, not a tag.

    A tag can be moved to different code by anyone with push rights on the repository that
    owns it; a commit id cannot. The hook runs on every commit and the actions on every
    push, so a moved tag would change what runs without any change in the catalogue.
    """

    def test_the_commit_message_hook_is_pinned_to_a_commit(self, fresh: Path):
        config = (fresh / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        found = re.search(
            r"repo: https://github.com/compilerla/conventional-pre-commit\s*\n\s*rev: (\S+)(.*)",
            config,
        )
        assert found, config
        rev, rest = found.group(1), found.group(2)
        assert re.fullmatch(r"[0-9a-f]{40}", rev), f"rev {rev!r} is not a commit id"
        assert "# frozen: v4.0.0" in rest, rest

    def test_the_actions_match_the_engine_s_own_pins(self, fresh: Path):
        # One set of pins, reviewed once: the engine's own CI is where an action update is
        # tried first, and a catalogue created afterwards should not start behind it.
        ours = action_pins((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        theirs = action_pins(
            (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        for action in ("actions/checkout", "astral-sh/setup-uv"):
            assert action in theirs, (action, theirs)
            assert re.fullmatch(r"[0-9a-f]{40}", theirs[action][0]), theirs[action]
            assert theirs[action] == ours[action], (action, theirs[action], ours[action])

    def test_every_setup_uv_step_pins_the_engine_s_uv_version(self, fresh: Path):
        # Without a `version:` input setup-uv installs whatever uv is current at run time.
        # The commented-out job counts too: uncommenting it must not bring in an unpinned uv.
        pin = re.compile(r"^[ \t]*(?:#[ \t]*)?version: '(\d[\w.]*)'$", re.M)
        ours = set(pin.findall((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8")))
        text = (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        steps = text.count("astral-sh/setup-uv@")
        pins = pin.findall(text)
        assert steps and len(pins) == steps, f"{len(pins)} uv pins for {steps} setup-uv step(s)"
        assert len(ours) == 1 and set(pins) == ours, (pins, ours)

    def test_init_writes_a_dependabot_config_for_the_actions(self, fresh: Path):
        # A commit pin never moves on its own, so without something proposing updates the
        # catalogue keeps the versions it was created with, security fixes included.
        config = fresh / ".github" / "dependabot.yml"
        assert config.is_file(), "radar init wrote no .github/dependabot.yml"
        text = config.read_text(encoding="utf-8")
        assert re.search(r"^version: 2$", text, re.M), text
        assert 'package-ecosystem: "github-actions"' in text, text
        # The catalogue commits a uv.lock (ruff, pre-commit), which stays on the versions
        # it was created with unless something proposes updates.
        assert 'package-ecosystem: "uv"' in text, text
        # A release is held back before it is proposed, so a compromised one that is
        # pulled within days never reaches the pin.
        assert re.search(r"cooldown:\n\s+default-days: [1-9]", text), text

    def test_the_audit_covers_the_dependabot_config_too(self, fresh: Path):
        # zizmor reads dependabot.yml as well as the workflows, but only when it is given
        # the repository root: a scan of .github/workflows never sees the file that decides
        # how the pins above move.
        ci = (fresh / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        scans = [ln.strip() for ln in ci.splitlines() if re.search(r"uvx zizmor==\S+", ln)]
        assert scans, ci
        assert all(ln.endswith(" .") for ln in scans), scans


def comments(text: str) -> str:
    """The comment lines of a generated file, joined, with the comment signs dropped."""
    return " ".join(
        ln.lstrip()[1:].strip() for ln in text.splitlines() if ln.lstrip().startswith("#")
    )


class TestTheGeneratedFilesDescribeTheRules:
    """What the generated files tell their reader matches what the engine enforces."""

    def test_the_marker_shows_how_to_declare_extra_claude_home_paths(self, fresh: Path):
        text = (fresh / MARKER).read_text(encoding="utf-8")
        assert re.search(r"^# claude_home_paths = \[", text, re.M), text
        # Commented, so a new catalogue declares no extra paths until its operator does.
        assert "claude_home_paths" not in tomllib.loads(text)["invariant"]

    def test_the_marker_says_the_publication_check_is_off_until_declared(self, fresh: Path):
        text = (fresh / MARKER).read_text(encoding="utf-8")
        publish = comments(text.split("[invariant.exempt]")[-1].split("[publish]")[0])
        assert "OFF until `framework_worktree` is declared" in publish, publish
        assert "refuses to count as a pass" not in publish, publish
        assert tomllib.loads(text)["publish"] == {}

    def test_the_gitignore_names_overlays_by_their_pattern(self, fresh: Path):
        text = (fresh / ".gitignore").read_text(encoding="utf-8")
        assert "one `<env>.local.toml` per environment" in text, text

    def test_the_tools_readme_states_the_history_rules(self, fresh: Path):
        text = " ".join((fresh / "tools" / "README.md").read_text(encoding="utf-8").split())
        assert "carries a `date` (`YYYY-MM-DD`) and a `reason`" in text, text
        assert "`adopt` or `pilot` also carries `evidence`" in text, text
        assert "--evidence" in text, text


def step_lines(stdout: str) -> list[str]:
    """The lines of the `next:` list, as printed: from the heading to the first blank line."""
    tail = stdout.split("\nnext:\n", 1)[1]
    return tail.split("\n\n", 1)[0].splitlines()


def printed_steps(stdout: str) -> list[str]:
    """The commands `radar init` prints under `next:`, exactly as a reader would copy them."""
    return [ln.strip()[2:] for ln in step_lines(stdout) if ln.strip().startswith("- ")]


class TestThePrintedStepsLeadToAPassingWorkflow:
    """The steps `radar init` prints put a lock in the first commit.

    The generated workflow's first step is `uv lock --check`, which fails on a tree with no
    `uv.lock`. A lock created after the first commit is a file the first push does not
    carry, so the stranger's first CI run is red on a step they were told to run.
    """

    def test_uv_lock_comes_before_the_first_commit(self, tmp_path: Path):
        target = outside(tmp_path) / "ordered-radar"
        done = radar("init", str(target), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        steps = printed_steps(done.stdout)
        assert "uv lock" in steps, steps
        add = next(i for i, s in enumerate(steps) if "git add" in s)
        commit = next(i for i, s in enumerate(steps) if "git commit" in s)
        assert steps.index("uv lock") < add <= commit, steps

    def test_the_hook_mode_is_fixed_before_the_first_commit(self, tmp_path: Path):
        # A mode fixed in the index after the commit is a staged change nobody commits,
        # so the first clone on POSIX gets hooks git skips in silence.
        target = outside(tmp_path) / "mode-radar"
        done = radar("init", str(target), cwd=tmp_path)
        steps = printed_steps(done.stdout)
        chmod = next(i for i, s in enumerate(steps) if "update-index --chmod=+x" in s)
        commit = next(i for i, s in enumerate(steps) if "git commit" in s)
        assert chmod < commit, steps

    def test_each_step_is_one_line_that_every_shell_reads_alike(self, tmp_path: Path):
        # A backslash continuation is sh syntax: PowerShell and cmd read the next line as
        # a new command. An unquoted directory with a space in it is two arguments to `cd`.
        target = outside(tmp_path) / "a radar with spaces"
        done = radar("init", str(target), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        listed = step_lines(done.stdout)
        assert listed and all(ln.startswith("  - ") for ln in listed), listed
        assert not any(ln.rstrip().endswith("\\") for ln in listed), listed
        assert f'cd "{target.as_posix()}"' in printed_steps(done.stdout), listed

    def test_no_step_carries_a_trailing_comment(self, tmp_path: Path):
        # `#` starts a comment in sh and PowerShell and nowhere in cmd, which passes it and
        # every word after it to the command as arguments: `git config core.hooksPath
        # .githooks # ...` is then a git config call with too many arguments.
        done = radar("init", str(outside(tmp_path) / "plain-radar"), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        steps = printed_steps(done.stdout)
        assert steps and not any("#" in s for s in steps), steps

    def test_why_the_order_matters_is_still_printed(self, tmp_path: Path):
        # The reasons move below the list rather than disappearing: the order is the
        # content, and a reader who reorders the steps is the failure they prevent.
        done = radar("init", str(outside(tmp_path) / "reasons-radar"), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        after = done.stdout.split("\nnext:\n", 1)[1].split("\n\n", 1)[-1]
        assert "uv.lock" in after and "executable bit" in after, after

    def test_the_files_to_review_include_the_ones_uv_reads(self, tmp_path: Path):
        # The hooks run through `uv run`, which reads pyproject.toml, uv.toml and
        # .python-version as well as uv.lock: a pull request that changes one of them
        # changes what runs at the next commit.
        target = outside(tmp_path) / "review-radar"
        done = radar("init", str(target), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        after = done.stdout.split("\nnext:\n", 1)[1].split("\n\n", 1)[-1]
        header = (target / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        header = header.split("\nrepos:", 1)[0]
        for name in ("uv.lock", "pyproject.toml", "uv.toml", ".python-version"):
            assert name in after, (name, after)
            assert name in header, (name, header)


class TestTheProfileIsDiscoveredNotTemplated:
    """`environments/default.toml` with DISCOVERED paths, never templated ones.

    The distinction is the whole reason `bootstrap.py` exists: a profile that ships
    `<home>/Documents` makes the operator turn placeholders into values by hand, and
    guessing a home directory is not a judgement - it is computable, so it is computed.
    """

    def test_the_profile_is_the_default_environment_the_marker_names(self, fresh: Path):
        marker = tomllib.loads((fresh / MARKER).read_text(encoding="utf-8"))
        profile = tomllib.loads(
            (fresh / "environments" / "default.toml").read_text(encoding="utf-8")
        )
        # A marker naming an environment that does not exist is the silent failure gate.py
        # already spends a check on for tool entries: invisible everywhere, reported nowhere.
        assert profile["name"] == marker["default_environment"]

    def test_the_subprocess_discovered_the_synthetic_home(self, fresh: Path):
        # The synthetic home is empty, so a profile with any path in it was discovered
        # somewhere else - the operator's own home.
        profile = tomllib.loads(
            (fresh / "environments" / "default.toml").read_text(encoding="utf-8")
        )
        assert profile.get("paths", {}) == {}, profile

    def test_no_written_path_is_a_placeholder(self, fresh: Path):
        paths = tomllib.loads(
            (fresh / "environments" / "default.toml").read_text(encoding="utf-8")
        ).get("paths", {})
        templated = {k: v for k, v in paths.items() if "{" in str(v) or "<" in str(v)}
        assert not templated, f"templated instead of discovered: {templated}"

    def test_a_path_that_was_not_found_is_commented_out_rather_than_guessed(self, tmp_path: Path):
        """A synthetic home with nothing in it: no key may be invented.

        Run against the function rather than the CLI, because the point is the function's
        own contract: given a home that does NOT hold the things being looked for, it
        invents nothing.
        """
        from stack_radar import init

        home = tmp_path / "bare-home"
        home.mkdir()
        discovered = init.discovered_paths(home)
        assert set(discovered) >= {"documents", "claude_home"}
        assert not [k for k, v in discovered.items() if v], discovered

        target = outside(tmp_path) / "bare-radar"
        init.create(target, home=home)
        profile = tomllib.loads(
            (target / "environments" / "default.toml").read_text(encoding="utf-8")
        )
        assert profile.get("paths", {}) == {}, (
            "a path that could not be discovered was written anyway - a profile carrying a "
            "guess fails later and somewhere else"
        )

    def test_the_starter_profile_writes_the_home_as_a_tilde(self, tmp_path: Path):
        """The profile lands in a TRACKED file, so it must not spell the account out.

        Every reader expands `~` back (`radar_lib.with_home`), so the file says the same
        thing and names nobody. A sibling the home is merely a prefix of is not this
        account's and stays as written.
        """
        from stack_radar import bootstrap, init

        home = tmp_path / "someone"
        (home / "Documents").mkdir(parents=True)
        (home / ".claude").mkdir()
        target = outside(tmp_path) / "tilde-radar"
        init.create(target, home=home)
        text = (target / "environments" / "default.toml").read_text(encoding="utf-8")
        assert home.as_posix() not in text, text
        paths = tomllib.loads(text)["paths"]
        assert paths["documents"] == "~/Documents"
        assert paths["claude_home"] == "~/.claude"

        sibling = {"documents": (tmp_path / "someone-old" / "Documents").as_posix()}
        assert bootstrap.collapse_home(sibling, home) == sibling


class TestTheGeneratedRootStandsOnItsOwn:
    def test_the_version_sites_of_the_new_root_agree(self, fresh: Path):
        assert radar_lib.version_site_errors(fresh) == []

    def test_the_root_satisfies_the_framework_range_it_declares(self, fresh: Path):
        """The engine that made the root satisfies the range the root declares.

        The range `init` writes is derived from the engine's own version, and the framework
        check reads that version (`framework_version()`). Reading the new catalogue's
        `pyproject.toml` instead would agree only while a new catalogue started at the
        engine's version, which stops being true at the engine's next minor release.
        """
        marker = tomllib.loads((fresh / MARKER).read_text(encoding="utf-8"))
        errs = radar_lib.framework_errors(
            radar_lib.framework_version(fresh), marker["requires_framework"]
        )
        assert errs == [], (
            f"the engine does not satisfy the `requires_framework` of a root it just made: {errs}"
        )

    def test_the_root_is_a_root_and_its_parent_is_not(self, fresh: Path):
        """`radar gate` needs a marker above, seen from the tree `init` just made.

        The same condition tests/test_data_root.py proves against a bare temporary
        directory, asserted here about the tree `init` just made - the marker it wrote is
        what a command standing inside finds, and standing one directory up finds nothing.
        """
        inside = radar("gate", "--no-data-plane", cwd=fresh)
        assert inside.returncode != 2, (
            f"the initialised root was not recognised as one:\n{inside.stdout}{inside.stderr}"
        )
        above = radar("gate", "--no-data-plane", cwd=fresh.parent)
        output = above.stdout + above.stderr
        assert above.returncode == 2, f"expected exit 2 above the root, got:\n{output}"
        assert MARKER in output, f"the failure never names {MARKER}:\n{output}"

    def test_snapshot_writes_its_first_file_into_a_fresh_root(self, fresh: Path):
        # `radar init` creates no snapshots/, and the gate sends a new catalogue to
        # `radar snapshot`. A fresh root has no entries, so the run makes no lookups.
        done = radar("snapshot", cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        written = list((fresh / "snapshots").glob("*.json"))
        assert len(written) == 1, done.stdout + done.stderr
        assert written[0].read_text(encoding="utf-8") == "{}"

    def test_init_refuses_to_overwrite_an_existing_root(self, fresh: Path):
        before = (fresh / MARKER).read_text(encoding="utf-8")
        done = radar("init", str(fresh), cwd=fresh.parent)
        assert done.returncode != 0, f"init overwrote a catalogue:\n{done.stdout}{done.stderr}"
        assert MARKER in done.stdout + done.stderr
        assert (fresh / MARKER).read_text(encoding="utf-8") == before

    def test_init_refuses_a_directory_holding_a_file_it_would_write(self, tmp_path: Path):
        # An existing project: its own pyproject.toml and an ignore rule for a secret. The
        # printed next steps end in `git add -A`, so replacing its .gitignore would commit
        # what it was keeping out of git.
        target = outside(tmp_path) / "existing"
        target.mkdir()
        (target / "pyproject.toml").write_text('[project]\nname = "mine"\n', encoding="utf-8")
        (target / ".gitignore").write_text("secret.env\n", encoding="utf-8")
        done = radar("init", str(target), cwd=tmp_path)
        out = done.stdout + done.stderr
        assert done.returncode != 0, f"init replaced the project's files:\n{out}"
        assert (target / ".gitignore").read_text(encoding="utf-8") == "secret.env\n", out
        assert "mine" in (target / "pyproject.toml").read_text(encoding="utf-8"), out
        assert ".gitignore" in out and "pyproject.toml" in out and "--force" in out, out
        assert not (target / MARKER).exists(), "init wrote part of the tree before refusing"

        forced = radar("init", str(target), "--force", cwd=tmp_path)
        assert forced.returncode == 0, forced.stdout + forced.stderr
        assert (target / MARKER).is_file()
        assert "secret.env" not in (target / ".gitignore").read_text(encoding="utf-8")


class TestAdd:
    def test_add_writes_an_entry_with_the_first_history(self, fresh: Path):
        done = radar(*PUBLIC_TOOL, cwd=fresh)
        assert done.returncode == 0, f"radar add failed:\n{done.stdout}{done.stderr}"
        entry = tomllib.loads((fresh / "tools" / "just.toml").read_text(encoding="utf-8"))
        assert entry["name"] == "just"
        assert radar_lib.validate(entry) == []
        history = entry["history"]
        assert len(history) == 1
        assert history[0]["ring"] == entry["ring"]
        assert history[0]["date"] and history[0]["reason"]

    def test_the_printed_commands_carry_no_trailing_comment(self, fresh: Path):
        # The same reason as for `radar init`'s steps: cmd reads `#` as an argument.
        done = radar(*PUBLIC_TOOL, cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        commands = [
            ln.strip()[2:] for ln in done.stdout.splitlines() if ln.strip().startswith("- ")
        ]
        assert commands == ["radar gate", "radar render"], done.stdout

    @pytest.mark.parametrize("name", ["../../evil", ".hidden", "a/b"])
    def test_add_refuses_a_name_that_is_not_one_plain_component(self, fresh: Path, name: str):
        # The name becomes tools/<name>.toml. A traversal, a hidden file or a nested
        # directory is refused by the schema's name rule before anything is written.
        before = sorted(p.name for p in (fresh / "tools").iterdir())
        args = list(PUBLIC_TOOL)
        args[1] = name
        done = radar(*args, cwd=fresh)
        assert done.returncode != 0, done.stdout + done.stderr
        assert "bad name" in done.stdout + done.stderr, done.stdout + done.stderr
        assert sorted(p.name for p in (fresh / "tools").iterdir()) == before
        assert not list(fresh.parent.glob("*.toml")), "a file landed outside tools/"

    def test_add_keeps_each_refusal_on_one_line(self, fresh: Path):
        # The name is quoted as written, so a control character in it cannot split the
        # [FAIL] line and leave a fragment that reads as a separate message.
        args = list(PUBLIC_TOOL)
        args[1] = "ok" + chr(10)
        done = radar(*args, cwd=fresh)
        assert done.returncode != 0, done.stdout + done.stderr
        fails = [ln for ln in done.stdout.splitlines() if "bad name" in ln]
        assert fails and all(ln.startswith("[FAIL] 'ok\\n': ") for ln in fails), done.stdout

    def test_add_refuses_an_entry_validate_rejects_and_writes_nothing(self, fresh: Path):
        """A public entry whose `repo` is a local path: the URL rule, checked before writing.

        Validated BEFORE the file lands, not after: an entry written and then reported is
        an entry the next `git add -A` commits, and the gate would be the thing that found
        it - one commit too late.
        """
        args = list(PUBLIC_TOOL)
        args[args.index("--repo") + 1] = "../somewhere/on/this/machine"
        done = radar(*args, cwd=fresh)
        assert done.returncode != 0, f"a rejected entry was accepted:\n{done.stdout}"
        assert not (fresh / "tools" / "just.toml").exists()
        assert "repo" in done.stdout + done.stderr

    def test_add_refuses_what_the_gate_would_immediately_fail(self, fresh: Path):
        """`validate()` is not the whole gate, and a new catalogue has to pass the gate.

        `adopt` with a measurable artifact and no matcher is a FAIL from `gate.py`, not a
        schema error, so an `add` that only ran `validate()` would hand the operator a
        catalogue that fails on the very next command. Same predicate, imported, rather
        than a second opinion about the same rule.
        """
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = "adopt"
        args += ["--evidence", "used on every commit of a synthetic project"]
        done = radar(*args, cwd=fresh)
        output = done.stdout + done.stderr
        assert done.returncode != 0, f"an entry the gate fails was accepted:\n{output}"
        assert "telemetry" in output.lower(), output
        assert not (fresh / "tools" / "just.toml").exists()

    @pytest.mark.parametrize("ring", ["adopt", "pilot"])
    def test_a_ring_that_claims_use_needs_evidence(self, fresh: Path, ring: str):
        """`--ring adopt|pilot` without `--evidence` is refused by the parser, naming the flag.

        An entry at those rings claims the tool is in use, and a history block that says
        so without saying what it rests on is refused by the schema too; asking at the
        parser names the flag the operator can pass rather than the field it fills.
        """
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = ring
        done = radar(*args, cwd=fresh)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "--evidence" in done.stderr, done.stderr
        assert not (fresh / "tools" / "just.toml").exists()

    def test_evidence_that_is_only_whitespace_is_no_evidence(self, fresh: Path):
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = "adopt"
        done = radar(*args, "--evidence", "   ", cwd=fresh)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "--evidence" in done.stderr, done.stderr

    @pytest.mark.parametrize("value", ["", "   "])
    def test_add_refuses_an_empty_matcher_glob(self, fresh: Path, value: str):
        # An empty or whitespace-only matcher passes the "needs one matcher" rule while
        # matching nothing, so `radar field` reports zero use and the zero reads as
        # evidence. The schema rejects it, and `radar add` writes nothing.
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = "adopt"
        args += ["--evidence", "a synthetic run log", "--telemetry-match-command", value]
        done = radar(*args, cwd=fresh)
        assert done.returncode != 0, f"an empty matcher was accepted:\n{done.stdout}"
        assert "match_command" in (done.stdout + done.stderr), done.stdout + done.stderr
        assert not (fresh / "tools" / "just.toml").exists()

    def test_an_adopt_entry_with_evidence_is_written(self, fresh: Path):
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = "adopt"
        args += ["--evidence", "a synthetic run log", "--telemetry-match-command", "just"]
        done = radar(*args, cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        entry = tomllib.loads((fresh / "tools" / "just.toml").read_text(encoding="utf-8"))
        assert entry["history"][0]["evidence"] == "a synthetic run log"

    def test_the_registry_help_names_every_accepted_kind(self, tmp_path: Path):
        done = radar("add", "--help", cwd=outside(tmp_path))
        assert done.returncode == 0, done.stdout + done.stderr
        help_text = " ".join(done.stdout.split())
        for kind in ("pypi:<name>", "npm:<name>", "github:<owner>/<repo>"):
            assert kind in help_text, (kind, help_text)

    def test_add_refuses_to_clobber_an_existing_entry(self, fresh: Path):
        assert radar(*PUBLIC_TOOL, cwd=fresh).returncode == 0
        (fresh / "tools" / "just.toml").write_text("name = 'hand edited'\n", encoding="utf-8")
        done = radar(*PUBLIC_TOOL, cwd=fresh)
        assert done.returncode != 0, done.stdout
        assert (fresh / "tools" / "just.toml").read_text(encoding="utf-8") == (
            "name = 'hand edited'\n"
        )

    def test_add_needs_a_data_root(self, tmp_path: Path):
        done = radar(*PUBLIC_TOOL, cwd=outside(tmp_path))
        assert done.returncode == 2, done.stdout + done.stderr
        assert MARKER in done.stdout + done.stderr


class TestTheStrangerPath:
    """init, add, gate, render, end to end, in a directory that never held a catalogue.

    The one test that proves the four commands compose. Every other assertion here reads
    one artefact; this one runs the sequence a person who has never seen a catalogue would
    run, and the gate is the step that judges everything the three others wrote. Each step
    is a real process, as the stranger runs it.
    """

    def test_init_add_gate_render(self, tmp_path: Path, empty_claude_home: Path):
        target = outside(tmp_path) / "a-strangers-radar"
        assert radar("init", str(target), cwd=tmp_path, real_process=True).returncode == 0
        assert radar(*PUBLIC_TOOL, cwd=target, real_process=True).returncode == 0

        gate = radar("gate", "--claude-home", str(empty_claude_home), cwd=target, real_process=True)
        output = gate.stdout + gate.stderr
        assert gate.returncode == 0, f"the gate bit on a freshly made catalogue:\n{output}"
        assert "0 FAIL" in output, output

        rendered = radar("render", cwd=target, real_process=True)
        assert rendered.returncode == 0, rendered.stdout + rendered.stderr
        readme = (target / "README.md").read_text(encoding="utf-8")
        assert readme.strip(), "render wrote an empty README.md"
        assert "just" in readme
        # The header names the commands that make and refresh the file, and a stranger has
        # the `radar` verbs - an installed engine has no script files to name.
        assert "Generated by `radar render` - do not hand-edit." in readme
        assert "`radar snapshot` refreshes metrics -> `radar gate` applies" in readme
        assert "scripts/" not in readme
        assert (target / "docs" / "radar" / "index.html").read_text(encoding="utf-8").strip()

    def test_render_help_prints_the_options_and_writes_nothing(
        self, tmp_path: Path, empty_claude_home: Path
    ):
        # A `render` without a parser of its own would read `--public` straight from argv
        # and treat `radar render --help` as a full render that rewrites the catalogue's
        # tracked README.md. Asking a command what it does must not run it.
        target = outside(tmp_path) / "help-radar"
        assert radar("init", str(target), cwd=tmp_path).returncode == 0
        assert radar(*PUBLIC_TOOL, cwd=target).returncode == 0
        artefacts = [target / "README.md", target / "docs" / "radar" / "index.html"]
        before = [p.read_bytes() if p.exists() else None for p in artefacts]

        shown = radar("render", "--help", cwd=target)

        assert shown.returncode == 0, shown.stdout + shown.stderr
        assert "--public" in shown.stdout, shown.stdout
        after = [p.read_bytes() if p.exists() else None for p in artefacts]
        assert after == before, "render --help wrote the catalogue's artefacts"

    def test_the_rendered_title_is_the_catalogue_s_own_and_not_the_engine_s(
        self, tmp_path: Path, empty_claude_home: Path
    ):
        # The title rule, seen from the stranger's side: an engine anyone can install must not
        # title their radar with any name but the one their own marker carries.
        target = outside(tmp_path) / "borrowed-name-radar"
        assert (
            radar("init", str(target), "--title", "Someone Else's Radar", cwd=tmp_path).returncode
            == 0
        )
        assert radar(*PUBLIC_TOOL, cwd=target).returncode == 0
        assert radar("render", cwd=target).returncode == 0
        readme = (target / "README.md").read_text(encoding="utf-8")
        assert readme.splitlines()[0] == "# Someone Else's Radar"


def synthetic_home(tmp_path: Path) -> dict[str, str]:
    """An environment whose home directory is a synthetic one under tmp_path.

    `Path.home()` reads USERPROFILE on Windows and HOME elsewhere, so both are set: the
    profile a subprocess writes then carries this directory's paths, never the operator's.
    """
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    return {"HOME": str(home), "USERPROFILE": str(home)}


def radar_at_home(*args: str, cwd: Path, home: dict[str, str]) -> subprocess.CompletedProcess:
    return run_engine("cli", args, cwd=cwd, env=child_env(PYTHONPATH=str(SRC), **home))


class TestProfilesNameCommandsNotModules:
    """What a written profile or overlay tells its reader names a `radar` verb.

    An installed engine has no module files to run, and a comment that points at one sends
    the reader looking for a script that does not exist. The same goes for a remote the
    reader's catalogue may not have: the comment describes the catalogue's own remote.
    """

    def test_the_starter_profile_comments(self, tmp_path: Path):
        home = synthetic_home(tmp_path)
        target = outside(tmp_path) / "profile-radar"
        done = radar_at_home("init", str(target), cwd=tmp_path, home=home)
        assert done.returncode == 0, done.stdout + done.stderr
        text = (target / "environments" / "default.toml").read_text(encoding="utf-8")
        flat = comments(text)
        assert "`radar apply` checks presence" in flat, flat
        assert "reach the catalogue's remote" in flat, flat
        assert not re.search(r"\b\w+\.py\b", text), text
        # The overlay comment explains the gitignored machine-local file in its own terms.
        assert "paths only this machine has" in flat, flat
        # The starter profile defaults sync to the common case, a reachable remote, which
        # is the path the walk-through makes primary; git-bundle is the documented
        # alternative.
        assert 'sync = "git-remote"' in text, text

    def test_the_proposed_overlay_comments(self, tmp_path: Path):
        home = synthetic_home(tmp_path)
        target = outside(tmp_path) / "overlay-radar"
        assert radar_at_home("init", str(target), cwd=tmp_path, home=home).returncode == 0
        profile = target / "environments" / "default.toml"
        text = profile.read_text(encoding="utf-8")
        profile.write_text(
            text.replace('documents = "~/Documents"', 'documents = "~/Elsewhere"'),
            encoding="utf-8",
        )
        done = radar_at_home("bootstrap", "--env", "default", cwd=target, home=home)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "layers this file over it" in done.stdout, done.stdout
        assert "apply.py" not in done.stdout, done.stdout

    def test_the_env_help_uses_a_neutral_example(self, tmp_path: Path):
        done = radar("bootstrap", "--help", cwd=outside(tmp_path))
        assert done.returncode == 0, done.stdout + done.stderr
        assert "e.g. laptop" in done.stdout, done.stdout


class TestTheGeneratedLintSkipsTheCatalogueData:
    """`uv run ruff` in a catalogue lints its code and never reads an entry as a config.

    Ruff treats every `ruff.toml` it meets under the project as a configuration file, and
    an entry's file is named after its tool, so `radar add ruff` writes `tools/ruff.toml` -
    an entry, which ruff then fails to load as a config. The generated workflow runs ruff
    over the whole tree, so the catalogue's CI would fail for holding that entry.
    """

    def test_the_generated_pyproject_excludes_the_entry_directories(self, fresh: Path):
        pyproject = tomllib.loads((fresh / "pyproject.toml").read_text(encoding="utf-8"))
        excluded = pyproject["tool"]["ruff"].get("extend-exclude", [])
        assert {"tools", "tools.local"} <= set(excluded), excluded

    @pytest.mark.parametrize("command", [("check", "."), ("format", "--check", ".")])
    def test_ruff_passes_on_a_catalogue_holding_an_entry_named_ruff(
        self, fresh: Path, command: tuple[str, ...]
    ):
        pytest.importorskip("ruff")
        added = radar(
            "add",
            "ruff",
            "--repo",
            "https://github.com/astral-sh/ruff",
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
            "Linter and formatter",
            "--reason",
            "Entered the catalogue on the day it was first looked at",
            cwd=fresh,
        )
        assert added.returncode == 0, added.stdout + added.stderr
        assert (fresh / "tools" / "ruff.toml").is_file()
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "ruff", *command],
            cwd=fresh,
            capture_output=True,
            text=True,
            timeout=120,
            env=child_env(),
        )
        assert done.returncode == 0, done.stdout + done.stderr


class TestBootstrapNamesOnlyASafeEnvironment:
    """`radar bootstrap --env` names a file under environments/, so it is a file name.

    The value becomes `environments/<env>.toml` and a TOML string inside it. A value with a
    path separator writes outside the catalogue, and one with a quote writes a profile no
    TOML reader accepts, so both are refused before anything is read or written.
    """

    @pytest.mark.parametrize("name", ["../../escaped", "..", 'a"b', "a/b", ".hidden", ""])
    def test_an_unsafe_name_is_refused_and_nothing_is_written(
        self, fresh: Path, tmp_path: Path, name: str
    ):
        before = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)
        done = radar("bootstrap", "--env", name, "--write", cwd=fresh)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "--env" in done.stderr, done.stderr
        after = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)
        assert after == before, set(after) - set(before)

    def test_a_plain_name_is_accepted(self, fresh: Path):
        done = radar("bootstrap", "--env", "laptop", "--write", cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        assert (fresh / "environments" / "laptop.toml").is_file()

    def test_the_printed_commands_carry_no_trailing_comment(self, fresh: Path):
        # The same reason as for `radar init`'s steps: cmd reads `#` as an argument.
        done = radar("bootstrap", "--env", "default", cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        tail = done.stdout.split("next:\n", 1)[1]
        commands = [ln.strip()[2:] for ln in tail.splitlines() if ln.strip().startswith("- radar")]
        assert commands, tail
        assert not any("#" in c for c in commands), commands


class TestTheNameIsTheFile:
    """An entry lives at tools/<name>.toml, and a file name is lowercase with single
    hyphens. `radar add` refuses a name that would sit in a file named differently from
    itself, so a name and its file are always one, and two names that differ only in case
    cannot both claim one file."""

    ARGS = (
        "--repo",
        "https://example.invalid/x",
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

    @pytest.mark.parametrize(("name", "form"), [("MyTool", "mytool"), ("a--b", "a-b")])
    def test_a_name_unlike_its_file_is_refused(self, fresh: Path, name: str, form: str):
        done = radar("add", name, *self.ARGS, cwd=fresh)
        assert done.returncode == 1, done.stdout + done.stderr
        assert f"enter the name as {form!r}" in done.stdout, done.stdout
        assert not (fresh / "tools" / f"{form}.toml").exists()

    def test_a_name_that_is_its_file_is_written(self, fresh: Path):
        done = radar("add", "my-tool", *self.ARGS, cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        assert (fresh / "tools" / "my-tool.toml").is_file()

    def test_a_name_another_file_claims_is_refused(self, fresh: Path):
        # A file written by hand under another name already claims `my-tool`: a second
        # entry for it would put the tool in the catalogue twice.
        assert radar("add", "my-tool", *self.ARGS, cwd=fresh).returncode == 0
        (fresh / "tools" / "my-tool.toml").rename(fresh / "tools" / "other.toml")
        done = radar("add", "my-tool", *self.ARGS, cwd=fresh)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "claimed by 2 entries" in done.stdout, done.stdout
        assert not (fresh / "tools" / "my-tool.toml").exists()

    def test_an_entry_that_does_not_parse_is_named_and_does_not_stop_the_write(self, fresh: Path):
        # The name rule reads every other entry. One that is not TOML is the gate's to
        # fail; it claims the name its file gives, and `radar add` says it could not read it.
        (fresh / "tools" / "broken.toml").write_text('name = "broken\n', encoding="utf-8")
        done = radar("add", "my-tool", *self.ARGS, cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Traceback" not in done.stderr, done.stderr
        assert "[WARN] tools/broken.toml does not parse" in done.stdout, done.stdout
        assert (fresh / "tools" / "my-tool.toml").is_file()

    def test_an_entry_that_does_not_parse_still_claims_its_file_name(self, fresh: Path):
        (fresh / "tools.local").mkdir(exist_ok=True)
        (fresh / "tools.local" / "my-tool.toml").write_text("name = [\n", encoding="utf-8")
        done = radar("add", "my-tool", *self.ARGS, cwd=fresh)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "claimed by 2 entries" in done.stdout, done.stdout
        assert not (fresh / "tools" / "my-tool.toml").exists()


def test_the_generated_changelog_describes_the_tree_init_writes(fresh: Path):
    # `tools/` carries its README from the start, so the directory survives a commit.
    changelog = " ".join((fresh / "CHANGELOG.md").read_text(encoding="utf-8").split())
    assert (fresh / "tools" / "README.md").is_file()
    assert "empty `tools/`" not in changelog, changelog
    assert "`tools/` holding only its README" in changelog, changelog
    # "tools" starts with a consonant sound, so the article is "a", not "an".
    assert "an `tools/`" not in changelog, changelog
    assert "a `tools/`" in changelog, changelog


def test_line_endings_are_kept_lf_on_every_checkout(fresh: Path):
    # The hooks are `#!/bin/sh` scripts git runs directly, and a checkout that gave them
    # CRLF endings would put a carriage return into the interpreter path.
    rules = [
        line
        for line in (fresh / ".gitattributes").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert rules == ["* text=auto eol=lf"], rules


def test_bootstrap_points_at_the_policy_guide_in_the_engine_repository(fresh: Path):
    from stack_radar import __version__

    done = radar("bootstrap", "--env", "default", cwd=fresh)
    assert done.returncode == 0, done.stdout + done.stderr
    page = f"https://github.com/grimaldost/stack-radar/blob/v{__version__}/docs/new-environment.md"
    assert page in done.stdout, done.stdout


def test_add_refuses_a_telemetry_since_that_is_not_a_day(fresh: Path):
    done = radar(
        "add",
        "counter",
        *TestTheNameIsTheFile.ARGS,
        "--telemetry-match-command",
        "counter",
        "--telemetry-since",
        "2026-99-01",
        cwd=fresh,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "[telemetry].since '2026-99-01' is not a real day" in done.stdout, done.stdout
    assert not (fresh / "tools" / "counter.toml").exists()


class TestTelemetrySince:
    """`--telemetry-since` is recorded only inside a [telemetry] block, which exists only
    with a matcher. Given alone, the flag is refused and the matcher flags named, rather
    than dropped while the operator believes a window is recorded."""

    def test_since_without_a_matcher_is_refused(self, fresh: Path):
        done = radar(*PUBLIC_TOOL, "--telemetry-since", "2026-08-01", cwd=fresh)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "--telemetry-since" in done.stderr, done.stderr
        assert not (fresh / "tools" / "just.toml").exists()

    def test_since_with_a_matcher_is_accepted(self, fresh: Path):
        args = list(PUBLIC_TOOL)
        args[args.index("--ring") + 1] = "adopt"
        args += [
            "--evidence",
            "a synthetic run log",
            "--telemetry-match-command",
            "just",
            "--telemetry-since",
            "2026-08-01",
        ]
        done = radar(*args, cwd=fresh)
        assert done.returncode == 0, done.stdout + done.stderr
        entry = tomllib.loads((fresh / "tools" / "just.toml").read_text(encoding="utf-8"))
        assert entry["telemetry"]["since"] == "2026-08-01"
