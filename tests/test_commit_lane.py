"""The commit lane is armed by one command, so everything that command relies on is checked.

`git config core.hooksPath .githooks` is the one command CONTRIBUTING gives for arming the
lane, and the hooks it points at are tracked files rather than generated ones precisely so
a fresh clone needs nothing else. Three things have to hold for that to be true, and each
fails without a word from anything else in this repository - CI runs on a runner that
never commits, so it never reaches a hook:

1. EVERY HOOK IS EXECUTABLE IN THE INDEX. On Windows git runs a hook whatever its mode; on
   Linux and macOS git SKIPS a hook that is not executable, printing only a hint. A
   clone there reports the lane armed while no check runs - the "looking armed while
   nothing runs" failure these tracked files exist to avoid.
   The mode lives in the index, so that is what is read: a working-tree `os.access(X_OK)`
   is meaningless on Windows and would pass on a clone where the bit was lost.

2. THE CONFIG EACH HOOK NAMES IS TRACKED, and declares a hook for that hook's stage. A
   hook pointing at a config the tree does not carry fails every commit before any check
   runs; a config with nothing at that stage runs nothing and passes.

3. PRE-COMMIT IS IN THE DEV GROUP. The hooks run `uv run --frozen python -m pre_commit`,
   so the module has to be in the environment `uv sync` builds, or every commit fails on
   an import error.

And one thing has to hold for the lane to be safe to arm: EVERY `uv run` IT MAKES IS
FROZEN. Without `--frozen`, `uv run` re-resolves a `pyproject.toml` that `uv.lock` no
longer matches and installs the result, so a pull request that changes only
`pyproject.toml` would run new code at the next commit on a branch checked out to review
it. Frozen, uv installs the project's dependencies from `uv.lock` alone.

What `--frozen` does not stop is a rebuild. A project that is itself built, as this one is,
is rebuilt when `pyproject.toml` changes, and the rebuild installs the `[build-system]`
requirements and runs the build backend that file names. The last test here holds that
it does, which is the reason the documents give for keeping `pyproject.toml` on the
review list.

Last, THE ATTRIBUTION HOOK IS THERE AND WORKS. The `no-ai-attribution` hook is the only
thing that enforces CONTRIBUTING's "No AI-attribution trailers" (no CI job reads a commit
message), and nothing else here names it: deleting it, or loosening its pattern until it
matches nothing, would leave every other test green. The tests at the end run the hook the
way git does, through pre-commit at the `commit-msg` stage, against message files.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from pre_commit.parse_shebang import normalize_cmd

# The checkout under test: this one, or the one whose `src/` `RADAR_ENGINE_SRC` names, which
# is how CONTRIBUTING's red-proof procedure points the suite at an earlier commit.
# `RADAR_DATA_ROOT` is NOT read here: it names a catalogue, and an operator who exports it
# for their catalogue would otherwise have these tests judge that catalogue's hooks.
REPO = (
    Path(os.environ["RADAR_ENGINE_SRC"]).resolve().parent
    if os.environ.get("RADAR_ENGINE_SRC")
    else Path(__file__).resolve().parent.parent
)

EXECUTABLE = "100755"
HOOKS = (".githooks/pre-commit", ".githooks/commit-msg")
CONFIG = ".pre-commit-config.yaml"

_CONFIG = re.compile(r"--config=(\S+)")
_HOOK_TYPE = re.compile(r"--hook-type=(\S+)")


def tracked(*paths: str) -> list[tuple[str, str]]:
    """`(mode, path)` for every tracked file under `paths`."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO), "ls-files", "-s", "--", *paths],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    entries = []
    for line in out:
        meta, path = line.split("\t", 1)
        entries.append((meta.split()[0], path.strip()))
    return entries


def tracked_hooks() -> list[tuple[str, str]]:
    """`(mode, path)` for every tracked file under `.githooks/`."""
    return tracked(".githooks/")


