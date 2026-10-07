"""The programs the engine runs of its own accord are found on PATH and run by absolute path.

The engine runs git, gh, uv and a few others by name. Handed a bare name, CreateProcess on
Windows looks in the working directory before PATH, and so does `shutil.which`; on POSIX,
execvp follows a relative PATH entry such as `.`. The engine usually runs from inside the
catalogue, so a `git.exe` or a `git` committed there would run with the operator's
account the next time anybody ran `radar gate`. These tests plant such a program in the
data root, run the verbs from there, and require that it never runs.

Two layers. The first replaces `subprocess.run` in an engine process and records the
program each call names: every one must be an absolute path taken from PATH. The second
runs the verbs for real against a planted program that leaves a marker behind: a
launcher on Windows, where only a real `.exe` shows the working-directory search, and a
shell script on POSIX, found through a `.` on PATH.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from engine_run import Catalogue, entry, fake_program, hermetic_env, run_engine

WINDOWS = sys.platform == "win32"
# Set, it stops CreateProcess and `shutil.which` searching the working directory. It is
# not set by default, so the tests run without it, as an operator's machine does.
NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"


def env_for(cat: Catalogue, path: str) -> dict[str, str]:
    env = hermetic_env(cat.home, {"PATH": path})
    # Windows spells os.environ's keys in upper case.
    return {k: v for k, v in env.items() if k.upper() != NO_CWD_SEARCH.upper()}


def radar(cat: Catalogue, path: str, verb: str, *args: str) -> subprocess.CompletedProcess[str]:
    """A verb run from the data root, with `path` as PATH."""
    return run_engine(verb, args, cwd=cat.root, env=env_for(cat, path))


# ------------------------------------------------------------ what each call names


RECORD = """
import subprocess
from pathlib import Path

seen = []


def fake(argv, *args, **kwargs):
    seen.append(str(argv[0]))
    return subprocess.CompletedProcess(argv, 0, "", "")


subprocess.run = fake
from stack_radar import bootstrap, changelog_gate, gate, redact_backfill, snapshot

root = Path.cwd()
gate.git_ls_files(root)
snapshot.gh_repo("example/widget")
bootstrap.run(["git", "--version"])
bootstrap.run(["uv", "--version"])
changelog_gate._git("--version")
redact_backfill.tracked(root)
for program in seen:
    print("RAN", program)
