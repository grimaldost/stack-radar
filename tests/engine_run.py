"""A catalogue under tmp_path, and the engine run against it the way an operator runs it.

HOW A COMMAND RUNS. `run_engine` runs `python -m stack_radar.<module>` IN THIS PROCESS
by default: the module is executed as `__main__` with the same argv, working directory
and environment a subprocess would get, and its output and exit code come back as a
`CompletedProcess`. A new interpreter per command costs far more than the command itself
wherever process start-up is expensive, and the suite runs many commands.

It runs a real SUBPROCESS instead, with `RADAR_ENGINE_SRC` (or this checkout's `src/`)
first on PYTHONPATH, in two cases:

- `RADAR_ENGINE_SRC` is set. That is what makes a red proof possible: pointed at a `src/`
  from an earlier commit, the same test runs the earlier engine, which an in-process call
  cannot do - it runs whichever copy of the package this interpreter imported.
- `RADAR_TEST_SUBPROCESS=1` is set, which runs every command end to end, as a check that
  the in-process runner hides nothing a real process start would show.

The properties that only a real process start has - console encoding, `python -m`
reaching the package by name, argv and exit codes at the process boundary - keep their
own subprocess tests (tests/test_verb_help.py, tests/test_data_root.py and the stranger
path in tests/test_init.py), which do not go through this runner.

Everything a command could read from the machine is pinned to the fixture: the home
directory (`HOME` and `USERPROFILE`), uv's tool directory (`UV_TOOL_DIR`), and git's
global and system configuration. A test here never reads the operator's real home, uv
tools or git config, and never reads a real catalogue - the data root is found by the
marker search from inside tmp_path, with `RADAR_DATA_ROOT` removed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import traceback
import warnings
from collections.abc import Callable
from pathlib import Path

import stack_radar

REPO = Path(__file__).resolve().parent.parent
ENGINE_SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))
PY = Path(sys.executable).as_posix()

# In-process only when this interpreter's `stack_radar` IS the engine under test.
IN_PROCESS = (
    not os.environ.get("RADAR_ENGINE_SRC")
    and not os.environ.get("RADAR_TEST_SUBPROCESS")
    and Path(stack_radar.__file__).resolve().is_relative_to(ENGINE_SRC.resolve())
)


class _Text(io.StringIO):
    """Captured output that reports an encoding, as a real stream does."""

    encoding = "utf-8"


def _exit_code(code: object, err: io.StringIO) -> int:
    """The process exit status `sys.exit(code)` produces."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=err)
    return 1


