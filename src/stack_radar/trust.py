"""Commands the catalogue asks the engine to run, and the operator's approval of each.

WHY THIS EXISTS
---------------
A catalogue is a git repository that takes pull requests, and some of its fields are
commands: `[install].check` and `[install].apply` in a tool entry, and
`[feedback].index_builder`, a script the engine runs with its own interpreter. Whoever
edits one of those lines writes code that runs, with the operator's account, the next time
the operator runs `radar apply`, `radar versions`, `radar reconcile` or
`radar sync-feedback` - which are the commands a reviewer reaches for first. The gate
cannot tell a probe from a payload, and a diff shows a probe line like any other line.

So a catalogue command runs only after the operator approved that exact command. An
approval covers:

- the entry's name, the field, and the command text exactly as it will run - after the
  profile's `{placeholders}` are filled, because a profile is catalogue data too;
- the content of every file and directory the command's arguments name inside the
  repository the data root sits in (the data root itself when it is not in git), so a
  pull request that edits a script an approved command runs makes it pending again - an
  argument such as `../scripts/probe.js`, from a data root in a subdirectory of the
  repository, included;
- the whole tree of the directory that holds each named file, because a program loads
  files from beside its script without being told to (`require('./helper')`, `source
  "$(dirname "$0")/lib.sh"`); a named file directly in the data root, or in a directory
  that holds the data root, is covered alone, since its directory is the whole catalogue;
- every symbolic link inside the repository on the way to a named path or inside a
  hashed tree, by its target text; a link that resolves inside the repository is
  followed and what it leads to is hashed too, once, so a link cycle ends, and one that
  resolves outside contributes its target text alone. The way to a path is walked as the
  system resolves it, one link at a time, so a link that a later `..` steps back out of
  (`lnk/../probe.py`) is on the way, and so is a link reached only through another
  link's target (`lnk1` leading to `lnk2`, which leads outside).

An index builder is a path rather than a command line. Its approval covers the path and
the content hash of the file, wherever the file lives, and not the files beside it: the
builder usually lives in another repository, whose every change would otherwise ask for a
new approval, and PYTHONSAFEPATH keeps its directory off its import path. A file it loads
by a path it computes is not covered.

Without `--trust-commands` a pending command is not run: it is printed as `[UNTRUSTED]`
with a short id, the thing it would have observed is reported as unknown, the listing is
recorded, and the command exits non-zero. The listing records the whole sha256 of what
the approval covers; the id is only its first twelve characters, printed for reading,
and short enough that whoever writes two commands can find a pair that shares it. With
the flag, a pending command is approved, recorded and run only when an earlier run of the
same verb listed the same whole digest - the same command text and the same content of
everything above. One that no run of that verb has listed, or that changed since it was
listed, is listed now and left for the next run, so the flag approves what the operator
was shown and nothing that arrived after.

Each run is the operator's latest look at what its verb would run, so it replaces that
verb's listing, with the flag or without it: a command an earlier run of the same verb
listed and this run did not show is dropped, and the flag no longer approves it. A branch
reviewed and then dropped leaves nothing approvable behind once the verb runs again on the
main line. A run that asked about no command at all - `radar sync-feedback --check` never
looks at the builder - leaves the listing as it was. Each verb keeps its own listing and
approves only from it: `radar apply` does not drop what `radar versions` listed, and a
command only `radar versions` still lists is not approved by `radar apply --trust-commands`.

WHERE APPROVALS LIVE
--------------------
In `<git common dir>/stack-radar/trusted-commands.json`: inside the catalogue's git
directory, which git never tracks, so an approval cannot arrive with a pull request. The
common directory is shared by every worktree of the repository, so a branch checked out
in a second worktree for review reads the approvals the main checkout recorded, and the
reverse. It is found by reading `.git` - a directory, or a file holding `gitdir:` plus the
`commondir` file a worktree keeps - without running git, so deciding what may run runs
nothing itself.

A data root that is not inside a git repository has nowhere to keep approvals. Nothing
there is approved and nothing runs, and the command says where approvals would live.

WHERE A COMMAND RUNS
--------------------
Not in the data root. An approval hashes the files a command's arguments name, but a
program also reads files it was never told about, from its working directory: `python -m
<module>` and `python -c "import json"` import from it, `npx` prefers its
`node_modules/.bin`, `uv run` reads its `pyproject.toml`. Were the data root the working
directory, a file a pull request adds or edits there would change what an approved
command does while the approval still matched. So each catalogue command runs in a fresh
empty directory (`scratch_dir`), and the files it names are passed to it as absolute
paths (`catalogue_argv`) - the same files the approval hashes. `PYTHONSAFEPATH` is set
too (`command_env`), so Python adds neither the working directory nor a script's own
directory to its import path, and so is `PYTHONDONTWRITEBYTECODE`: a script that imports a
helper by putting its own directory on the path would otherwise write `__pycache__` into
the tree its approval hashes.

The program itself is looked up on PATH and never in the working directory (see
`find_executable`), so a file added to the data root cannot stand in for the program an
approved command names.

No shell runs a command, with one exception the engine cannot remove: on Windows a program
that is a `.bat` or `.cmd` file (npm and npx are; the name is read as Windows reads it,
without trailing dots or spaces) is run by cmd.exe, which reads `&`, `|`, `%` and the like
inside an argument, or inside the program's own path, as its own syntax. Such a command
runs only when neither holds any; otherwise it is refused with a FAIL, approved or not
(`batch_refusal`).
WHAT AN APPROVAL DOES NOT COVER
-------------------------------
Files a program reads on its own account from elsewhere - its own configuration in the
operator's home, a module installed in its environment, anything outside the repository.
Those belong to the machine, not to the catalogue, and are the operator's to keep.
Inside the repository, what a program finds by walking up from its script (a
`node_modules` in an ancestor directory), a path it computes at run time, a path written
inside a longer argument (a `sh -c` string, `-c` code) rather than as an argument of its
own, and the files beside a script that sits directly in the data root are not covered
either. The listing points out such a script, and an argument that holds both a space
and a path separator. A script that loads helpers belongs in a directory of its own,
which the approval then hashes whole, and is named by an argument of its own.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any

from .paths import is_link

FLAG = "--trust-commands"

STORE_DIR = "stack-radar"
STORE_FILE = "trusted-commands.json"
SCHEMA = "stack-radar/trusted-commands/v1"

# The fields whose values the engine runs. Spelled as the TOML reads, because that is
# where the operator goes to look at what they are approving.
CHECK = "[install].check"
APPLY = "[install].apply"
INDEX_BUILDER = "[feedback].index_builder"

# The suffixes of an approval key that is not a named path: the directory that holds a
# named file, and a symbolic link whose target text is recorded.
DIRECTORY = " (directory)"
LINK = " (link)"


# ------------------------------------------------------------------ command lines


def split_command(cmd: str) -> list[str]:
    """argv for a catalogue command. Raises ValueError on unbalanced quotes.

    One splitter for every command the engine runs, so the argv that runs and the argv
    whose files an approval hashes are the same list. POSIX rules on every platform:
    quotes group an argument that holds spaces, and a backslash escapes the next
    character - which is why catalogue commands spell paths with forward slashes, a form
    Windows accepts. `posix=False` would keep the quote characters inside the arguments,
    and a plain `str.split` would cut a quoted argument at its spaces.
    """
    return shlex.split(cmd)


def find_executable(name: str, path: str | None = None) -> str | None:
    """A bare program name resolved on PATH - never in the working directory.

    `shutil.which` on Windows searches the current directory before PATH, and so does
    CreateProcess for a bare name. The engine usually runs from inside the catalogue (it
    finds the data root by searching upwards), so either would let a `ruff.cmd` added to
    the catalogue stand in for the `ruff` an approved command names. Relative PATH entries
    (`.`, or an empty one) are skipped for the same reason.

    PATHEXT is applied here because CreateProcess does not apply it, and a `.cmd` shim
    (npm, npx, most global npm installs) is otherwise invisible to a subprocess call.
    """
    search = os.environ.get("PATH", "") if path is None else path
    names = [name]
    if sys.platform == "win32":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if Path(name).suffix.upper() not in {e.upper() for e in exts}:
            names = [name + e for e in exts]
    for directory in search.split(os.pathsep):
        if not directory or not os.path.isabs(directory):
            continue
        for candidate in names:
            full = os.path.join(directory, candidate)
            if os.path.isfile(full) and (sys.platform == "win32" or os.access(full, os.X_OK)):
                return full
    return None


def executable(argv0: str, base: Path | None = None, path: str | None = None) -> str | None:
    """argv[0] as the file that will run, or None when there is none.

    A name with a directory part is a file, taken relative to `base` (the data root, for a
    catalogue command) - and it is one of the files an approval hashes. A bare name is
    searched on PATH only (`find_executable`).
    """
    if os.path.dirname(argv0) or os.path.isabs(argv0):
        p = Path(argv0)
        if not p.is_absolute():
            p = (base or Path.cwd()) / p
        return str(p) if p.is_file() else None
    return find_executable(argv0, path)


# A program Windows runs through cmd.exe rather than directly. CreateProcess hands a
# `.bat` or `.cmd` file to `cmd.exe /c`, and the quoting `subprocess` applies is the C
# runtime's, which cmd.exe does not read: a `"` inside an argument ends cmd's quoting, and
# `&`, `|`, `<`, `>`, `^`, `%` and `!` are then its own syntax. npm, npx and most global npm
# installs are such files.
BATCH_SUFFIXES = frozenset({".bat", ".cmd"})

# The characters an argument to a batch file may hold besides letters and digits: none of
# them makes cmd.exe run anything, quoted or not.
BATCH_SAFE = frozenset(" -_.,:;/\\@+=~#*?[]{}'$`")

# The batch file's own path is read by cmd.exe too. `subprocess` passes it bare when it
# holds no space or tab, and cmd.exe then ends or splits the name at `,`, `;` and `=` as
# well, running another file. In quotes, only `%` and `!` still mean anything.
PROGRAM_SAFE = BATCH_SAFE - frozenset(",;=")
QUOTED_PROGRAM_UNSAFE = frozenset('%!"')


def is_batch(exe: str, platform: str = sys.platform) -> bool:
    """Whether Windows would run `exe` through cmd.exe: a `.bat` or `.cmd` file there.

    The name is read as Windows reads it, without its trailing dots and spaces: Windows
    opens `p.cmd.` and `p.cmd ` as `p.cmd`, and CreateProcess hands them to cmd.exe too.
    """
    if platform != "win32":
        return False
    name = PureWindowsPath(exe).name.rstrip(" .")
    return PureWindowsPath(name).suffix.lower() in BATCH_SUFFIXES


def _program_syntax(exe: str) -> str:
    """The characters in `exe`, a batch file's path, that cmd.exe would read as syntax."""
    if subprocess.list2cmdline([exe]).startswith('"'):
        return "".join(sorted({c for c in exe if c in QUOTED_PROGRAM_UNSAFE}))
    return "".join(sorted({c for c in exe if not (c.isalnum() or c in PROGRAM_SAFE)}))