def test_every_tracked_hook_is_executable_in_the_index():
    hooks = tracked_hooks()
    # Vacuity guard: an empty listing would pass the assertion below and prove nothing.
    assert {path for _mode, path in hooks} >= set(HOOKS), hooks
    not_executable = [path for mode, path in hooks if mode != EXECUTABLE]
    assert not not_executable, (
        f"tracked as non-executable: {', '.join(not_executable)}. Git skips a hook "
        f"without the executable bit on POSIX, so `git config core.hooksPath "
        f".githooks` would arm nothing there. Fix with: git update-index --chmod=+x "
        f"{' '.join(not_executable)}"
    )


def test_each_hook_runs_a_tracked_config_that_declares_its_stage():
    for hook in HOOKS:
        text = (REPO / hook).read_text(encoding="utf-8")
        config = _CONFIG.search(text)
        hook_type = _HOOK_TYPE.search(text)
        assert config and hook_type, f"{hook} names no --config or no --hook-type"
        assert hook_type.group(1) == Path(hook).name, (
            f"{hook} runs the {hook_type.group(1)!r} stage - git calls it for "
            f"{Path(hook).name!r}, so the hooks declared for that stage would never run"
        )
        name = config.group(1)
        assert tracked(name), (
            f"{hook} runs {name}, which this repository does not track - every commit on a "
            "clone that armed the lane fails before any check runs"
        )
        declared = (REPO / name).read_text(encoding="utf-8")
        assert re.search(rf"stages:\s*\[\s*{re.escape(hook_type.group(1))}\s*\]", declared), (
            f"{name} declares no hook at the {hook_type.group(1)!r} stage, so {hook} runs "
            "nothing and passes"
        )


def test_pre_commit_is_in_the_dev_group():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    dev = project.get("dependency-groups", {}).get("dev", [])
    names = {re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].lower() for spec in dev}
    assert "pre-commit" in names, (
        "`pre-commit` is not in [dependency-groups].dev - the hooks run "
        "`uv run python -m pre_commit`, so every commit on an armed clone would fail on "
        "the import"
    )


_UNFROZEN = re.compile(r"\buv run\b(?! --frozen\b)")


def unfrozen_uv_runs(root: Path, paths: tuple[str, ...]) -> list[str]:
    """Each line under `paths`, comments aside, that runs `uv run` without `--frozen`."""
    found = []
    for rel in paths:
        for n, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.strip()
            if not code.startswith("#") and _UNFROZEN.search(code):
                found.append(f"{rel}:{n}: {code}")
    return found


def test_every_uv_run_the_lane_makes_is_frozen():
    for hook in HOOKS:
        # Vacuity guard: a hook that stopped calling uv would pass the check below.
        assert "uv run --frozen" in (REPO / hook).read_text(encoding="utf-8"), hook
    found = unfrozen_uv_runs(REPO, (*HOOKS, CONFIG))
    assert not found, (
        "`uv run` without --frozen re-resolves a pyproject.toml that uv.lock does not "
        "match and installs the result at commit time:\n" + "\n".join(found)
    )


def hook_uv_words(hook: str) -> list[str]:
    """The words a hook's `exec` line starts with, up to and including `python`."""
    text = (REPO / hook).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("exec "))
    words = shlex.split(line.rstrip().removesuffix("\\"))[1:]
    return words[: words.index("python") + 1]


PROJECT = """\
[project]
name = "lane"
version = "0"
requires-python = ">=3.11"
dependencies = []

[tool.uv]
package = false
"""

# A dependency whose build backend leaves a marker the moment uv loads it, which is what
# resolving it does: its metadata comes from the backend.
PLANTED_PROJECT = """\
[project]
name = "planted"
version = "0"

[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]
"""

PLANTED_BACKEND = """\
import pathlib
pathlib.Path({marker!r}).write_text("built", encoding="utf-8")
def get_requires_for_build_wheel(config_settings=None):
    return []
def prepare_metadata_for_build_wheel(directory, config_settings=None):
    info = pathlib.Path(directory) / "planted-0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\\nName: planted\\nVersion: 0\\n")
    return info.name
def build_wheel(*args, **kwargs):
    raise SystemExit("not built")
"""


