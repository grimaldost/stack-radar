"""`versions.run` must find the GLOBAL tool, split a command the way `radar apply` does,
and never take a program from the working directory.

Under `uv run`, the interpreter that runs this suite has `VIRTUAL_ENV` set to the
project's own `.venv`, and that venv's `Scripts` (Windows) or `bin` (POSIX) directory
sits ahead of the rest of PATH - `uv run` puts it there so the project's pinned tools are
what the shell finds. A probe resolved against that PATH finds a tool the project pins as
a dev dependency (`ruff`, here) ahead of the operator's globally installed copy, and
compares the PROJECT'S pinned version against upstream while the global install was never
read at all. Reproduced with two fake executables rather than the real `ruff`, so the test
depends neither on what this project pins nor on `ruff` existing on the machine.

The splitting and the lookup are the approval's business as much as the probe's: an
approval covers a command text, so the argv that runs has to be the one `radar apply`
and the approval derive from that text (POSIX quoting, where `str.split` would cut a
quoted argument at its space), and the program has to be the one PATH names - on Windows
a lookup that searches the working directory first would run a file planted in the
catalogue instead. Those two run in a subprocess against `RADAR_ENGINE_SRC`, so they can
be pointed at an earlier engine.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

from engine_run import engine_eval, fake_program

from stack_radar import versions

VENV_BIN_NAME = "Scripts" if sys.platform == "win32" else "bin"


def _make_tool(directory: Path, name: str, version_line: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (directory / f"{name}.cmd").write_text(
            f"@echo off\r\necho {version_line}\r\n", encoding="utf-8"
        )
    else:
        script = directory / name
        script.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
        script.chmod(0o755)


def test_run_ignores_the_running_project_s_virtualenv(tmp_path, monkeypatch):
    venv = tmp_path / "project" / ".venv"
    venv_bin = venv / VENV_BIN_NAME
    global_dir = tmp_path / "global-tools"

    # The project-pinned copy sits AHEAD on PATH, the way `uv run` arranges it - and it
    # is the OLDER version, so a probe that reads it reports "behind" for a tool that is
    # actually current.
    _make_tool(venv_bin, "ruff", "ruff 0.1.0")
    _make_tool(global_dir, "ruff", "ruff 99.0.0")

    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), str(global_dir)]))

    out = versions.run("ruff --version")

    assert out is not None, "the probe found no `ruff` at all"
    assert "99.0.0" in out, f"expected the global tool's version, got: {out!r}"
    assert "0.1.0" not in out, f"the probe read the shadowing venv copy instead: {out!r}"


def test_run_still_finds_a_tool_with_no_virtualenv_active(tmp_path, monkeypatch):
    """The common case - no `VIRTUAL_ENV` at all - must keep working."""
    global_dir = tmp_path / "global-tools"
    _make_tool(global_dir, "ruff", "ruff 1.2.3")

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", str(global_dir))

    out = versions.run("ruff --version")

    assert out is not None
    assert "1.2.3" in out


def _run_in_engine(tmp_path: Path, cmd: str, path: Path, cwd: Path) -> str:
    """`versions.run(cmd)` in a subprocess against the engine under test."""
    code = (
        "import os, sys\n"
        # Windows' default: a bare name is looked up in the working directory first. A
        # shell may switch that off with this variable and the operator's may not, so the
        # test runs with the default.
        "os.environ.pop('NoDefaultCurrentDirectoryInExePath', None)\n"
        f"os.environ['PATH'] = {str(path)!r}\n"
        "from stack_radar import versions\n"
        f"sys.stdout.write(repr(versions.run({cmd!r})))\n"
    )
    done = engine_eval(code, tmp_path / "home", cwd)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_run_keeps_a_quoted_argument_whole(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    fake_program(bin_dir, "widget", "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    out = _run_in_engine(tmp_path, 'widget --name "a b"', bin_dir, tmp_path)
    printed = ast.literal_eval(out)  # the repr of what run() returned
    assert printed is not None, out
    assert json.loads(printed.strip()) == ["--name", "a b"], printed


def test_run_never_takes_a_program_from_the_working_directory(tmp_path: Path):
    here = tmp_path / "catalogue"
    fake_program(here, "widget", "print('planted 6.6.6')\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    out = _run_in_engine(tmp_path, "widget --version", empty, here)
    assert "6.6.6" not in out, f"a program in the working directory ran: {out}"
    assert out == "None", out


def test_run_neither_runs_in_the_data_root_nor_imports_from_it(tmp_path: Path):
    # A version probe is catalogue text: it runs in an empty scratch directory, with a
    # path relative to the data root passed as the absolute path the approval hashed.
    root = tmp_path / "catalogue"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "probe.py").write_text(
        "import os\nprint('widget 1.2.3', os.getcwd())\n", encoding="utf-8"
    )
    (root / "json.py").write_text("raise SystemExit('planted json imported')\n", "utf-8")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from stack_radar import versions\n"
        f"root = Path({root.as_posix()!r})\n"
        f"py = {Path(sys.executable).as_posix()!r}\n"
        "out = versions.run(py + ' scripts/probe.py', root)\n"
        "shadow = versions.run(py + ' -c \"import json; print(json.dumps(1))\"', root)\n"
        "sys.stdout.write(repr((out, shadow)))\n"
    )
    done = engine_eval(code, tmp_path / "home", tmp_path)
    assert done.returncode == 0, done.stderr
    out, shadow = ast.literal_eval(done.stdout)
    assert out is not None and "widget 1.2.3" in out, out
    ran_in = Path(out.split("widget 1.2.3", 1)[1].strip())
    assert not ran_in.resolve().is_relative_to(root.resolve()), ran_in
    assert shadow is not None and "planted" not in shadow, shadow