"""


def test_every_program_is_run_by_the_absolute_path_found_on_path(tmp_path: Path):
    cat = Catalogue(tmp_path)
    fake_bin = tmp_path / "bin"
    for name in ("git", "gh", "uv"):
        fake_program(fake_bin, name, "")
        # The same names planted in the data root, which is the working directory.
        for planted in (name, f"{name}.exe", f"{name}.bat", f"{name}.cmd"):
            (cat.root / planted).write_text("", encoding="utf-8")
            (cat.root / planted).chmod(0o755)
    path = os.pathsep.join([".", "", str(fake_bin)])
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", RECORD],
        cwd=cat.root,
        capture_output=True,
        text=True,
        timeout=120,
        env=env_for(cat, path),
    )
    ran = [line.removeprefix("RAN ") for line in done.stdout.splitlines() if line.startswith("RAN")]
    assert len(ran) == 6, done.stdout + done.stderr
    for program in ran:
        assert os.path.isabs(program), f"{program} was run by a name, not a path: {ran}"
        assert Path(program).parent == fake_bin, f"{program} is not the one on PATH: {ran}"


# ------------------------------------------------------------ a real planted program


def plant(directory: Path, name: str, marker: Path) -> None:
    """A program called `name` in `directory` that writes `marker` when it runs.

    On Windows it is a real `.exe`: the console launcher pip puts in front of a script,
    taken from the pip wheel the standard library bundles, with a small zipped program
    appended. A `.bat` or `.cmd` would not show the working-directory search, because
    CreateProcess appends only `.exe` to a bare name.
    """
    program = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
    if not WINDOWS:
        script = directory / name
        script.write_text(f'#!/bin/sh\necho ran > "{marker}"\n', encoding="utf-8")
        script.chmod(0o755)
        return
    import ensurepip

    wheels = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    if not wheels:
        pytest.skip("this Python bundles no pip wheel to take a console launcher from")
    with zipfile.ZipFile(wheels[-1]) as wheel:
        try:
            launcher = wheel.read("pip/_vendor/distlib/t64.exe")
        except KeyError:
            pytest.skip("the bundled pip wheel carries no console launcher")
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("__main__.py", program)
    shebang = f'#!"{sys.executable}"\r\n'.encode()
    (directory / f"{name}.exe").write_bytes(launcher + shebang + body.getvalue())


def real_path(fake_bin: Path | None = None) -> str:
    """This machine's PATH, after `fake_bin` when given, and on POSIX after `.`, which is
    how a program planted in the working directory is reached there."""
    parts = [os.environ.get("PATH", "")]
    if fake_bin is not None:
        parts.insert(0, str(fake_bin))
    if not WINDOWS:
        parts.insert(0, ".")
    return os.pathsep.join(parts)


def test_the_gate_never_runs_a_git_planted_in_the_data_root(tmp_path: Path):
    cat = Catalogue(tmp_path)
    # A machine-local entry, so the gate asks git which files the catalogue tracks.
    (cat.root / "tools.local").mkdir()
    (cat.root / "tools.local" / "secret.toml").write_text(entry("secret"), encoding="utf-8")
    marker = cat.markers / "planted-git-ran"
    plant(cat.root, "git", marker)
    done = radar(cat, real_path(), "gate")
    assert not marker.exists(), "radar gate ran the git planted in the data root"
    # The real git answered: the scan read the tracked files.
    assert "scope leak" in done.stdout, done.stdout + done.stderr


def test_bootstrap_never_runs_a_git_planted_in_the_data_root(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "planted-git-ran"
    plant(cat.root, "git", marker)
    radar(cat, real_path(), "bootstrap", "--env", "default")
    assert not marker.exists(), "radar bootstrap ran the git planted in the data root"


def test_snapshot_never_runs_a_gh_planted_in_the_data_root(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.tool(
        "widget",
        entry("widget", ring="observe", top={"repo": "https://github.com/example/widget"}),
    )
    marker = cat.markers / "planted-gh-ran"
    plant(cat.root, "gh", marker)
    # The gh on PATH answers without the network.
    fake_bin = tmp_path / "bin"
    fake_program(fake_bin, "gh", 'print(\'{"stars": 1, "license": "MIT"}\')\n')
    radar(cat, real_path(fake_bin), "snapshot")
    assert not marker.exists(), "radar snapshot ran the gh planted in the data root"


# ------------------------------------------------------------ a git that is a batch file
#
# A `git.cmd` on PATH ahead of any `git.exe` - a wrapper some installs put there - is run
# by cmd.exe, which reads `&` in an argument as its own syntax. The gate hands git a
# directory a catalogue entry names, and `&` and `,` are legal in a Windows directory name.


BATCH_GIT = """
from pathlib import Path
from stack_radar import gate

print("ENUMERATED", gate.git_ls_files(Path({target!r})))
"""


@pytest.mark.skipif(not WINDOWS, reason="only Windows runs a batch file through cmd.exe")
def test_a_git_that_is_a_batch_file_is_never_handed_cmd_syntax(tmp_path: Path):
    cat = Catalogue(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
    work = tmp_path / "work"
    target = work / "a&mkdir,INJECTED&b"
    (target / ".git").mkdir(parents=True)
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", BATCH_GIT.format(target=str(target))],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
        env=env_for(cat, str(shim)),
    )
    assert not (work / "INJECTED").exists(), "cmd.exe read an argument as a command"
    assert "ENUMERATED None" in done.stdout, done.stdout + done.stderr