def uv_runner(project: Path, cache: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run uv in `project`: offline, with the cache `cache`, on this interpreter."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(
        UV_CACHE_DIR=str(cache),
        UV_OFFLINE="1",
        UV_PYTHON=sys.executable,
        UV_PYTHON_DOWNLOADS="never",
    )

    def uv_run(*words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, *words], cwd=project, env=env, capture_output=True, text=True, timeout=300
        )

    return uv_run


def test_a_dependency_only_pyproject_declares_does_not_run_at_commit(tmp_path: Path):
    # The pull request: a dependency added to pyproject.toml, uv.lock left as it was. Each
    # hook's own `uv run` words run against it, offline, with a cache of their own.
    project = tmp_path / "project"
    planted = project / "planted"
    planted.mkdir(parents=True)
    marker = tmp_path / "built"
    uv_run = uv_runner(project, tmp_path / "cache")
    (project / "pyproject.toml").write_text(PROJECT, encoding="utf-8")
    (planted / "pyproject.toml").write_text(PLANTED_PROJECT, encoding="utf-8")
    (planted / "backend.py").write_text(
        PLANTED_BACKEND.format(marker=marker.as_posix()), encoding="utf-8"
    )

    locked = uv_run("lock")
    assert locked.returncode == 0, locked.stdout + locked.stderr
    (project / "pyproject.toml").write_text(
        PROJECT.replace("dependencies = []", 'dependencies = ["planted"]')
        + '\n[tool.uv.sources]\nplanted = { path = "planted" }\n',
        encoding="utf-8",
    )
    for hook in HOOKS:
        words = hook_uv_words(hook)
        assert words[0] == "uv", (hook, words)
        done = uv_run(*words[1:], "-c", "print('hook ran')")
        assert not marker.exists(), (
            f"{hook} resolved and built a dependency that only pyproject.toml declares:\n"
            + done.stdout
            + done.stderr
        )
        # Not vacuous: the hook's interpreter ran, from the locked environment.
        assert "hook ran" in done.stdout, done.stdout + done.stderr

    # The control: plain `uv run` does build it, so the scenario above is a live one here.
    uv_run("run", "python", "-c", "pass")
    assert marker.exists(), "plain `uv run` built nothing, so this test proves nothing here"


# A project that is built, by a backend in its own tree. `backend` builds a wheel holding
# only the project's metadata; `planted` leaves a marker the moment uv loads it, then builds
# the same wheel.
BUILT_PROJECT = """\
[project]
name = "lane"
version = "0"
requires-python = ">=3.11"
dependencies = []

[build-system]
requires = []
build-backend = "{backend}"
backend-path = ["."]
"""

BUILT_BACKEND = r"""
import base64, hashlib, pathlib, zipfile
_INFO = "lane-0.dist-info"
_FILES = {
    f"{_INFO}/METADATA": "Metadata-Version: 2.1\nName: lane\nVersion: 0\n",
    f"{_INFO}/WHEEL": (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ),
}
def _hash(text):
    data = text.encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest},{len(data)}"
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "lane-0-py3-none-any.whl"
    record = "".join(f"{p},{_hash(t)}\n" for p, t in _FILES.items()) + f"{_INFO}/RECORD,,\n"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as wheel:
        for p, t in _FILES.items():
            wheel.writestr(p, t)
        wheel.writestr(f"{_INFO}/RECORD", record)
    return name
build_editable = build_wheel
"""

PLANTED_BUILD_BACKEND = """\
import pathlib
pathlib.Path({marker!r}).write_text("built", encoding="utf-8")
from backend import *
"""