def batch_refusal(exe: str, args: Sequence[str], platform: str = sys.platform) -> str | None:
    """Why `exe` may not be run with `args`, or None when it may.

    On Windows, a batch file runs only when cmd.exe would read nothing in it as syntax:
    every argument is made of letters, digits and `BATCH_SAFE`, and so is the program's
    own path, less `,;=` (`PROGRAM_SAFE`) - or, when the path holds a space and is passed
    in quotes, it holds no `%` or `!`. Anything else could be read by cmd.exe as a command
    of its own, so the command is refused rather than escaped: an escaping rule for
    cmd.exe would be one more thing a reviewer has to trust. Elsewhere, and for any other
    program, None.
    """
    if not is_batch(exe, platform):
        return None
    bad = _program_syntax(exe)
    if bad:
        return (
            f"{Path(exe).name} is a batch file, which Windows runs through cmd.exe, and its "
            f"path {Path(exe).as_posix()} holds {bad!r}, which cmd.exe can read as its own "
            "syntax"
        )
    for arg in args:
        bad = "".join(sorted({c for c in arg if not (c.isalnum() or c in BATCH_SAFE)}))
        if bad:
            return (
                f"{Path(exe).name} is a batch file, which Windows runs through cmd.exe, "
                f"and the argument {arg!r} holds {bad!r}, which cmd.exe can read as its "
                "own syntax"
            )
    return None


