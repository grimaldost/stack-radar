"""Where the catalogue is. The data root, and the directories it implies.

A module of its own because the root resolver is what every command needs FIRST and
what nothing else depends on, so it is the one module here that imports no sibling. An
installed engine has no `__file__` anywhere near the data it reads, and this is the
module that says so.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import NamedTuple

# --------------------------------------------------------------------- the data root

MARKER = "radar.toml"

# The env var and the flag, in the order they are consulted. Both exist for CI and for
# nothing else: the ordinary way to reach a data root is to stand in
# one. Each still has to name a directory that carries the marker, so a typo fails
# naming the file rather than resolving to a plausible wrong tree.
ENV_VAR = "RADAR_DATA_ROOT"
FLAG = "--data-root"


class DataRootNotFound(Exception):
    """No radar data root: no marker above the working directory, none named.

    Its own exception type because the callers need to spend a distinct EXIT CODE on it.
    A missing data root is not a finding about a catalogue - it is the absence of one -
    and a gate that answered `1` for both would make "the invariant failed" and "there
    was nothing to check" the same shell condition, which is the class of confusion the
    skipped-check reporting in gate.py exists to prevent.
    """


class DataRoot(NamedTuple):
    path: Path
    # How it was found, printed on the first line of every command. Not decoration: with
    # three ways in, "which tree did that just judge, and why that one" is otherwise
    # answerable only by re-deriving the resolution by hand.
    source: str


def _flag_value(argv: list[str] | None = None) -> str | None:
    """`--data-root X` or `--data-root=X`, read straight from argv.

    Read here rather than taken from each parser because the commands are not uniform:
    `render.py` has no argparse at all, and threading an override through every call
    site would mean the flag worked on some commands and not others. The argparse
    commands still DECLARE the option - argparse rejects what it does not know - and
    both readings see the same string.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for n, arg in enumerate(args):
        if arg == FLAG and n + 1 < len(args):
            return args[n + 1]
        if arg.startswith(FLAG + "="):
            return arg.split("=", 1)[1]
    return None


def _marked(given: str, source: str) -> DataRoot:
    # `expanduser` on a path the OPERATOR typed: they wrote `~`, so they named their own
    # home and this only honours the spelling. It is not the code finding the home, which
    # is why the publication check's home-finding rule flags `expanduser` only on a `~`
    # written in the source.
    path = Path(str(given).replace("\\", "/")).expanduser()
    if not (path / MARKER).is_file():
        raise DataRootNotFound(
            f"{source} names {path}, which holds no {MARKER} - the marker is what makes "
            "a directory a radar data root, and an override does not exempt it"
        )
    return DataRoot(path.resolve(), source)


def resolve_data_root(override: str | None = None, *, start: Path | None = None) -> DataRoot:
    """Where the catalogue is, and how that was decided.

    The search is the marker, the way git finds `.git`: up from the working directory
    until a `radar.toml` turns up. It is never derived from `__file__`, which answers a
    different question - where the MODULE is - and gives the same answer only when the
    engine and the catalogue share one tree. An installed engine has no
    `__file__` anywhere near the data it reads.
    """
    given = override if override is not None else _flag_value()
    if given:
        return _marked(given, FLAG)
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return _marked(from_env, ENV_VAR)
    here = (Path(start) if start else Path.cwd()).resolve()
    for cand in (here, *here.parents):
        if (cand / MARKER).is_file():
            return DataRoot(cand, MARKER)
    raise DataRootNotFound(
        f"no {MARKER} in {here} or any directory above it - a radar command reads the "
        f"catalogue it is standing in. Run it from inside the data root, or name one "
        f"with {FLAG} / {ENV_VAR}"
    )


def data_root(override: str | None = None) -> Path:
    """The data root, resolved. Never cached: a test that moves the tree moves it."""
    return resolve_data_root(override).path


# The six directories the root implies. Functions rather than constants because a
# constant is evaluated at IMPORT time, which would resolve the root - and therefore
# fail, in a directory with no marker - merely because a module was imported. Import has
# to stay free of it: `--help` must work, and the import smoke proof in
# tests/test_data_root.py depends on nothing being resolved before a command runs.
def tools_dir(root: Path | None = None) -> Path:
    return (root or data_root()) / "tools"


# ------------------------------------------------------------ linked git worktrees
#
# The gitignored files a data root carries - `tools.local/` and `redact.local.toml` - live
# in ONE checkout. `git worktree add` makes a second checkout of the same catalogue that has
# neither, because git never copies an ignored file, so a command run there would quietly
# lose every machine-local entry and every declared home: the gate would stop looking for
# the scoped names and the redaction would stop mapping them. So when the data root is a
# linked worktree with no copy of its own, the main worktree's copy is read instead, and
# the command says so once on stderr.
#
# Found by READING FILES, never by running git: a linked worktree's `.git` is a file
# holding `gitdir: <common>/worktrees/<name>`, that directory holds `commondir` (the path
# of the repository's common git directory, usually `../..`), and the main worktree is
# the parent of the common directory. Every command resolves this, and a subprocess per
# command for a question two small files answer would be cost with no gain.

_NOTED: set[str] = set()