def call_main(
    main: Callable[[], object], argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Call `main` in this process as a command would run: `argv`, `cwd` and exactly `env`
    in place, output captured, `sys.exit` turned into a return code, an uncaught
    exception into a traceback on stderr and exit 1. Everything is restored after."""
    saved_env, saved_cwd, saved_argv = dict(os.environ), os.getcwd(), sys.argv
    out, err = _Text(), _Text()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(cwd)
        sys.argv = list(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = main()
                code = result if isinstance(result, int) else 0
            except SystemExit as e:
                code = _exit_code(e.code, err)
            except Exception:  # noqa: BLE001 - reported as an interpreter would
                traceback.print_exc(file=err)
                code = 1
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv = saved_argv
    return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())


def _run_module(module: str) -> None:
    with warnings.catch_warnings():
        # runpy says the module is already imported; `python -m` would not.
        warnings.filterwarnings("ignore", category=RuntimeWarning, module="runpy")
        runpy.run_module(f"stack_radar.{module}", run_name="__main__", alter_sys=True)


def run_engine(
    module: str,
    args: tuple[str, ...] | list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """`python -m stack_radar.<module> *args` in `cwd` with exactly `env` (see the module
    docstring for when this is a subprocess and when it runs in this process)."""
    argv = [sys.executable, "-m", f"stack_radar.{module}", *args]
    if not IN_PROCESS:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    done = call_main(
        lambda: _run_module(module), [f"stack_radar.{module}", *args], cwd=cwd, env=env
    )
    return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)


MARKER = """schema = "radar-data/v1"
title = "test catalogue"
default_environment = "default"
"""


def quote(part: str) -> str:
    """One argument of a catalogue command line, quoted when it holds whitespace."""
    return f'"{part}"' if any(c.isspace() for c in part) else part


def cmdline(*parts: object) -> str:
    return " ".join(quote(str(p)) for p in parts)


def toml_value(v: object) -> str:
    """A TOML value for a str/bool/list. JSON's string escapes are TOML's for ASCII."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(v)


def entry(
    name: str,
    *,
    ring: str = "adopt",
    artifact: str = "cli",
    install: dict | None = None,
    top: dict | None = None,
    tables: str = "",
) -> str:
    """A tool entry that passes the schema: required fields, one dated history block.
    `top` adds or replaces top-level keys."""
    fields: dict[str, object] = {
        "name": name,
        "repo": f"https://example.invalid/{name}",
        "axis": "lint-format",
        "ring": ring,
        "license": "MIT",
        "visibility": "public",
        "note": "a synthetic entry",
        "artifact": artifact,
        **(top or {}),
    }
    lines = [f"{k} = {toml_value(v)}" for k, v in fields.items()]
    if install is not None:
        lines += ["", "[install]"] + [f"{k} = {toml_value(v)}" for k, v in install.items()]
    lines += [
        "",
        "[[history]]",
        'date = "2026-01-15"',
        f"ring = {toml_value(ring)}",
        'evidence = "a synthetic fixture"',
        'reason = "a synthetic fixture"',
    ]
    return "\n".join(lines) + "\n" + (("\n" + tables) if tables else "")


def probe_script(path: Path, marker: Path, *, output: str = "widget 9.9.9", code: int = 0) -> str:
    """A probe that leaves `marker` behind when it runs. Returns its command line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({marker.as_posix()!r}).write_text('ran', encoding='utf-8')\n"
        f"print({output!r})\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    return cmdline(PY, path.as_posix())


def fake_program(directory: Path, name: str, script: str) -> Path:
    """An executable called `name` in `directory` that runs `script` with this Python.

    A `.cmd` on Windows, a shell script elsewhere - the forms a real PATH lookup finds.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / f"{name}_impl.py"
    body.write_text(script, encoding="utf-8")
    if sys.platform == "win32":
        launcher = directory / f"{name}.cmd"
        launcher.write_text(f'@"{PY}" "{body.as_posix()}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / name
        launcher.write_text(f'#!/bin/sh\nexec "{PY}" "{body.as_posix()}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def hermetic_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ}
    env.pop("RADAR_DATA_ROOT", None)
    env.pop("VIRTUAL_ENV", None)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["UV_TOOL_DIR"] = str(home / "uv-tools")
    gitconfig = home / ".gitconfig-empty"
    home.mkdir(parents=True, exist_ok=True)
    if not gitconfig.exists():
        gitconfig.write_text("", encoding="utf-8")
    env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ENGINE_SRC), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
    )
    env.update(extra or {})
    return env


def git(cwd: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "git",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=hermetic_env(home),
    )


def engine_eval(code: str, home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a snippet of Python against the engine under test, in a subprocess."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=hermetic_env(home),
    )


class Catalogue:
    """A miniature catalogue: marker, tools/, one profile named `default`, a fake home."""

    def __init__(self, base: Path, *, under_git: bool = True) -> None:
        self.base = base
        self.root = base / "catalogue"
        self.home = base / "home"
        self.claude_home = self.home / ".claude"
        self.markers = base / "markers"
        for d in (self.root / "tools", self.root / "environments", self.claude_home, self.markers):
            d.mkdir(parents=True, exist_ok=True)
        (self.root / "radar.toml").write_text(MARKER, encoding="utf-8")
        self.profile()
        if under_git:
            git(self.root, self.home, "init", "-q")

    def profile(self, body: str | None = None, *, name: str = "default") -> None:
        """Write environments/<name>.toml. The default wants adopt and own, and declares
        the fake claude_home and no [flags]."""
        if body is None:
            body = (
                f'name = "{name}"\nrings = ["own", "adopt", "pilot"]\n\n[paths]\n'
                f'claude_home = "{self.claude_home.as_posix()}"\n'
            )
        (self.root / "environments" / f"{name}.toml").write_text(body, encoding="utf-8")

    def tool(self, name: str, text: str, *, file: str | None = None) -> None:
        (self.root / "tools" / f"{file or name}.toml").write_text(text, encoding="utf-8")

    def commit(self) -> None:
        git(self.root, self.home, "add", "-A")
        git(self.root, self.home, "commit", "-q", "-m", "catalogue")

    def run(
        self, module: str, *args: str, cwd: Path | None = None, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run_engine(module, args, cwd=cwd or self.root, env=hermetic_env(self.home, extra))

    def store(self) -> Path:
        common = git(self.root, self.home, "rev-parse", "--git-common-dir").stdout.strip()
        path = Path(common)
        if not path.is_absolute():
            path = self.root / path
        return path.resolve() / "stack-radar" / "trusted-commands.json"