def test_a_built_project_still_runs_the_build_backend_pyproject_names(tmp_path: Path):
    # The pull request: a built project's pyproject.toml names another build backend,
    # uv.lock left as it was. `--frozen` installs nothing uv.lock does not list, but the
    # project itself is rebuilt, and the backend pyproject.toml names runs at commit time.
    # This is why pyproject.toml stays on the review list of a built project.
    for hook in HOOKS:
        words = hook_uv_words(hook)
        assert words[0] == "uv", (hook, words)
        work = tmp_path / Path(hook).name
        project = work / "project"
        project.mkdir(parents=True)
        marker = work / "built"
        uv_run = uv_runner(project, work / "cache")
        (project / "backend.py").write_text(BUILT_BACKEND, encoding="utf-8")
        (project / "planted.py").write_text(
            PLANTED_BUILD_BACKEND.format(marker=marker.as_posix()), encoding="utf-8"
        )
        pyproject = project / "pyproject.toml"
        pyproject.write_text(BUILT_PROJECT.format(backend="backend"), encoding="utf-8")
        locked = uv_run("lock")
        assert locked.returncode == 0, locked.stdout + locked.stderr
        # Before the change: the project builds with its own backend and the hook runs.
        before = uv_run(*words[1:], "-c", "print('hook ran')")
        assert "hook ran" in before.stdout, before.stdout + before.stderr
        assert not marker.exists(), before.stdout + before.stderr

        pyproject.write_text(BUILT_PROJECT.format(backend="planted"), encoding="utf-8")
        after = uv_run(*words[1:], "-c", "print('hook ran')")
        assert marker.exists(), (
            f"{hook}: uv did not rebuild a built project under --frozen after "
            "pyproject.toml named another build backend. SECURITY.md, CONTRIBUTING.md, the "
            "0.2.1 CHANGELOG entry and this repository's hook comments say it does; bring "
            "them in line with what uv does now.\n" + after.stdout + after.stderr
        )
        assert "hook ran" in after.stdout, after.stdout + after.stderr


ATTRIBUTION_HOOK = "no-ai-attribution"


def attribution_only_config() -> str:
    """The real config cut down to the repository entry that holds `no-ai-attribution`.

    The other entries (ruff, `conventional-pre-commit`) are not under test, and
    `pre-commit run` clones every remote repository in the config it is given, so the full
    file would make each test fetch `conventional-pre-commit` from github.com and fail
    offline, as the rest of this module's tests do not. The local entry needs no clone.
    The entry is read from the real file, so the hook under test is the one that ships.
    """
    declared = yaml.safe_load((REPO / CONFIG).read_text(encoding="utf-8"))
    kept = [
        entry
        for entry in declared["repos"]
        if entry.get("repo") == "local"
        and any(hook.get("id") == ATTRIBUTION_HOOK for hook in entry.get("hooks", []))
    ]
    if not kept:
        pytest.fail(f"{CONFIG} declares no local `{ATTRIBUTION_HOOK}` hook to run")
    return yaml.safe_dump({"repos": kept})


def run_attribution_hook(
    tmp_path: Path,
    message: str,
    *,
    lang: str | None = None,
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the `no-ai-attribution` hook on `message` as git does at `commit-msg`.

    `lang` runs the hook under that `LANG` with `LC_ALL` and `LC_CTYPE` removed, the way a
    caller's shell sets a locale; None leaves the inherited environment alone. `path`
    replaces `PATH` for the hook's own run (pre-commit resolves `bash` from it).

    Through `pre-commit run --hook-stage commit-msg`, so the hook's id, its stage and its
    entry are all exercised. The config is a scratch copy holding only the local hook (see
    `attribution_only_config`), staged in a scratch repository: pre-commit refuses a config
    with unstaged changes, which would fail these tests in the middle of an edit to the
    very file they judge, and the scratch repository gives it a cache of its own, so
    nothing is written to the machine's `~/.cache/pre-commit`. Nothing here reaches the
    network.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-home")
    if lang is not None:
        env.pop("LC_ALL", None)
        env.pop("LC_CTYPE", None)
        env["LANG"] = lang
    (repo / CONFIG).write_text(attribution_only_config(), encoding="utf-8")
    subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
        ["git", "init", "-q"], cwd=repo, env=env, check=True, capture_output=True
    )
    subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
        ["git", "add", CONFIG], cwd=repo, env=env, check=True, capture_output=True
    )
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(message.encode("utf-8"))
    if path is not None:
        env["PATH"] = path
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            ATTRIBUTION_HOOK,
            "--hook-stage",
            "commit-msg",
            "--commit-msg-filename",
            str(msg),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_the_config_declares_the_attribution_hook_at_commit_msg():
    declared = (REPO / CONFIG).read_text(encoding="utf-8")
    block = re.search(
        rf"^\s*- id: {ATTRIBUTION_HOOK}\s*$(.*?)(?=^\s*- id: |^\s*- repo: |\Z)",
        declared,
        re.MULTILINE | re.DOTALL,
    )
    assert block, (
        f"{CONFIG} declares no `{ATTRIBUTION_HOOK}` hook, so CONTRIBUTING's rule against "
        "AI-attribution trailers is checked by nothing"
    )
    assert re.search(r"stages:\s*\[\s*commit-msg\s*\]", block.group(1)), (
        f"`{ATTRIBUTION_HOOK}` is not at the `commit-msg` stage, the only place a message "
        "can be read before it is written"
    )