def run_program(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` for a program the engine itself runs - git, gh, uv and the rest.

    A bare argv[0] is looked up with `find_executable`, on the PATH the program will run
    with (`env`'s, when given), and run by that absolute path. Handed a bare name,
    CreateProcess searches the working directory before PATH, and the engine usually runs
    from inside the catalogue, so a `git.exe` committed there would run in place of git.
    An absolute argv[0] runs as it is.

    A program that is a batch file runs only when `batch_refusal` finds nothing in its
    path or its arguments that cmd.exe would read as syntax - the rule a catalogue command
    is held to. The engine hands these programs paths a catalogue names (`git -C <dir>`,
    the file names `git mv` moves), and `&` is legal in a Windows file name.

    Raises FileNotFoundError, an OSError, when the program is not on PATH, and
    PermissionError, also an OSError, when the batch-file rule refuses it: the errors a
    program that cannot run raises anyway, so a caller's handling stays as it is.
    """
    name = str(argv[0])
    if os.path.isabs(name):
        exe: str | None = name
    elif os.path.dirname(name):
        exe = None
    else:
        env = kwargs.get("env")
        exe = find_executable(name, (env if env is not None else os.environ).get("PATH", ""))
    if exe is None:
        raise FileNotFoundError(errno.ENOENT, f"{name} is not on PATH", name)
    refused = batch_refusal(exe, [str(a) for a in argv[1:]])
    if refused:
        raise PermissionError(errno.EACCES, f"{refused}; not run", exe)
    return subprocess.run([exe, *argv[1:]], **kwargs)  # noqa: S603 - fixed argv, no shell


# The git configuration whose values git runs as a program, set on the command line so
# the repository git operates on cannot supply it. Every git call the engine makes touches
# a tree a catalogue, a profile or the data root chose. A pull request can commit a
# directory laid out as a bare repository, config included (git refuses to track a `.git`
# path component, so a nested `.git` is never committed, though one can exist on disk as
# an untracked clone), and git pointed at either would read that config:
#
# - `safe.bareRepository=explicit` stops git from discovering a directory laid out as a
#   bare repository and adopting its config at all.
# - `core.fsmonitor=` empties the file-system-monitor hook, a program git runs to list
#   changes - the one a read-only command such as `ls-files` reaches when a repository's
#   config names it.
# - `core.hooksPath` points at a path that holds no hooks, so an index-touching subcommand
#   (`mv`) runs none the tree carries.
# - `core.sshCommand=` empties the program an ssh transport would run; the engine's only
#   remote fetch is `ls-remote` over https, so it is never spawned, but it is emptied in
#   case a crafted remote reached it.
#
# A repository's `diff.external` and a diff driver's `textconv` are programs a CONTENT
# diff runs, and they are NOT set here: an empty `diff.external=` is itself spawned, which
# breaks every diff. The only content diff the engine takes is the changelog gate's, and
# it passes `--no-ext-diff --no-textconv` at its call site; `ls-files`, `mv`, `describe`,
# `merge-base`, `log`, `rev-parse` and the name-only diffs run neither.
#
# Not emptied: a clean or process FILTER. `git mv` can run `filter.<driver>.clean` or
# `.process` for a file whose `.gitattributes` selects that driver. The attributes can come
# from the tree, but the program comes only from git configuration (the repository's own,
# the user's or the system's), which a catalogue cannot carry: a committed bare layout is
# never adopted, and a checkout's own `.git/config` belongs to whoever cloned it. Driver
# names are arbitrary, so there is no single key to empty.
GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    "safe.bareRepository=explicit",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=" + os.devnull,
    "-c",
    "core.sshCommand=",
)


def git(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`run_program` for a git subcommand, with `GIT_SAFE_CONFIG` inserted.

    How the engine runs git on a tree, so the configuration a repository could turn into a
    command is disabled on every call and not on the ones a reviewer remembered. The one
    other runner, `versions._exec`, runs git in a scratch directory against the global
    probe environment and inserts the same `GIT_SAFE_CONFIG` itself. `argv[0]` is `git`;
    the safe `-c` options go before the subcommand, where the repository's own config
    cannot override them, and the caller's arguments follow.
    """
    if not argv or str(argv[0]) != "git":
        raise ValueError("trust.git runs git: argv[0] must be 'git'")
    return run_program(["git", *GIT_SAFE_CONFIG, *argv[1:]], **kwargs)


def _anchored(piece: str, root: Path) -> str:
    """`piece` as an absolute path when it names something in the data root, else as is.

    Only an argument that is plainly a path is rewritten: one with a directory part (or
    `.` / `..`) that exists under the data root, or a bare name that is a FILE there
    (`probe.py`). A bare name that is only a directory there (`tools`) is left alone,
    because it is far more often a package or subcommand name than a path.
    """
    if not piece or piece.startswith("-") or os.path.isabs(piece):
        return piece
    target = root / piece
    has_dir = bool(os.path.dirname(piece)) or piece in (".", "..")
    try:
        if target.is_file() or (has_dir and target.exists()):
            return str(target)
    except OSError:
        pass
    return piece


def catalogue_argv(cmd: str, root: Path) -> list[str]:
    """argv for a catalogue command as it runs: split, with data-root paths made absolute.

    The command runs outside the data root (`scratch_dir`), so a path it names relative
    to the data root is passed as an absolute path - the file the approval hashed. That
    holds for `--option=path` too. argv[0] is rewritten only when it has a directory
    part; a bare program name stays bare and is found on PATH. Raises ValueError on
    unbalanced quotes.
    """
    out: list[str] = []
    for i, arg in enumerate(split_command(cmd)):
        if i == 0 and not os.path.dirname(arg):
            out.append(arg)
        elif arg.startswith("-") and "=" in arg:
            key, value = arg.split("=", 1)
            out.append(f"{key}={_anchored(value, root)}")
        else:
            out.append(_anchored(arg, root))
    return out


@contextmanager
def scratch_dir() -> Iterator[Path]:
    """A fresh, empty working directory for one catalogue command, removed afterwards.

    Empty, so a program that looks in its working directory for modules, binaries or
    configuration finds nothing that a pull request could have put there.
    """
    with tempfile.TemporaryDirectory(prefix="radar-cmd-", ignore_cleanup_errors=True) as d:
        yield Path(d)


def command_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a catalogue command runs with: `base` (default: this process's),
    with PYTHONSAFEPATH set so Python adds neither the working directory nor a script's
    own directory to its import path, and PYTHONDONTWRITEBYTECODE set so a script that
    imports a helper from its own directory writes no `__pycache__` into the tree its
    approval hashes, which would make the approval lapse after the first run."""
    env = dict(os.environ if base is None else base)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def inside(child: Path, parent: Path) -> bool:
    """Whether `child`, resolved, is `parent` or lies under it, resolved.

    Resolved on both sides, so `..` segments and symbolic links are judged by where they
    land rather than by how they are spelled.
    """
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, RuntimeError):
        return False


def strictly_inside(child: Path, parent: Path) -> bool:
    """Whether `child`, resolved, lies under `parent` and is not `parent` itself.

    For a directory named after catalogue data - a tool's inbox or archive - where a name
    such as `.` would otherwise make the directory the root that holds every tool's.
    """
    try:
        return inside(child, parent) and child.resolve() != parent.resolve()
    except (OSError, RuntimeError):
        return False


# --------------------------------------------------------------------- git layout


def _git_dot(start: Path) -> tuple[Path, Path] | None:
    """(the directory holding the nearest `.git`, that `.git`) at or above `start`."""
    start = Path(start).resolve()
    for cand in (start, *start.parents):
        dot = cand / ".git"
        if dot.is_dir() or dot.is_file():
            return cand, dot
    return None


def git_work_tree(start: Path) -> Path | None:
    """The top of the git work tree `start` sits in - the directory that holds `.git`,
    for a main checkout and a linked worktree alike - or None outside git."""
    found = _git_dot(start)
    return found[0] if found else None


def git_common_dir(start: Path) -> Path | None:
    """The git common directory of the repository `start` sits in, or None.

    Read from disk rather than asked of git. `.git` is either the directory itself or,
    in a linked worktree (and a submodule), a file whose `gitdir:` line names the
    worktree's private directory. That directory holds a `commondir` file naming the
    directory every worktree shares, which is where refs, config and these approvals
    live.
    """
    found = _git_dot(start)
    if found is None:
        return None
    cand, dot = found
    if dot.is_dir():
        gitdir = dot
    else:
        try:
            text = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = cand / gitdir
    common = gitdir
    pointer = gitdir / "commondir"
    if pointer.is_file():
        try:
            named = Path(pointer.read_text(encoding="utf-8").strip())
        except OSError:
            return None
        common = named if named.is_absolute() else gitdir / named
    return common.resolve() if common.is_dir() else None


# ----------------------------------------------------------------------- hashing


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# More links than any system follows while resolving one path, so a walk that stops here
# stops where the system would already have refused the path.
MAX_LINK_HOPS = 128


def _walkable(target: str) -> Path:
    """A link's target text as a path a walk can go on from. Windows spells a junction's
    target with the `\\\\?\\` prefix, which says nothing about where it leads."""
    if os.name == "nt":
        if target.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + target[len("\\\\?\\UNC\\") :])
        if target.startswith("\\\\?\\"):
            return Path(target[len("\\\\?\\") :])
    return Path(target)


def _resolution(spelled: Path, *, lexical: bool) -> Iterator[tuple[Path, str | None]]:
    """Each path the system looks at while it resolves `spelled`, with its target text
    when it is a symbolic link (or junction) and None when it is not.

    Links are followed one at a time: the walk goes on from the link's directory through
    its target text, component by component, so a link reached only through another
    link's target is met too. `resolve()` follows a whole chain in one step and shows none
    of the links inside it. With `lexical`, `..` is collapsed before each walk, as Windows
    does; otherwise `..` steps up from where the walk has got to, as a POSIX kernel does,
    so `lnk/..` is the parent of the link's target. The walk ends after MAX_LINK_HOPS
    links.
    """

    def start(path: Path) -> tuple[Path, list[str]]:
        if lexical:
            path = Path(os.path.normpath(path))
        return Path(path.anchor), list(path.parts[1:] if path.anchor else path.parts)

    here, todo = start(spelled)
    hops = 0
    while todo:
        part = todo.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            here = here.parent
            continue
        here = here / part
        if not is_link(here):
            yield here, None
            continue
        try:
            target = os.readlink(here)
        except (OSError, ValueError):
            yield here, "?"
            return
        yield here, target
        hops += 1
        if hops > MAX_LINK_HOPS:
            return
        here, todo = start(here.parent.joinpath(_walkable(target), *todo))


def _links_on_the_way(spelled: Path, scope: Path) -> dict[Path, str]:
    """The symbolic links inside `scope` that resolving `spelled` passes through, each
    keyed by where it sits (its directory resolved) and valued by its target text.

    The path is walked twice (`_resolution`): as a POSIX kernel resolves it and as
    Windows does. A link either walk meets is kept; one that only the other platform's
    walk meets makes an approval stricter, never looser. A link outside `scope` belongs to
    this machine and is left out, but the walk still follows it, so a link inside `scope`
    that it leads back to is kept.
    """
    out: dict[Path, str] = {}
    for lexical in (False, True):
        for here, target in _resolution(spelled, lexical=lexical):
            if target is None:
                continue
            try:
                holder = here.parent.resolve()
            except (OSError, RuntimeError):
                continue
            if holder.is_relative_to(scope):
                out[holder / here.name] = target
    return out


def _tree_sha256(directory: Path, scope: Path) -> str:
    """One digest over everything under `directory`: each entry's relative path, and a
    file's content or a link's target text, in sorted order.

    A command that names a directory reads what is in it, so a change to any file there
    changes what the command does. `.git` is skipped - it is not content a command runs.

    A symbolic link (or junction) is recorded by its target text, so adding or
    retargeting one changes the digest, and so is every link inside `scope` that it
    leads through on its way (`_links_on_the_way`): a link to a link can be retargeted
    at either end. Programs that walk a tree follow such links, so one that resolves
    inside `scope` - the repository, where a pull request can change what it leads to -
    is followed and its content hashed as well; each directory is walked once, so a cycle
    of links ends. One that resolves outside `scope` leads to this machine's own files
    and contributes the target texts alone.
    """
    h = hashlib.sha256()
    seen: set[Path] = set()

    def walk(here: Path, rel: str) -> None:
        try:
            real = here.resolve()
        except (OSError, RuntimeError):
            h.update(f"{rel}\0unresolvable\n".encode())
            return
        if real in seen:
            h.update(f"{rel}\0walked\n".encode())
            return
        seen.add(real)
        try:
            entries = sorted(os.scandir(here), key=lambda e: e.name)
        except OSError:
            h.update(f"{rel}\0unreadable\n".encode())
            return
        for e in entries:
            if e.name == ".git":
                continue
            path = Path(e.path)
            name = f"{rel}{e.name}"
            if is_link(path):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = "?"
                h.update(f"{name}\0link\0{target}\n".encode())
                for link, text in sorted(_links_on_the_way(path, scope).items()):
                    via = Path(os.path.relpath(link, scope)).as_posix()
                    h.update(f"{name}\0via\0{via}\0{text}\n".encode())
                try:
                    led = path.resolve()
                    if not led.is_relative_to(scope):
                        continue
                    if led.is_dir():
                        walk(path, name + "/")
                    elif led.is_file():
                        h.update(f"{name}\0{_sha256(led)}\n".encode())
                except (OSError, RuntimeError):
                    continue
            elif e.is_dir(follow_symlinks=False):
                walk(path, name + "/")
            elif e.is_file(follow_symlinks=False):
                try:
                    h.update(f"{name}\0{_sha256(path)}\n".encode())
                except OSError:
                    h.update(f"{name}\0unreadable\n".encode())

    walk(directory, "")
    return h.hexdigest()


# ------------------------------------------------------------------- approvals


@dataclass(frozen=True)
class Command:
    """One thing the catalogue asks to run, with the file hashes its approval covers."""

    tool: str
    field: str
    text: str
    # (path, sha256), sorted. A path is written relative to the data root, `../` and all
    # when it lies elsewhere in the repository; a key with a suffix in parentheses is a
    # directory of a named file (`DIRECTORY`) or a link's target text (`LINK`).
    files: tuple[tuple[str, str], ...] = ()
    # What the operator is told about the paths the approval does not cover. Printed with
    # the command, never compared: it follows from the text and the files above.
    notes: tuple[str, ...] = dataclass_field(default=(), compare=False)
    # Why this machine will not run the command at all, approved or not (`batch_refusal`);
    # empty when it may. Never compared: it follows from the text and this machine's PATH.
    refusal: str = dataclass_field(default="", compare=False)

    @property
    def digest(self) -> str:
        """The sha256 of everything the approval covers: what a listing records and
        `--trust-commands` holds the approving run to, whole."""
        body = json.dumps(
            [self.tool, self.field, self.text, sorted(self.files)], separators=(",", ":")
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def id(self) -> str:
        """The first characters of `digest`, printed for the operator to read. Never
        compared: whoever writes two commands can find a pair that shares it."""
        return self.digest[:12]

    def record(self) -> dict:
        return {
            "tool": self.tool,
            "field": self.field,
            "command": self.text,
            "files": dict(self.files),
            "approved_on": date.today().isoformat(),
        }

    def matches(self, rec: dict) -> bool:
        return (
            rec.get("tool") == self.tool
            and rec.get("field") == self.field
            and rec.get("command") == self.text
            and rec.get("files") == dict(self.files)
        )


def _holds_a_path(arg: str) -> bool:
    """Whether `arg` looks like program text that may name a file: several words and a
    path separator. A path written that way is never hashed."""
    return any(c.isspace() for c in arg) and ("/" in arg or "\\" in arg)


def _is_digest(key: object) -> bool:
    """Whether `key` is a whole sha256 hexdigest, as `Command.digest` spells it."""
    return isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key)


class Trust:
    """The approval gate every catalogue command passes through.

    `approve` is `--trust-commands`: approve what this invocation would run, when an
    earlier run listed it as it stands now. `skipped` counts the commands refused, and
    the caller turns a non-zero count into a non-zero exit, so a run that observed less
    than it was asked to never reads as a clean one.

    `path` is the PATH the verb's runner looks a program up on, when it is not this
    process's own (`radar versions` drops the running project's virtualenv from it), so
    that the program the listing and the batch-file rule judge is the one that would run.
    """

    def __init__(
        self, root: Path, *, approve: bool = False, verb: str = "", path: str | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.approve = approve
        self.path = path
        # The verb this run belongs to: the run replaces that verb's listing with what it
        # showed (`closing`).
        self.verb = verb
        self._consulted = False
        self._shown: set[str] = set()
        # What a pull request to this catalogue can change: the repository's work tree,
        # or the data root alone outside git. A named path is hashed when it lies here.
        self.scope = git_work_tree(self.root) or self.root
        common = git_common_dir(self.root)
        self.store: Path | None = common / STORE_DIR / STORE_FILE if common else None
        self.skipped = 0
        # The commands this run refused to run at all (`Command.refusal`), each a FAIL.
        self._refused = 0
        self._told_no_store = False
        self._records, self._listed = self._load()
        # One decision per command per run: a builder shared by several targets, or a
        # probe asked for twice, is reported and counted once.
        self._decided: dict[Command, bool] = {}

    # ----------------------------------------------------------------- building
    def _key(self, path: Path) -> str:
        """`path` (resolved) as an approval key: relative to the data root."""
        return Path(os.path.relpath(path, self.root)).as_posix()

    def command(self, tool: str, field: str, text: str) -> Command:
        """The approval `text` needs: itself, plus what it names inside the repository
        (see the module docstring for what that covers)."""
        files: dict[str, str] = {}
        notes: list[str] = []
        try:
            argv = split_command(text)
        except ValueError:
            argv = []
        for arg in argv:
            pieces = [arg]
            # `--config=path` names the path too.
            if "=" in arg:
                pieces.append(arg.split("=", 1)[1])
            named = False
            for piece in pieces:
                if not piece:
                    continue
                spelled = Path(piece)
                p = spelled if spelled.is_absolute() else self.root / spelled
                files.update(self._links(p))
                try:
                    if not p.exists():
                        continue
                    named = True
                    real = p.resolve()
                    if not real.is_relative_to(self.scope):
                        if not spelled.is_absolute():
                            notes.append(
                                f"it names {piece}, which lies outside the repository: "
                                "its content is this machine's and not part of the approval"
                            )
                        continue
                    key = self._key(real)
                    if real.is_dir():
                        files[key.rstrip("/") + "/"] = _tree_sha256(real, self.scope)
                        continue
                    if not real.is_file():
                        continue
                    files[key] = _sha256(real)
                    holder = real.parent
                    if self.root.is_relative_to(holder):
                        where = "the data root" if holder == self.root else self._key(holder)
                        notes.append(
                            f"{key} sits directly in {where}, so the files beside it are "
                            "not part of the approval"
                        )
                    else:
                        files[self._key(holder) + "/" + DIRECTORY] = _tree_sha256(
                            holder, self.scope
                        )
                except (OSError, RuntimeError, ValueError):
                    continue
            if not named and _holds_a_path(arg):
                notes.append(
                    f"the argument {arg!r} holds more than a path: a path written inside "
                    "it, as in a `sh -c` string or `-c` code, is not part of the approval - "
                    "only an argument that is a path on its own, or the value after '=', is"
                )
        refusal = ""
        try:
            run_argv = catalogue_argv(text, self.root)
        except ValueError:
            run_argv = []
        exe = executable(run_argv[0], self.root, self.path) if run_argv else None
        if exe is not None:
            refusal = batch_refusal(exe, run_argv[1:]) or ""
            if not refusal and is_batch(exe):
                where = Path(exe).as_posix()
                spelled = argv[0] if argv else where
                program = where if spelled == where else f"{spelled} ({where})"
                notes.append(
                    f"{program} is a batch file, which Windows runs through cmd.exe: the "
                    "engine refuses what cmd.exe reads as syntax in its path and arguments, "
                    "not what the batch file itself does with them"
                )
        return Command(
            tool,
            field,
            text,
            tuple(sorted(files.items())),
            tuple(dict.fromkeys(notes)),
            refusal,
        )

    def _links(self, spelled: Path) -> dict[str, str]:
        """The symbolic links inside the repository that `spelled` passes through, as
        approval keys, each with its target text (`_links_on_the_way`).

        Resolution judges a path by where it lands, so a link that points outside the
        repository contributes no content hash - and retargeting it would change what
        the command runs without changing anything the approval covers. Each such link's
        own target text is therefore part of the approval, and so is that of every link
        reached through it.
        """
        return {
            self._key(link) + LINK: target
            for link, target in _links_on_the_way(spelled, self.scope).items()
        }

    def script(self, tool: str, field: str, path: Path) -> Command:
        """The approval a script run by path needs: the path and its content."""
        text = Path(path).resolve().as_posix()
        try:
            digest = _sha256(Path(path))
        except OSError:
            digest = "unreadable"
        return Command(tool, field, text, ((text, digest),))

    # ------------------------------------------------------------------ deciding
    def approved(self, cmd: Command) -> bool:
        """Whether `cmd` is approved as it stands. Prints nothing and records nothing."""
        self._consulted = True
        return self.store is not None and any(cmd.matches(r) for r in self._records)

    def allows(self, cmd: Command) -> bool:
        """Whether `cmd` may run now, saying why when it may not.

        Approved already: yes, silently. Pending under `--trust-commands`, and listed by
        an earlier run of this verb with the same digest: printed as `[TRUSTED]`,
        recorded, yes. Pending
        otherwise: printed as `[UNTRUSTED]` with its id, the listing recorded, counted,
        no. No git repository: nothing can be recorded, so no, even with the flag.
        """
        self._consulted = True
        if cmd not in self._decided:
            self._decided[cmd] = self._decide(cmd)
        return self._decided[cmd]

    def _decide(self, cmd: Command) -> bool:
        if self.store is None:
            self._no_store()
            self.skipped += 1
            return False
        if cmd.refusal:
            print(f"[FAIL] {cmd.tool}: {cmd.field} not run - {cmd.refusal}: {cmd.text}")
            self._refused += 1
            self.skipped += 1
            return False
        if self.approved(cmd):
            return True
        # The listing is per verb, and so is what the flag approves: a command another
        # verb's run listed was not in this verb's latest look, which may have dropped it.
        if self.approve and self.verb in self._listed.get(cmd.digest, {}).get("by", ()):
            print(f"[TRUSTED] {cmd.tool}: {cmd.field} approved and recorded: {cmd.text}")
            print(f"    id {cmd.id}, as an earlier run listed it")
            self.explain(cmd)
            self._record(cmd)
            return True
        run = f"`radar {self.verb}` run" if self.verb else "run"
        why = (
            f"no earlier {run} listed it as it stands now, so {FLAG} does not approve it yet"
            if self.approve
            else "new or changed since it was last approved"
        )
        print(f"[UNTRUSTED] {cmd.tool}: {cmd.field} not run - {why}: {cmd.text}")
        print(f"    listed as id {cmd.id}: {FLAG} on a later run approves it as it stands now")
        self.explain(cmd)
        self._list(cmd)
        self.skipped += 1
        return False

    def listed(self, cmd: Command) -> str:
        """Record `cmd`, pending, as shown to the operator by a listing that is not a
        run - `radar apply --apply` without `--yes`. Returns the id to print beside it."""
        if self.store is not None and not cmd.refusal and not self.approved(cmd):
            self._list(cmd)
        return cmd.id

    @property
    def failures(self) -> int:
        """FAIL lines this object printed: 1 once it said there is nowhere to keep
        approvals, plus one per command it refused to run. A caller adds it to the FAIL
        count its summary prints."""
        return (1 if self._told_no_store else 0) + self._refused

    def closing(self) -> None:
        """The last word on a run: it ends the run's listing (`_replace_listing`), and
        says what to do about the commands it refused. Silent when nothing was."""
        self._replace_listing()
        if not self.skipped:
            return
        print()
        if self.store is None:
            print(
                f"{self.skipped} catalogue command(s) not run: approvals need the data root "
                "to be inside a git repository."
            )
            return
        if self._refused:
            print(
                f"{self._refused} catalogue command(s) refused. A batch file on Windows runs "
                "only when cmd.exe reads its path and arguments as plain text: change the "
                "path or argument each [FAIL] above names, or name the program the batch "
                "file starts."
            )
        untrusted = self.skipped - self._refused
        if not untrusted:
            return
        print(
            f"{untrusted} catalogue command(s) not run. Review each [UNTRUSTED] command "
            "above - it comes from the catalogue and runs with your account - then re-run "
            f"with {FLAG} to approve and run them. {FLAG} approves only the ids listed "
            "above: a command added or changed after this run is listed again, not run."
        )

    # ------------------------------------------------------------------ internals
    def explain(self, cmd: Command) -> None:
        """Print, indented, what the approval of `cmd` covers and what the listing notes
        about it: the lines under an `[UNTRUSTED]` or `[TRUSTED]` line."""
        for path, value in cmd.files:
            if path.endswith(LINK):
                print(
                    f"    it passes through the link {path.removesuffix(LINK)} -> {value}, "
                    "whose target is part of the approval"
                )
            elif path.endswith(DIRECTORY):
                print(
                    f"    it names a file in {path.removesuffix(DIRECTORY)}, whose whole "
                    "content is part of the approval"
                )
            else:
                print(f"    it names {path}, whose content is part of the approval")
        for note in cmd.notes:
            print(f"    {note}")

    def _no_store(self) -> None:
        if self._told_no_store:
            return
        self._told_no_store = True
        print(
            f"[FAIL] trust: {self.root.as_posix()} is not inside a git repository, so no "
            "catalogue command can be approved and none is run. Approvals live in "
            f"<git common dir>/{STORE_DIR}/{STORE_FILE}, a place git never tracks - put "
            "the catalogue under git (`git init`) and approve with " + FLAG
        )

    def _load(self) -> tuple[list[dict], dict[str, dict]]:
        """(the approvals, the digests of the commands listed and not yet approved, each
        with the date it was first listed and the verbs whose latest run listed it)."""
        if self.store is None or not self.store.is_file():
            return [], {}
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
            recs = data.get("approvals") if isinstance(data, dict) else None
        except (OSError, ValueError):
            data, recs = None, None
        if not isinstance(recs, list):
            # Unreadable approvals approve nothing; the next write replaces the file.
            print(f"[WARN] trust: {self.store.as_posix()} is unreadable; treating it as empty")
            return [], {}
        listed = data.get("listed") if isinstance(data, dict) else None
        if not isinstance(listed, dict):
            listed = {}
        # A listing in any other shape, or kept under anything but a whole digest,
        # approves nothing: the command is listed again when shown.
        return (
            [r for r in recs if isinstance(r, dict)],
            {
                k: {"on": str(v.get("on", "")), "by": [str(b) for b in v["by"]]}
                for k, v in listed.items()
                if _is_digest(k) and isinstance(v, dict) and isinstance(v.get("by"), list)
            },
        )

    def _list(self, cmd: Command) -> None:
        self._shown.add(cmd.digest)
        entry = self._listed.setdefault(cmd.digest, {"on": date.today().isoformat(), "by": []})
        if self.verb in entry["by"]:
            return
        entry["by"].append(self.verb)
        self._save()

    def _replace_listing(self) -> None:
        """Drop from this verb's listing every command this run did not show, when this
        run asked about at least one command, with the flag or without it. A command no
        verb lists any more is gone."""
        if not self._consulted or self.store is None:
            return
        changed = False
        for digest in list(self._listed):
            by = self._listed[digest]["by"]
            if self.verb in by and digest not in self._shown:
                by.remove(self.verb)
                changed = True
                if not by:
                    del self._listed[digest]
        if changed:
            self._save()

    def _record(self, cmd: Command) -> None:
        self._records = [
            r
            for r in self._records
            if not (
                r.get("tool") == cmd.tool
                and r.get("field") == cmd.field
                and r.get("command") == cmd.text
            )
        ]
        self._records.append(cmd.record())
        self._listed.pop(cmd.digest, None)
        self._save()

    def _save(self) -> None:
        assert self.store is not None
        payload = {"schema": SCHEMA, "approvals": self._records, "listed": self._listed}
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_name(self.store.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.store)
        except OSError as exc:
            print(f"[WARN] trust: {self.store.as_posix()} could not be written ({exc})")