def main_worktree(root: Path) -> Path | None:
    """The main worktree of the repository `root` is a LINKED worktree of; None otherwise.

    None for a main worktree (its `.git` is a directory), for a directory outside git, for
    a submodule (its gitdir has no `commondir`), for a worktree of a bare repository (there
    is no main checkout) and for anything that does not parse.
    """
    dotgit = root / ".git"
    if not dotgit.is_file():
        return None
    try:
        first = dotgit.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(first[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    commondir = gitdir / "commondir"
    try:
        common = Path(commondir.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return None
    if not common.is_absolute():
        common = gitdir / common
    common = common.resolve()
    main = common.parent
    # Only a checkout's `.git` directory has a main worktree around it. The common dir of
    # a bare repository is the repository itself, and its parent is whatever directory it
    # sits in: no overlays live there, and it is not the catalogue's own path.
    if (main / ".git").resolve() != common:
        return None
    if not main.is_dir() or main == root.resolve():
        return None
    return main


def local_overlay(root: Path, name: str) -> Path:
    """`root / name`, or the main worktree's copy when `root` is a linked worktree that
    has none of its own. With neither present the answer is `root / name`.

    The first time a process takes a file from the main worktree it prints one `[NOTE]`
    to stderr naming it - stderr so that a command whose stdout is a document stays one.
    """
    own = root / name
    if own.exists():
        return own
    main = main_worktree(root)
    if main is None or not (main / name).exists():
        return own
    if name not in _NOTED:
        _NOTED.add(name)
        print(
            f"[NOTE] {name} read from the main worktree {main.as_posix()} - this data root "
            "is a linked git worktree with no copy of its own",
            file=sys.stderr,
        )
    return main / name


def tools_local_dir(root: Path | None = None) -> Path:
    """Machine-local tool entries, gitignored, for private tooling whose NAME must not
    travel in a tracked repository, wherever it is hosted. The tool-level counterpart of
    environments/*.local.toml. Content that legitimately refers to such a tool is mapped
    to a placeholder on ingest - see redact.py. In a linked git worktree without its own
    copy, the main worktree's is read (`local_overlay`).

    THE TRAP, because it is not obvious: README.md and docs/radar/*.html are COMMITTED
    and are rendered from load_tools(). A local entry that reached a renderer would be
    written straight into a tracked file and the gitignore would have bought nothing.
    Every entry loaded from here carries `_local = True`, and render.py drops those
    before writing any tracked artefact. That is the whole safety mechanism — do not
    remove the flag.
    """
    return local_overlay(root or data_root(), "tools.local")


def snap_dir(root: Path | None = None) -> Path:
    return (root or data_root()) / "snapshots"


def env_dir(root: Path | None = None) -> Path:
    return (root or data_root()) / "environments"


def feedback_dir(root: Path | None = None) -> Path:
    return (root or data_root()) / "feedback"


def field_dir(root: Path | None = None) -> Path:
    return (root or data_root()) / "field"


# ------------------------------------------------------------ writing inside the root
#
# Every file the engine writes into a data root - README.md, PUBLIC.md, docs/radar/*,
# snapshots/*, field reports, archived feedback, profiles, the files `radar init` and
# `radar add` create - is written through `write_inside`. A catalogue takes pull requests,
# and a symbolic link committed in place of README.md, or in place of a directory such as
# docs/, would otherwise carry the next routine `radar render` to whatever file the link
# names, with the operator's account.


class UnsafeWrite(Exception):
    """A file the engine will not write inside a data root. The message names the path
    and the reason."""


def is_link(path: Path) -> bool:
    """Whether `path` is a symbolic link, or a Windows junction, which redirects a
    directory the same way. A missing path is neither."""
    try:
        if path.is_symlink():
            return True
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except (OSError, ValueError):
        return False
    return bool(tag) and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def write_error(root: Path, path: Path) -> str | None:
    """Why `path` may not be written as a file of the data root `root`, or None.

    Refused: a path outside the data root; a path that is a link, or that a link between
    the data root and it leads to; and a path whose directory resolves outside the data
    root by any other route.
    """
    base = Path(os.path.abspath(root))
    target = Path(os.path.abspath(path))
    shown = target.as_posix()
    try:
        rel = target.relative_to(base)
    except ValueError:
        return f"{shown} is outside the data root {base.as_posix()}"
    if not rel.parts:
        return f"{shown} is the data root itself, not a file in it"
    here = base
    for part in rel.parts:
        here = here / part
        if is_link(here):
            where = here.relative_to(base).as_posix()
            if here == target:
                return f"{shown} is a symbolic link; the engine does not write through one"
            return (
                f"{shown} lies under {where}, a symbolic link; the engine does not write "
                "through one"
            )
    try:
        escapes = not target.parent.resolve().is_relative_to(base.resolve())
    except (OSError, RuntimeError):
        escapes = True
    if escapes:
        return f"{shown} resolves outside the data root {base.as_posix()}"
    return None


def write_inside(root: Path, path: Path, content: str | bytes) -> None:
    """Write `content` to `path`, a file of the data root `root`, or raise UnsafeWrite
    naming why not (`write_error`). The write itself is `replace_file`'s."""
    problem = write_error(root, path)
    if problem:
        raise UnsafeWrite(problem)
    replace_file(path, content)


def replace_file(path: Path, content: str | bytes) -> None:
    """Write `content` to `path` by replacing it rather than writing through it.

    Text is written as UTF-8 exactly as given, so a "\\n" stays LF on every platform. The
    content goes to a new file beside the target, created exclusively, which then
    replaces the target: a symbolic link at the target is replaced, never followed. An
    existing target keeps its permission bits.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = None if is_link(path) else stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = None
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