SCISSORS = "# " + "-" * 24 + " >8 " + "-" * 24

# CONTRIBUTING: "a person called Claude, an indented quote and the diff of `git commit -v`
# all pass".
ORDINARY_MESSAGES = {
    "plain": "fix(trust): annotate scratch_dir\n\nNo behaviour moves.\n",
    "person-called-claude": "docs: thank a reviewer\n\nCo-Authored-By: Claude Monet <monet@example.org>\n",
    "indented-quote": "docs: explain the rule\n\n    Co-Authored-By: Claude <noreply@anthropic.com>\n",
    "comment-line": "fix: x\n\n# Co-Authored-By: Claude <noreply@anthropic.com>\n",
    "below-scissors": f"fix: x\n\n{SCISSORS}\n+Co-Authored-By: Claude <noreply@anthropic.com>\n",
}

ATTRIBUTED_MESSAGES = {
    "claude-trailer": "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
    "claude-model-trailer": "fix: x\n\nCo-Authored-By: Claude Opus 4 <noreply@anthropic.com>\n",
    "bot-address-only": "fix: x\n\nCo-authored-by: Assistant <noreply@anthropic.com>\n",
    "generated-with-footer": "fix: x\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n",
    "plain-generated-with": "fix: x\n\nGenerated with Claude Code\n",
}


# The hook's match must not depend on the caller's locale: under a UTF-8 locale an emoji is
# one character, and a `[:alnum:]` that counts it as a letter let a robot-emoji footer
# through. `None` is the inherited environment.
LOCALES = {"inherited": None, "C.UTF-8": "C.UTF-8", "en_GB.UTF-8": "en_GB.UTF-8"}


@pytest.mark.parametrize("lang", LOCALES.values(), ids=LOCALES.keys())
@pytest.mark.parametrize("message", ORDINARY_MESSAGES.values(), ids=ORDINARY_MESSAGES.keys())
def test_the_attribution_hook_accepts_an_ordinary_message(
    tmp_path: Path, message: str, lang: str | None
):
    done = run_attribution_hook(tmp_path, message, lang=lang)
    out = done.stdout + done.stderr
    # `Passed` is asserted as well as the exit code: a hook that is missing, or skipped for
    # want of a file, must not read as a pass.
    assert done.returncode == 0 and "Passed" in out, out


@pytest.mark.parametrize("lang", LOCALES.values(), ids=LOCALES.keys())
@pytest.mark.parametrize("message", ATTRIBUTED_MESSAGES.values(), ids=ATTRIBUTED_MESSAGES.keys())
def test_the_attribution_hook_rejects_an_ai_attribution(
    tmp_path: Path, message: str, lang: str | None
):
    done = run_attribution_hook(tmp_path, message, lang=lang)
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "AI-attribution" in out, (
        f"the hook let an AI-attributed message through (exit {done.returncode}):\n{out}"
    )


