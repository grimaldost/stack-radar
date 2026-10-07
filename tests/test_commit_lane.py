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