def attribution_entry() -> list[str]:
    """The hook's `entry` as pre-commit splits it: `bash -c '<script>' --`."""
    declared = yaml.safe_load((REPO / CONFIG).read_text(encoding="utf-8"))
    for repo in declared["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == ATTRIBUTION_HOOK:
                return shlex.split(hook["entry"])
    pytest.fail(f"{CONFIG} declares no `{ATTRIBUTION_HOOK}` hook")


def run_attribution_entry(
    message_path: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the hook's entry on `message_path` the way pre-commit calls it.

    `shlex.split(entry) + [path]`, with `bash` resolved as pre-commit resolves it, and no
    other part of pre-commit in between. `env` is added to the inherited environment for
    the entry's own run.
    """
    cmd = normalize_cmd((*attribution_entry(), str(message_path)))
    env = None if env is None else {**os.environ, **env}
    return subprocess.run(  # noqa: S603 - argv read from the tracked config, no shell
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, env=env
    )


def test_the_attribution_hook_refuses_a_message_file_it_cannot_read(tmp_path: Path):
    # The entry is called as pre-commit calls it, `shlex.split(entry) + [path]`, because
    # pre-commit drops a message path that does not exist before the hook runs and reports
    # the hook as skipped. `bash` is resolved the way pre-commit resolves it.
    missing = tmp_path / "no-such-message"
    done = run_attribution_entry(missing)
    out = done.stdout + done.stderr
    assert done.returncode == 1, f"an unreadable message passed (exit {done.returncode}):\n{out}"
    assert "cannot read the commit message file" in out and str(missing) in out, out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a PATH holding only symlinks to bash and git, which Windows cannot make "
    "without privileges",
)
def test_the_attribution_hook_refuses_when_sed_is_not_found(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "git"):
        found = shutil.which(name)
        assert found, f"`{name}` is not on PATH"
        (tools / name).symlink_to(found)
    done = run_attribution_hook(tmp_path, "fix: x\n\nAn ordinary message.\n", path=str(tools))
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "sed was not found" in out, (
        f"a hook that cannot run `sed` passed (exit {done.returncode}):\n{out}"
    )


def test_the_attribution_hook_refuses_when_sed_fails_on_a_readable_path(tmp_path: Path):
    # A directory passes `[ -r ]` and fails in `sed`, on every OS: only the status of the
    # `sed` command substitution stands between that failure and a commit that was never read.
    done = run_attribution_entry(tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "sed failed" in out, (
        f"a hook whose `sed` failed passed (exit {done.returncode}):\n{out}"
    )


def test_the_attribution_hook_refuses_when_grep_fails(tmp_path: Path):
    # `grep` exits 2 on an error, and a bare `grep` pipeline reads that as "no match".
    # The failing `grep` is an exported bash function that returns 2, not a stub on PATH:
    # Git for Windows' `bin/bash.exe` wrapper puts its own `/usr/bin` first on PATH, where the
    # real `grep` would shadow a stub. `command -v grep` finds the function and the pipeline
    # runs it.
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_bytes(b"fix: x\n\nAn ordinary message.\n")
    done = run_attribution_entry(message, env={"BASH_FUNC_grep%%": "() { return 2; }"})
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "grep exited 2" in out, (
        f"a hook whose `grep` exited 2 passed (exit {done.returncode}):\n{out}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a PATH holding only symlinks to bash, git and sed, which Windows cannot "
    "make without privileges",
)
def test_the_attribution_hook_refuses_when_grep_is_not_found(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "git", "sed"):
        found = shutil.which(name)
        assert found, f"`{name}` is not on PATH"
        (tools / name).symlink_to(found)
    done = run_attribution_hook(tmp_path, "fix: x\n\nAn ordinary message.\n", path=str(tools))
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "grep was not found" in out, (
        f"a hook without `grep` did not report it missing (exit {done.returncode}):\n{out}"
    )
