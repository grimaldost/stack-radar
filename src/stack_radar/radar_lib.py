"""Shared loading/validation for stack-radar. Stdlib only (Python 3.11+).

THE CONTROL-PLANE INVARIANT
---------------------------
A catalogue is a CONTROL PLANE. The stack it governs (plugins, skills, MCP servers,
settings, CLAUDE.md) is the DATA PLANE and must never reference the catalogue or depend
on it at runtime. Knowledge flows one way: the radar reads and writes the stack; the
stack never reads the radar. If the catalogue vanishes, the stack freezes in its current
state and keeps working. `radar gate` enforces this mechanically.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import json
import re
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path, PurePath
from typing import TypeVar

from .paths import (
    MARKER,
    DataRootNotFound,
    data_root,
    env_dir,
    resolve_data_root,
    snap_dir,
    tools_dir,
    tools_local_dir,
)


T = TypeVar("T")


class FrameworkMismatch(Exception):
    """The engine reading this catalogue is outside the range the catalogue requires."""


# ------------------------------------------------------------------------ the marker


def load_marker(root: Path | None = None) -> dict:
    """The `radar.toml` at the root, as a dict."""
    with ((root or data_root()) / MARKER).open("rb") as fh:
        return tomllib.load(fh)


# One comparator of a `requires_framework` range: `>=0.1.0`, `<0.3.0`, `==0.2.0`. A range
# is those, comma-separated, and every one of them has to hold. Deliberately smaller than
# PEP 440: this is a stdlib-only module, and the versions it compares are three dotted
# integers - the `X.Y.Z` of every release heading - so the parser that reads them can
# be too - and it REFUSES what it does not understand rather than passing it.
_COMPARATOR = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)$")

_COMPARE = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(version).split("."))


def framework_errors(version: str, requires: str) -> list[str]:
    """Why `version` does not satisfy the range. Empty list == it does."""
    errs: list[str] = []
    try:
        have = _version_key(version)
    except ValueError:
        return [f"{version!r} is not a dotted numeric version, so nothing can be compared to it"]
    for clause in str(requires).split(","):
        clause = clause.strip()
        if not clause:
            continue
        m = _COMPARATOR.match(clause)
        if not m:
            errs.append(
                f"`requires_framework` clause {clause!r} is not a comparator followed by a "
                f"dotted version ({'/'.join(_COMPARE)}) - an unreadable range is refused "
                "rather than ignored, because an ignored range constrains nothing"
            )
            continue
        op, want = m.group(1), m.group(2)
        if not _COMPARE[op](have, _version_key(want)):
            errs.append(f"engine {version} does not satisfy {clause}")
    return errs


# The engine's repository. Its documents - `docs/concepts.md`, `docs/new-environment.md`,
# SECURITY.md - are not part of the installed package, so a message that sends the reader
# to one gives its address there (`engine_docs`), never a path they would look for locally.
ENGINE_REPOSITORY = "https://github.com/grimaldost/stack-radar"


def engine_docs(page: str) -> str:
    """The address of `docs/<page>` in the engine's repository, at this engine's release
    tag, so the page read is the one written for the version that is running."""
    return f"{ENGINE_REPOSITORY}/blob/v{framework_version()}/docs/{page}"


def framework_version(root: Path | None = None) -> str:
    """The version of the engine reading this catalogue: the engine's own `__version__`.

    Not the data root's `pyproject.toml`: an installed engine has a version of its own,
    and a catalogue's `requires_framework` constrains that one. Read from the catalogue,
    the check would compare a catalogue with itself and could not fail for a real reason.
    `root` stays in the signature so every caller keeps working; it does not change the
    answer.
    """
    from . import __version__

    return __version__


def check_framework(root: Path | None = None, marker: dict | None = None) -> None:
    """Raise unless the engine satisfies the catalogue's `requires_framework`.

    Checked on EVERY command, with two exceptions: `changelog-gate` and
    `check-version-sites` judge whatever repository they are run from, run without a
    marker at all, and so have no range to be checked against - which is what lets them
    run in a repository that has no `radar.toml`.

    It fails naming BOTH versions. An engine too old for a catalogue does not misbehave
    loudly: it reads the fields it knows and silently ignores the rest, so the report is
    plausible and wrong. The error has to say what was required and what is present, or
    the reader is left diffing two repositories to find out.
    """
    root = root or data_root()
    requires = (marker if marker is not None else load_marker(root)).get("requires_framework")
    if not requires:
        return
    version = framework_version(root)
    errs = framework_errors(version, str(requires))
    if errs:
        raise FrameworkMismatch(
            f"this catalogue requires framework {requires!r} and the engine here is "
            f"{version}: " + "; ".join(errs)
        )


def begin_command(override: str | None = None, *, stream=None) -> Path:
    """Every command's first act: resolve the root, check the range, say both.

    ON STDERR, and that is the one deliberate reading of "prints the resolved root on the
    first line". Two commands write a machine-readable document to stdout -
    `radar field --json` emits the report another reader parses, and
    `radar render-feedback-targets --stdout` emits the artefact itself - so a banner there
    would corrupt them. stderr is still the first line the command prints, and it stays
    out of every pipe that means something.

    Both failures leave through EXIT CODE 2. The gate spends 1 on findings, so a run that
    never found its catalogue has to be distinguishable from one that judged a catalogue
    and did not like it.
    """
    try:
        root = resolve_data_root(override)
        check_framework(root.path)
    except (DataRootNotFound, FrameworkMismatch) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except tomllib.TOMLDecodeError as exc:
        # The marker was found and could not be read, so no catalogue was judged.
        print(
            f"[FAIL] {MARKER} at {root.path.as_posix()} is not valid TOML ({exc}); "
            "nothing was read",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(
        f"[NOTE] data root {root.path.as_posix()} (found by {root.source})",
        file=stream or sys.stderr,
    )
    return root.path


RINGS = ["own", "adopt", "pilot", "observe", "discard"]

# THE AXES an entry may declare - which problem a tool addresses. This is the default set,
# small and general on purpose: a catalogue that wants axes of its own declares them in
# radar.toml as `[entries].axes`, which replaces this set (`catalogue_axes`).
DEFAULT_AXES = [
    "build",
    "lint-format",
    "testing",
    "agent-plugins",
    "mcp-servers",
    "other",
]
AXES = DEFAULT_AXES
AXES_KEY = "[entries].axes"
# An axis is a slug: lowercase letters and digits in words joined by single hyphens.
AXIS_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def axes_errors(marker: dict) -> list[str]:
    """What is wrong with the marker's declared axes. Empty when they are well formed or
    not declared."""
    entries = marker.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, dict):
        return ["[entries] must be a table"]
    if "axes" not in entries:
        return []
    axes = entries["axes"]
    if not isinstance(axes, list) or not axes:
        return [f"{AXES_KEY} must be a non-empty list of axis names"]
    bad = [a for a in axes if not (isinstance(a, str) and AXIS_PATTERN.fullmatch(a))]
    if bad:
        return [
            f"{AXES_KEY} holds {bad!r}: an axis is lowercase letters and digits, in words "
            "joined by single hyphens"
        ]
    twice = sorted({a for a in axes if axes.count(a) > 1})
    if twice:
        return [f"{AXES_KEY} names {twice!r} more than once"]
    return []


def catalogue_axes(marker: dict | None = None, root: Path | None = None) -> list[str]:
    """The axes this catalogue's entries may declare: `[entries].axes` from radar.toml
    when it is declared and well formed, else DEFAULT_AXES. A malformed declaration is
    reported by `axes_errors`, which the gate and `radar add` print as a FAIL."""
    if marker is None:
        try:
            marker = load_marker(root)
        except (OSError, tomllib.TOMLDecodeError):
            return list(DEFAULT_AXES)
    if axes_errors(marker):
        return list(DEFAULT_AXES)
    declared = (marker.get("entries") or {}).get("axes")
    return list(declared) if declared else list(DEFAULT_AXES)


REQUIRED = ("name", "repo", "axis", "ring", "license", "visibility", "note", "artifact")

# The shape of a tool's `name`. The name is used as a path segment (a feedback directory,
# a rendered target) and as text in generated HTML, so a name holding `/`, `..` or `<`
# would move a write or break out of the page. One pattern, checked where every entry is
# checked, rather than an escape at each place that uses the name. It is applied with
# fullmatch(): under match() the `$` also matches before a trailing newline.
#
# The same pattern is every other name that becomes one path component: an environment
# name (`--env`, which names environments/<env>.toml), and the tool names that
# sync-feedback, render-feedback-targets and the field report turn into directories. They
# all read it from here, so the schema and the commands cannot disagree about a name.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The device names Windows reserves in every directory, whatever the case and with or
# without an extension: a file called `con.toml` or `aux.txt.toml` cannot be created or
# checked out there, so a catalogue that committed one could not be cloned onto Windows,
# and the Windows leg of its CI would fail on checkout.
RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
)
# The rule in words, for every message that states it to the operator.
NAME_RULE = (
    "letters, digits, '.', '_' and '-', starting with a letter or digit, and not a device "
    "name Windows reserves (CON, PRN, AUX, NUL, COM0-COM9, LPT0-LPT9, with or without an "
    "extension)"
)


def is_plain_name(name: object) -> bool:
    """Whether `name` is one plain path component under NAME_PATTERN, and not a reserved
    device name. Rules out `.`, `..`, separators, hidden names, characters a file system
    refuses (`<`, `>`, `:`), and names Windows keeps for devices (`CON`, `nul`,
    `aux.txt`)."""
    return (
        isinstance(name, str)
        and NAME_PATTERN.fullmatch(name) is not None
        and name.split(".", 1)[0].upper() not in RESERVED_DEVICE_NAMES
    )


def placeholder_error(tool: dict, placeholder: str) -> str | None:
    """Why `placeholder` cannot stand in for the machine-local entry `tool`, or None.

    A placeholder replaces the entry's name and its `redact_also` spellings in tracked text
    and file names, and names the directory its field report is written to. So it is one
    plain path component, and it contains none of the terms it replaces: redaction matches
    them anywhere and in any case, so a placeholder that contains one writes it back.
    """
    if not is_plain_name(placeholder):
        return (
            f"a placeholder is {NAME_RULE} - it replaces the name in tracked file names and "
            "names the field report's directory, so it cannot carry a separator"
        )
    also = tool.get("redact_also")
    terms = [tool.get("name"), *(also if isinstance(also, list) else [])]
    for term in terms:
        if isinstance(term, str) and term and term.lower() in placeholder.lower():
            return f"it contains {term!r}, a name it replaces, so redaction would write it back"
    return None


def file_form(name: str) -> str:
    """`name` in the form an entry's file name takes: lowercase, with every character other
    than an ASCII letter, a digit, '.', '_' or '-' folded to a hyphen, every run of hyphens
    folded to one, and no hyphen at either end.

    An entry's name is its file's stem, in this form already (`entry_file_errors`): a name
    in any other form would sit in a file named differently from itself, and two names
    that differ only in case would claim one file on a system that ignores case.
    """
    kept = [c if (c.isascii() and (c.isalnum() or c in "._-")) else "-" for c in name.lower()]
    return "-".join(part for part in "".join(kept).split("-") if part)


def entry_file_errors(tools: Sequence[dict]) -> list[tuple[str, str]]:
    """(name, why) for each entry that breaks the rule tying an entry to its file.

    The rule, which `radar add` applies when it writes and the gate applies to every file:
    an entry's `name` is already in file form (`file_form`), its file is `<name>.toml`,
    and no two entries - in `tools/` or `tools.local/` - claim one name, compared
    ignoring case. A name `validate()` refuses outright is left to it.
    """
    out: list[tuple[str, str]] = []
    claimed: dict[str, list[tuple[str, str]]] = {}
    for t in tools:
        name = t.get("name")
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            continue
        where = str(t.get("_file") or "?")
        shown = where if "/" in where else f"tools/{where}"
        folder = shown.rsplit("/", 1)[0]
        if file_form(name) != name:
            out.append(
                (
                    name,
                    f"the name is not in the form a file name takes: enter the name as "
                    f"{file_form(name)!r} - lowercase, single hyphens, no hyphen at either "
                    f"end - in {folder}/{file_form(name)}.toml",
                )
            )
        elif shown != f"{folder}/{name}.toml":
            out.append(
                (
                    name,
                    f"it lives in {shown}, a file named differently from the entry: an "
                    f"entry's file is named after it, {folder}/{name}.toml",
                )
            )
        claimed.setdefault(name.lower(), []).append((name, shown))
    for owners in claimed.values():
        if len(owners) > 1:
            files = ", ".join(shown for _, shown in owners)
            out.append(
                (
                    owners[0][0],
                    f"the name is claimed by {len(owners)} entries ({files}); a tool has "
                    "one entry, so keep one file and remove the others",
                )
            )
    return out


def environment_name(value: str) -> str:
    """The argparse `type` of every `--env`: the value unchanged, or exit 2 naming the flag.

    An environment name becomes a file name under environments/ and a TOML string in a
    profile, so a value such as `../../x` would read or write outside the catalogue.
    Refused before a data root is even resolved, by the one rule every verb shares.
    """
    if not is_plain_name(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an environment name: use {NAME_RULE}")
    return value


# The one admitted value of `repo` for a public entry with no repository at all.
# `radar render` writes `repo` as a Markdown link in the public projection; a local path
# there would publish that path, and this sentinel is how a phantom entry (a tool that
# research turned up and that turned out not to exist) is recorded honestly instead of by
# inventing a URL or hiding the tool from the public projection.
NO_REPO_SENTINEL = "(repo does not exist)"
_REPO_URL_PREFIXES = ("http://", "https://", "git@")

# WHAT KIND OF THING IS THIS — the dimension `axis` does not carry.
# `axis` says which problem a tool addresses; `artifact` says what you actually install
# and where it runs, which is what decides the three things an axis cannot answer:
# whether it costs context, how big its blast radius is, and whether a locked-down
# environment can have it at all. Two entries on the same axis can be a
# library and a hosted service — utterly different commitments.
ARTIFACTS = [
    "cc-plugin",  # Claude Code plugin: skills / agents / commands / hooks
    "mcp-server",  # exposes a tool surface INTO the agent's context (costs tokens)
    "cc-lsp",  # Claude Code language-server plugin: native, no tool surface
    "cli",  # standalone command, run from a shell or CI; no context cost
    "python-lib",  # imported by project code, so it becomes a runtime dependency
    "service",  # a server + UI that somebody has to operate, authenticate and patch
    "standard",  # spec or wire format; nothing to install, only something to target
    "corpus",  # authored knowledge, not executable
    "unknown",  # never established — reserved for entries the gate rejected
    # before their nature was worth verifying (astroturf, phantoms). Honest by design:
    # "we did not check what this is, because it failed on evidence first."
]

# [version].policy — what "current enough" means for one entry. Without a policy a check
# can only say WHETHER a tool is present, not WHICH version, and "installed" and "current"
# collapse into one word.
#   latest  warn when behind the registry (default for anything with a `registry`)
#   pin     fail on any deviation — for a tool whose behaviour was MEASURED at one version
#   floor   fail below `min`, silent above — a required fix, newer is fine
#   any     never compared — the version is not a meaningful axis, or it is pinned
#           per-project by each repo's lockfile rather than machine-wide
VERSION_POLICIES = ["latest", "pin", "floor", "any"]

# `registry` - where an entry's releases are read, and for a package index its downloads
# too. Taken from the tool's own repository metadata, never found by matching a name:
#   pypi:<name>            a PyPI project - releases and a monthly download count
#   npm:<name>             an npm package, scoped names included - the same two figures
#   github:<owner>/<repo>  a repository whose releases are its git tags. It has NO
#                          download count, so the download and astroturf checks treat it
#                          as declaring no download registry rather than as a lookup
#                          that failed
# Any other kind is refused: a registry nothing can resolve reads like one that was checked.
REGISTRY_KINDS = ("pypi", "npm", "github")
DOWNLOAD_REGISTRY_KINDS = ("pypi", "npm")
_REGISTRY_TARGET = {
    "pypi": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"),
    "npm": re.compile(r"^(?:@[A-Za-z0-9~-][A-Za-z0-9._~-]*/)?[A-Za-z0-9~-][A-Za-z0-9._~-]*$"),
}
# `owner/repo` as GitHub spells them. `radar versions` puts it into a URL, so the gate and
# that command share this one reading of a `github:` target (github_slug below).
GITHUB_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
_REGISTRY_FORMS = "pypi:<name>, npm:<name> or github:<owner>/<repo>"


# A github.com URL in an entry's free-text `repo` field: an https, http or ssh URL, or the
# `git@github.com:` form, whose host is github.com and nothing else. The match is anchored
# at the start, so `notgithub.com/...` and a URL that merely mentions `github.com/` in its
# path are not read as GitHub repositories. The `owner/name` part stops at `/`, `?`, `#` or
# whitespace, so a fragment or a query string is not part of the name.
_GITHUB_REPO_URL = re.compile(
    r"^(?:(?:https?|ssh)://(?:git@)?|git@)?github\.com[/:]([^/?#\s]+/[^/?#\s]+)",
    re.IGNORECASE,
)


def github_repo(repo: object) -> str | None:
    """`owner/name` from an entry's `repo` when it is a github.com URL, else None.

    `radar snapshot` looks the repository up by it, and the gate reads it to tell an
    entry that has nothing to measure from one whose measurement is missing. The name is
    read by `github_slug`, so a trailing `.git` is dropped and anything GitHub would not
    accept as `owner/repo` is None.
    """
    m = _GITHUB_REPO_URL.match(str(repo or "").strip())
    return github_slug(m.group(1)) if m else None


def github_slug(target: str) -> str | None:
    """`owner/repo` from the target of a `github:` registry, or None when it is not one.

    A trailing `.git` is dropped first, as a clone URL spells it. The repository part may
    not be `.` or `..`, which would change the URL's path rather than name a repository.
    """
    slug = target.removesuffix(".git")
    if not GITHUB_SLUG.fullmatch(slug) or slug.partition("/")[2] in (".", ".."):
        return None
    return slug


def registry_errors(value: object) -> list[str]:
    """Why `value` is not a `registry` the engine can resolve. Empty list == it is one."""
    if not isinstance(value, str):
        return [f"`registry` must be a string ({_REGISTRY_FORMS}), got {value!r}"]
    kind, sep, target = value.partition(":")
    if not sep or kind not in REGISTRY_KINDS:
        return [
            f"bad registry {value!r}: the kinds are {_REGISTRY_FORMS} - a kind the engine "
            "cannot resolve would read as a registry that was checked"
        ]
    well_formed = (
        github_slug(target) is not None
        if kind == "github"
        else _REGISTRY_TARGET[kind].fullmatch(target) is not None
    )
    if not well_formed:
        return [
            f"bad registry {value!r}: {kind}: needs the form {kind}:"
            + ("<owner>/<repo>" if kind == "github" else "<name>")
        ]
    return []


def download_registry(t: dict) -> str | None:
    """The entry's `registry` when it is one that publishes a download count, else None.

    `github:` publishes releases as tags and no download figure at all, so for every
    download-shaped question it is the same as an entry that declares no registry.
    """
    reg = t.get("registry")
    if isinstance(reg, str) and reg.partition(":")[0] in DOWNLOAD_REGISTRY_KINDS:
        return reg
    return None


# [install].kind — how `radar apply` converges a tool into an environment
INSTALL_KINDS = [
    "uv-tool",  # uv tool install <spec>
    "npm-tool",  # npm install -g <pkg> (how the tool actually ships)
    "claude-plugin",  # a Claude Code plugin dir/marketplace entry
    "mcp-plugin",  # a plugin whose payload is an MCP server
    "mcp-server",  # claude mcp add ...
    "pip",  # pip/uv pip install into a project
    "repo",  # a working tree the user maintains (verify presence only)
    "manual",  # apply prints instructions, never executes
    "none",  # nothing to install (a standard, a spec, a dead entry)
]

# [install].check / .apply are single commands, not shell lines: `radar apply`, `radar
# versions` and `radar reconcile` split them with trust.catalogue_argv and run them without
# a shell, so pipes/redirects/&& are not available and paths must use forward slashes. They
# run only once the operator approved them, in an empty scratch directory, with the
# data-root files they name passed as absolute paths. `{key}` placeholders are filled from
# the environment profile's [paths] table, which is what keeps one entry usable on several
# machines.


# --------------------------------------------------------------------------- load


def load_tools(include_local: bool = True, root: Path | None = None) -> list[dict]:
    """Every tool entry. Local (gitignored) entries are tagged `_local = True`.

    Pass include_local=False when producing anything that gets committed or published.
    render.py does exactly that; see tools_local_dir() for why it matters.
    """
    root = root or data_root()
    tools = []
    for f in sorted(tools_dir(root).glob("*.toml")):
        with f.open("rb") as fh:
            t = tomllib.load(fh)
        t["_file"] = f.name
        t["_local"] = False
        tools.append(t)
    # Resolved only when asked for: in a linked worktree the lookup may take the main
    # worktree's copy and say so, and a caller that excludes local entries reads nothing.
    local = tools_local_dir(root) if include_local else None
    if local is not None and local.exists():
        for f in sorted(local.glob("*.toml")):
            with f.open("rb") as fh:
                t = tomllib.load(fh)
            t["_file"] = f"tools.local/{f.name}"
            t["_local"] = True
            tools.append(t)
    return tools


def expand_home(paths: dict | None, home: PurePath) -> dict:
    """`[paths]` with a leading `~` written as the home directory, forward-slashed.

    A profile is tracked, and a path spelled out in it carries the account name into the
    catalogue; `~/.claude` says the same thing without naming anybody. Every reader of a
    profile expands it here, so a consumer never sees the `~`. Forward slashes because
    that is how profiles spell their paths, so a spelled-out path and a `~` one load
    equal. Only `~` alone or followed by a separator counts: `~other/x` is another
    account's home, and guessing where that is would be wrong more often than not.

    Handed the home rather than finding it, like `redact.redact_home`: only
    `bootstrap.home_dir()` looks the home up.
    """
    out = dict(paths or {})
    for key, value in out.items():
        if isinstance(value, str) and (value == "~" or value[:2] in ("~/", "~\\")):
            out[key] = home.as_posix() + ("/" + value[2:] if len(value) > 2 else "")
    return out


def with_home(profile: dict, home: PurePath | None = None) -> dict:
    """`profile` with its `[paths]` expanded (`expand_home`). The home is asked of
    `bootstrap.home_dir()` when not handed; imported here because bootstrap imports
    this module."""
    if not isinstance(profile.get("paths"), dict):
        return profile
    if home is None:
        from .bootstrap import home_dir

        home = home_dir()
    return {**profile, "paths": expand_home(profile["paths"], home)}


class ProfileError(Exception):
    """A profile, or the overlay it names, that cannot be read as one. The message starts
    with the file, `environments/<file>`, so the `[FAIL]` line it becomes names it."""


def reports_profile_errors(main: Callable[[], T]) -> Callable[[], T]:
    """Wrap a command's `main` so a ProfileError ends it as one `[FAIL]` line and exit 1.

    Every verb that reads a profile carries this, so a broken profile or overlay reads the
    same everywhere and never as a traceback. It stops the command: a profile read in part
    would act on paths the operator did not write.
    """

    @functools.wraps(main)
    def run() -> T:
        try:
            return main()
        except ProfileError as exc:
            print(f"[FAIL] {exc}")
            raise SystemExit(1) from None

    return run


def _read_profile_file(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"environments/{path.name}: not readable as TOML ({exc})") from exc


def read_committed_profile(path: Path) -> dict:
    """The profile in `path`, as written: `[paths]` not yet expanded, no overlay layered.
    A ProfileError when it is not TOML or lacks the shape readers rely on (`shape_errors`).
    """
    profile = _read_profile_file(path)
    errs = shape_errors(profile)
    if errs:
        raise ProfileError(f"environments/{path.name}: " + "; ".join(errs))
    return profile


def committed_environments(root: Path | None = None) -> list[dict]:
    """Every committed environment profile, `[paths]` expanded (`expand_home`), without
    its overlay.

    Machine-local overlays (`*.local.toml`) are not profiles and are skipped here: they
    are fragments, and because "laptop.local.toml" sorts before "laptop.toml" a glob that
    kept them would hand the fragment back as if it were the whole profile. A profile's
    overlay is layered only by `layer_overlay`, and only when the profile names it.
    """
    envs = []
    directory = env_dir(root)
    if not directory.exists():
        return envs
    for f in sorted(directory.glob("*.toml")):
        if f.name.endswith(OVERLAY_SUFFIX):
            continue
        e = read_committed_profile(f)
        e["_file"] = f.name
        envs.append(with_home(e))
    return envs


# The keys of a profile whose values an overlay adds to rather than replaces: an overlay
# adds local policy, it does not relax the base.
_ACCUMULATING = ("exclude", "require_env")
# The keys that are tables in every profile, whether or not the base carries them.
_TABLES = ("paths", "flags")
OVERLAY_SUFFIX = ".local.toml"
# Every key an overlay may set, and how each is layered on the profile (`merge_overlay`):
# `name` is ignored, `description` and `rings` replace the profile's, `exclude` and
# `require_env` gain the overlay's items, and `[paths]` and `[flags]` merge key by key. Any
# other key is refused rather than layered, and so is `overlay`, at the top level or under
# `[flags]`: which file is the overlay is the committed profile's to say.
OVERLAY_KEYS = ("name", "description", "rings", *_ACCUMULATING, *_TABLES)


def overlay_name(profile: dict) -> str | None:
    """The file a profile names as its overlay, from `overlay` under `[flags]` (a top-level
    `overlay` key is read too). None when it names none.

    The name is one file in environments/ ending in `.local.toml`, the form `.gitignore`
    keeps out of git; anything else is a ProfileError rather than a path to follow.
    """
    flags = profile.get("flags")
    name = profile.get("overlay") or (flags.get("overlay") if isinstance(flags, dict) else None)
    if not name:
        return None
    if not is_plain_name(name) or not str(name).endswith(OVERLAY_SUFFIX):
        raise ProfileError(
            f"environments/{profile.get('_file', '?')}: `overlay` names {name!r}; it must be "
            f"one file in environments/ whose name ends in {OVERLAY_SUFFIX}"
        )
    return str(name)


def merge_overlay(base: dict, over: dict) -> dict:
    """Layer an overlay on a profile: lists that accumulate accumulate, the rest replaces.

    `exclude` and `require_env` concatenate. Tables ([paths], [flags]) merge key by key,
    so an overlay that sets one path keeps the others. `description` and `rings` replace.
    The overlay's `name` is ignored: the profile it is layered on is already chosen. An
    overlay that sets any other key never gets here (`overlay_errors`).
    """
    merged = dict(base)
    for key, value in over.items():
        if key in ("_file", "name"):
            continue
        if key in _ACCUMULATING:
            seen = list(merged.get(key) or [])
            for item in value:
                if item not in seen:
                    seen.append(item)
            merged[key] = seen
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def shape_errors(profile: dict) -> list[str]:
    """Why `profile` does not have the shape every reader of a profile relies on. Empty
    list == it has.

    `rings`, `exclude` and `require_env` are lists of strings; `[paths]` is a table of
    strings, because each value is a path; `[flags]` is a table. A reader handed anything
    else would stop on a traceback or read a wrong path, so the resolver refuses it first.
    Whether the rings are known, and the other rules of a profile, are `validate_environment`'s.
    """
    errs = []
    for key in ("rings", *_ACCUMULATING):
        value = profile.get(key)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(i, str) for i in value)
        ):
            errs.append(f"`{key}` must be a list of strings, got {value!r}")
    for key in _TABLES:
        value = profile.get(key)
        if value is not None and not isinstance(value, dict):
            errs.append(f"[{key}] must be a table, got {value!r}")
    paths = profile.get("paths")
    if isinstance(paths, dict):
        for k, v in paths.items():
            if not isinstance(v, str):
                errs.append(f"[paths].{k} must be a string (a path), got {v!r}")
    return errs


def overlay_errors(base: dict, over: dict) -> list[str]:
    """Why `over` cannot be layered on `base` by `merge_overlay`. Empty list == it can.

    The overlay sets only `OVERLAY_KEYS`, has the shape of a profile (`shape_errors`), and
    a key the committed profile also sets keeps that key's type, at the top level and
    inside a table, so the merged profile reads the same way the committed one does.
    """
    errs = [
        f"`{key}` is not a key an overlay may set (it may set "
        f"{', '.join(k for k in OVERLAY_KEYS if k != 'name')})"
        for key in over
        if key not in OVERLAY_KEYS
    ]
    flags = over.get("flags")
    if isinstance(flags, dict) and "overlay" in flags:
        errs.append("[flags].overlay is the committed profile's to name, not the overlay's")
    errs += shape_errors(over)
    for key, value in over.items():
        if key in ("rings", *_ACCUMULATING, "paths"):
            continue
        mine = base.get(key)
        if key in _TABLES or isinstance(mine, dict):
            if isinstance(value, dict) and isinstance(mine, dict):
                errs += [
                    f"[{key}].{k} must be a {type(mine[k]).__name__} as in the profile, got {v!r}"
                    for k, v in value.items()
                    if k in mine and type(v) is not type(mine[k])
                ]
            elif not isinstance(value, dict) and key not in _TABLES:
                errs.append(f"[{key}] must be a table, got {value!r}")
        elif key in base and type(value) is not type(mine):
            errs.append(f"`{key}` must be a {type(mine).__name__} as in the profile, got {value!r}")
    return errs


def layer_overlay(profile: dict, root: Path | None = None) -> tuple[dict, list[str]]:
    """(`profile` with the overlay it names layered on, the files read).

    The files are `environments/<profile>` and, when the profile names an overlay,
    `environments/<overlay> (overlay)` or `(overlay absent)`. The overlay's own `[paths]`
    are expanded before the merge, as the profile's already are.
    """
    sources = [f"environments/{profile.get('_file', '?')}"]
    name = overlay_name(profile)
    if name is None:
        return profile, sources
    over = read_overlay(profile, root)
    if over is None:
        return profile, [*sources, f"environments/{name} (overlay absent)"]
    return merge_overlay(profile, with_home(over)), [*sources, f"environments/{name} (overlay)"]


def read_overlay(profile: dict, root: Path | None = None, name: str | None = None) -> dict | None:
    """The overlay `profile` names (or the file `name` in environments/), as written -
    `[paths]` not expanded - or None when there is none or the file does not exist. A
    ProfileError naming the file when it cannot be layered on `profile` (`overlay_errors`).
    """
    name = name or overlay_name(profile)
    if name is None:
        return None
    path = env_dir(root) / name
    if not path.exists():
        return None
    over = _read_profile_file(path)
    errs = overlay_errors(profile, over)
    if errs:
        raise ProfileError(f"environments/{name}: " + "; ".join(errs))
    return over


def load_environments(root: Path | None = None) -> list[dict]:
    """Every committed profile with the overlay it names layered on (`layer_overlay`)."""
    return [layer_overlay(e, root)[0] for e in committed_environments(root)]


def base_profile(name: str, root: Path | None = None) -> dict | None:
    """The committed profile named `name`, preferring environments/<name>.toml when two
    files carry the same `name`."""
    envs = committed_environments(root)
    for e in envs:
        if e.get("name") == name and e.get("_file") == f"{name}.toml":
            return e
    for e in envs:
        if e.get("name") == name:
            return e
    return None


def load_profile(name: str, root: Path | None = None) -> tuple[dict | None, list[str]]:
    """(the profile `name` with its overlay layered on, the files read), or (None, []).

    The one reader of a profile for every verb that takes `--env`, so a path fixed in the
    overlay is the path each of them uses. Only this profile's overlay is read: a broken
    overlay of another environment does not stop a command about this one.
    """
    base = base_profile(name, root)
    if base is None:
        return None, []
    return layer_overlay(base, root)


def load_environment(name: str, root: Path | None = None) -> dict | None:
    """The profile `name` with its overlay layered on, or None (`load_profile`)."""
    return load_profile(name, root)[0]


# The environment name used when the marker declares none: the one `radar init` writes.
DEFAULT_ENVIRONMENT = "default"


def default_environment(root: Path | None = None) -> str:
    """The environment name `--env` falls back to when a command's flag is not given.

    A property of the catalogue (`default_environment`, a top-level key of radar.toml),
    not of the code that reads it - a catalogue that renames its environments only has to
    touch the marker. With the key absent the answer is `default`, the profile `radar init` writes,
    so a marker without the key falls back to a profile a new catalogue actually has.
    The readers are `radar field`, `radar reconcile`, `radar versions` and
    `radar sync-feedback`. `radar apply` (every mode), `radar bootstrap` and
    `radar render-feedback-targets` require `--env`: each converges or writes something
    for one named machine, and refuses an implicit environment rather than default to one.
    """
    return str(load_marker(root).get("default_environment") or DEFAULT_ENVIRONMENT)


def latest_snapshot(root: Path | None = None) -> tuple[str | None, dict]:
    snaps = sorted(snap_dir(root).glob("*.json"))
    if not snaps:
        return None, {}
    with snaps[-1].open(encoding="utf-8") as fh:
        return snaps[-1].name, json.load(fh)


# ----------------------------------------------------------------------- validate


def validate(t: dict, axes: Sequence[str] | None = None) -> list[str]:
    """Schema errors for one tool entry. Empty list == valid.

    `axes` is the catalogue's axis set (`catalogue_axes`); DEFAULT_AXES when not given.
    """
    allowed = DEFAULT_AXES if axes is None else axes
    errs = []
    for k in REQUIRED:
        if not t.get(k):
            errs.append(f"missing field: {k}")
    name = t.get("name")
    if name and not is_plain_name(name):
        errs.append(
            f"bad name {name!r}: a name is {NAME_RULE} - it becomes a file and directory "
            "name and is written into rendered pages, so it cannot carry a separator or markup"
        )
    # A machine-local entry's placeholder (placeholder_error). A blank one is the gate's
    # WARN (no placeholder declared), not a schema error.
    redact_as = t.get("redact_as")
    if redact_as is not None and not (isinstance(redact_as, str) and not redact_as.strip()):
        why = placeholder_error(t, redact_as.strip() if isinstance(redact_as, str) else "")
        if why:
            errs.append(f"bad redact_as {redact_as!r}: {why}")
    if t.get("ring") not in RINGS:
        errs.append(f"bad ring: {t.get('ring')}")
    if t.get("axis") not in allowed:
        errs.append(f"bad axis: {t.get('axis')} (the catalogue's axes: {', '.join(allowed)})")
    if t.get("artifact") not in ARTIFACTS:
        errs.append(f"bad artifact: {t.get('artifact')}")
    # An `unknown` artifact is only defensible where the entry never got far enough to
    # matter. Claiming it for something you are running is a gap pretending to be a value.
    if t.get("artifact") == "unknown" and t.get("ring") not in ("discard", "observe"):
        errs.append("artifact=unknown is only for discard/observe entries")
    if t.get("visibility") not in ("public", "private"):
        errs.append(f"bad visibility: {t.get('visibility')}")
    # A public entry's `repo` is rendered as a link (`radar render`), so a local path there
    # would publish that path. A URL is the honest case; the sentinel is the honest
    # alternative for a tool that never had a repository to begin with - not silence,
    # since a phantom entry the projection dropped would look like it was never caught.
    if t.get("visibility") == "public":
        repo = str(t.get("repo") or "")
        if repo != NO_REPO_SENTINEL and not repo.startswith(_REPO_URL_PREFIXES):
            errs.append(
                f"visibility=public needs `repo` as a URL ({'/'.join(_REPO_URL_PREFIXES)}) "
                f"or the literal {NO_REPO_SENTINEL!r} sentinel for a tool with no "
                "repository - `radar render` writes `repo` as a link in the public projection"
            )

    # TWO SCOPES, deliberately separate — they answer different questions:
    #   top-level `environments`   which environments this tool EXISTS in at all
    #   [install].environments     where `radar apply` should CONVERGE it
    # A tool may exist everywhere and be installed on one machine only; a site-specific
    # library exists only in one environment. Collapsing them would force one to lie. The
    # subset rule below keeps them from drifting apart.
    envs = t.get("environments")
    if envs is not None:
        if not isinstance(envs, list) or not all(isinstance(s, str) for s in envs):
            errs.append("`environments` must be a list of environment names")
        elif not envs:
            errs.append("`environments` is empty - omit it to mean 'every environment'")
        # An environment-scoped entry is private or site-specific by construction, so it
        # has no business in the published projection. Enforced rather than trusted: the
        # public render filters on visibility, and one `public` here would leak a name.
        elif t.get("visibility") != "private":
            errs.append(
                "an environment-scoped tool must be visibility=private "
                "(scoping exists to keep site-specific names out of the projection)"
            )
        inst_envs = (t.get("install") or {}).get("environments")
        if isinstance(envs, list) and isinstance(inst_envs, list):
            stray = sorted(set(inst_envs) - set(envs))
            if stray:
                errs.append(
                    f"[install].environments names {stray} outside `environments` - "
                    "cannot converge a tool where it does not exist"
                )

    errs.extend(history_errors(t))

    if t.get("registry") is not None:
        errs.extend(registry_errors(t.get("registry")))

    es = t.get("eval_status", "unmeasured")
    if es not in ("unmeasured", "eval-pending") and not es.startswith("measured:"):
        errs.append(f"bad eval_status: {es}")

    inst = t.get("install")
    if inst is not None:
        kind = inst.get("kind")
        if kind not in INSTALL_KINDS:
            errs.append(f"bad install.kind: {kind}")
        if kind not in ("none", "manual") and not inst.get("check"):
            errs.append(
                f"install.kind={kind} needs a `check` probe (idempotent, exit 0 == present)"
            )
        if kind not in ("none", "manual", "repo") and not inst.get("apply"):
            errs.append(f"install.kind={kind} needs `apply` (or use kind=manual)")
        # A manual entry whose instruction is missing tells the operator nothing, and
        # manual is the escape hatch precisely for the cases a script must not guess.
        if kind == "manual" and not inst.get("instruction"):
            errs.append("install.kind=manual needs `instruction` (what to run by hand)")
        envs = inst.get("environments")
        if envs is not None and (
            not isinstance(envs, list) or not all(isinstance(s, str) for s in envs)
        ):
            errs.append("[install].environments must be a list of environment names")
        # `remove` is advice printed when a machine still carries an unwanted tool.
        # Nothing executes it: apply proposes removals and never performs them.
        if inst.get("remove") is not None and not isinstance(inst["remove"], str):
            errs.append("[install].remove must be a string (a suggestion, not a script)")

    fb = t.get("feedback")
    if fb is not None:
        # ring=own, because a feedback loop needs a maintainer who will act on the report.
        # `own` means you wrote it, which is exactly the case for a private library: you
        # use it yourself and you can land the fix. A third-party tool gets an upstream
        # issue, not a private report nobody reads.
        if t.get("ring") != "own":
            errs.append(
                "[feedback] is only for ring=own tools (tools you wrote - the ones you use "
                "yourself and can fix)"
            )
        if not fb.get("dir"):
            errs.append("[feedback] needs `dir` (relative to the radar root)")
        # The consumer of a rendered target writes reports about a checkout and
        # reads a version out of its manifest, so a remote URL is not enough.
        if str(t.get("repo", "")).startswith(("http://", "https://", "git@")) and not fb.get(
            "worktree"
        ):
            errs.append(
                "[feedback] needs `worktree` (absolute local checkout) when `repo` is a remote URL"
            )
        for k in ("worktree", "format_doc", "triage_template", "index_builder"):
            v = fb.get(k)
            if v is not None and not isinstance(v, str):
                errs.append(f"[feedback].{k} must be a string")
        ex = fb.get("extras")
        if ex is not None and (not isinstance(ex, list) or not all(isinstance(s, str) for s in ex)):
            errs.append("[feedback].extras must be a list of strings")

    tel = t.get("telemetry")
    if tel is not None:
        # `match` sees MCP tool names; `match_skill` sees a Skill call's input.skill,
        # which is the only way an own-ring tool (reached through its skills) is
        # visible in a transcript. Either alone is a usable matcher.
        # Three matchers because there are three ways a tool appears in a transcript, and
        # a tool invisible to all three cannot have measurable exit criteria:
        #   match         MCP tool names        (artifact = mcp-server)
        #   match_skill   a Skill call's input.skill   (own-ring tools, reached by skill)
        #   match_command the executable heading a Bash/PowerShell command  (artifact = cli)
        # Without the third, telemetry sees no CLI use at all, so a stack made mostly of
        # CLIs would be almost invisible to it.
        if not tel.get("match") and not tel.get("match_skill") and not tel.get("match_command"):
            errs.append(
                "[telemetry] needs one of `match` (tool-name globs), `match_skill` "
                "(skill-id globs) or `match_command` (CLI executable globs)"
            )
        for key in MATCHER_KEYS:
            v = tel.get(key)
            if v is not None and (
                not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v)
            ):
                # An empty or whitespace-only glob is the silent zero the closed key set
                # exists to prevent: it passes the "needs one matcher" check above while
                # matching nothing, so `radar field` reports zero measured use and the zero
                # reads as evidence rather than as a matcher that cannot fire.
                errs.append(
                    f"[telemetry].{key} must be a list of globs, none empty or "
                    "whitespace-only (an empty glob measures nothing and reads as a zero)"
                )
        since = tel.get("since")
        if not since:
            errs.append("[telemetry] needs `since` (YYYY-MM-DD)")
        elif not _is_history_date(since):
            # The first day of every report's window: a day that does not exist would
            # pass here and stop `radar field` on its first run instead.
            errs.append(f"[telemetry].since {since!r} is not a real day written YYYY-MM-DD")
        # Optional: the tool names the entry is expected to expose. Field telemetry can
        # only see what was used, so dead weight (exposed but never called) is
        # uncomputable unless the exposed surface is declared.
        exposed = tel.get("exposed")
        if exposed is not None and not isinstance(exposed, list):
            errs.append("[telemetry].exposed must be a list of full tool names")
        # Optional: the surfaces this tool ALSO runs on, which a transcript cannot see.
        # A git hook fires inside `git commit` and a CI job runs on a server; neither
        # writes a tool call anywhere. Without the declaration the census reads those
        # tools as barely-used and flags a thin opportunity surface that is false by
        # construction - a commit hook appears in few sessions while firing on every
        # commit. Declaring the surface turns the FLAG into a NOTE that names it.
        also = tel.get("also_runs_in")
        if also is not None and (
            not isinstance(also, list) or not all(isinstance(s, str) and s for s in also)
        ):
            errs.append(
                "[telemetry].also_runs_in must be a list of non-empty strings naming the "
                'surfaces a transcript cannot see (e.g. "pre-commit hook", "GitHub Actions")'
            )
        # OPPORTUNITY, which is the denominator use never had. Zero measured use has three
        # readings - the work never called for it, it was not mounted, or it was there and
        # the agent never reached for it - and a count of invocations cannot tell them
        # apart. These globs name the calls that mark a session where the tool WOULD have
        # applied: a WebFetch is an opportunity for a docs server, a Grep is an opportunity
        # for a semantic-search server. Same shape as the three use matchers, and matched
        # the same way, so `taken_ratio` compares like with like.
        #
        # Optional by design, and never guessed: an opportunity glob asserts "this call
        # means the tool was applicable", which is a judgement about the tool. Written
        # where the entry's own notes already say it, nowhere else - the same rule the
        # matchers themselves are under, for the same reason (a matcher that cannot mean
        # anything manufactures a number that reads as evidence).
        for key in OPPORTUNITY_KEYS:
            v = tel.get(key)
            if v is not None and (
                not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v)
            ):
                errs.append(
                    f"[telemetry].{key} must be a list of globs, none empty or whitespace-only"
                )

    ver = t.get("version")
    if ver is not None:
        pol = ver.get("policy")
        if pol not in VERSION_POLICIES:
            errs.append(
                f"[version].policy must be one of {'/'.join(VERSION_POLICIES)}, got {pol!r}"
            )
        if pol == "pin" and not ver.get("pinned"):
            errs.append("[version].policy=pin needs `pinned` (the exact version required)")
        if pol == "floor" and not ver.get("min"):
            errs.append("[version].policy=floor needs `min` (the lowest acceptable version)")
        # A policy other than `any` is only meaningful against a resolved registry, and a
        # registry resolved by NAME is the identity trap the gate forbids: querying
        # PyPI for a private tool's name returns somebody else's project, and comparing the
        # two reads as a version verdict while being two different pieces of software.
        if pol == "latest" and not t.get("registry"):
            errs.append(
                "[version].policy=latest needs `registry` - upstream cannot be resolved "
                "by name alone: a registry is taken from the tool's repository metadata, "
                "never found by matching a name"
            )
        for k in ("pinned", "min", "reason"):
            v = ver.get(k)
            if v is not None and not isinstance(v, str):
                errs.append(f"[version].{k} must be a string")

    px = t.get("pilot_exit")
    if px is not None:
        # Also allowed on ring=own. `own` states AUTHORSHIP, not exemption from
        # evidence — and an author is most biased about their own tool, so an own-ring
        # entry whose value is unproven should carry the same pre-registered criteria a
        # pilot does. Exempting them would leave the most biased claims unchecked.
        #
        # And allowed on any entry that WAS a pilot. Parking a pilot must not delete its
        # pre-registered criteria: they were written before the data existed, and that
        # is the only property that makes them worth anything. Forcing them out on
        # demotion would let a parked experiment come back later with fresh, conveniently
        # looser criteria — pre-registration laundering. The block stays as the record of
        # what was promised; the ring says whether it is currently being tested.
        was_pilot = any(h.get("ring") == "pilot" for h in (t.get("history") or []))
        if t.get("ring") not in ("pilot", "own") and not was_pilot:
            errs.append("[pilot_exit] is only for ring=pilot/own tools, or one previously piloted")
        for k in ("review_after_days", "adopt_if", "decline_if"):
            if not px.get(k):
                errs.append(
                    f"[pilot_exit] needs `{k}` — pre-registered BEFORE the field data exists"
                )
        if px.get("review_after_days") is not None and not isinstance(
            px.get("review_after_days"), int
        ):
            errs.append("[pilot_exit].review_after_days must be an integer (days)")
        # Optional numeric thresholds. `adopt_if`/`decline_if` are prose and are decided
        # by a human; these give `radar field` the mechanically checkable part, so a
        # number is compared against a pre-registered number rather than an intention.
        for k in ("min_sessions_with_use", "min_invocations"):
            v = px.get(k)
            if v is not None and not isinstance(v, int):
                errs.append(f"[pilot_exit].{k} must be an integer")
        rate = px.get("max_error_rate")
        if rate is not None and (not isinstance(rate, (int, float)) or not 0 <= float(rate) <= 1):
            errs.append("[pilot_exit].max_error_rate must be a fraction in 0..1")

    return errs


# THE HISTORY RULE. An entry's ring is a claim, and `[[history]]` is the record that backs
# it: one block per ring change, oldest first, the newest one agreeing with `ring`. Every
# block carries a `date` (YYYY-MM-DD) and a `reason`, because an undated change cannot be
# ordered against anything and an unexplained one cannot be argued with. A block that
# moves an entry to `adopt` or `pilot` also carries `evidence` - a link, a run, a report -
# because those are the two rings that claim something was tried and held up, and a claim
# with nothing a reader can check is the one the gate exists to refuse. `observe`,
# `discard` and `own` state a position or an authorship rather than a result, so they
# need a reason and no evidence. Each missing field is a schema error, so the gate FAILs.
EVIDENCE_RINGS = ("adopt", "pilot")
_HISTORY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_history_date(value: object) -> bool:
    """A `YYYY-MM-DD` string that names a real day, or a TOML date (`date = 2026-09-01`
    unquoted parses to one, and it is the same fact written without quotes)."""
    if isinstance(value, datetime.datetime):
        return False
    if isinstance(value, datetime.date):
        return True
    # Unstripped: a padded " 2026-09-01 " is not the form, and the renderers compare and
    # print the string as written.
    if not isinstance(value, str) or not _HISTORY_DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def history_errors(t: dict) -> list[str]:
    """Schema errors in an entry's `[[history]]`. Empty list == the record backs the ring."""
    hist = t.get("history")
    if not hist:
        return ["empty history - every entry records how it reached its ring"]
    if not isinstance(hist, list) or not all(isinstance(h, dict) for h in hist):
        return ["`history` must be written as [[history]] blocks (a list of tables)"]
    errs: list[str] = []
    # Blocks run oldest first, so the last block is the newest: the one whose ring the
    # entry's must equal, and the one every renderer lists as the latest move.
    previous: datetime.date | None = None
    for n, block in enumerate(hist, 1):
        ring = block.get("ring")
        where = f"history block {n} (ring {ring!r})"
        when = block.get("date")
        if not _is_history_date(when):
            errs.append(f"{where} needs `date` in YYYY-MM-DD form")
        else:
            day = when if isinstance(when, datetime.date) else datetime.date.fromisoformat(when)
            if previous is not None and day < previous:
                errs.append(
                    f"{where} is dated {day}, before the block above it ({previous}): blocks "
                    "run oldest first, so the last one is the newest"
                )
            previous = day
        reason = block.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errs.append(f"{where} needs a non-empty `reason` - why the entry moved")
        evidence = block.get("evidence")
        if ring in EVIDENCE_RINGS and (not isinstance(evidence, str) or not evidence.strip()):
            errs.append(
                f"{where} needs non-empty `evidence` - a move to adopt or pilot rests on "
                "something a reader can check (a link, a run, a report)"
            )
    if hist[-1].get("ring") != t.get("ring"):
        errs.append(f"ring {t.get('ring')!r} != last history ring {hist[-1].get('ring')!r}")
    return errs


def missing_pilot_exit(t: dict) -> bool:
    """A pilot without exit criteria is how a pilot becomes a parking lot.

    Kept out of validate() (which is schema-shaped) and enforced by the gate, where it
    is a FAIL. The criteria are written before the field data exists, which is the only
    property that makes them worth anything, and the cost of compliance is one block per
    new experiment.
    """
    return t.get("ring") == "pilot" and t.get("pilot_exit") is None


# Which artifact kinds a transcript can see at all. The three matcher kinds read MCP
# tool names, a Skill call's `skill`, and the executable heading a shell command - so a
# tool is measurable exactly when it reaches the session as one of those.
#
# The rest are not oversights: a `python-lib` is imported and leaves no tool call, a
# `cc-lsp` is driven natively by the harness, a `standard` is a format. Demanding a
# matcher there produces one that cannot match, and a matcher that cannot match is
# worse than none - it manufactures a zero that reads as evidence: a library measured
# with an MCP glob, a language server piloted with no instrument at all. That is why the
# exclusion is a rule rather than a habit.
MEASURABLE_ARTIFACTS = {"cli", "mcp-server", "cc-plugin", "corpus"}
MATCHER_KEYS = ("match", "match_skill", "match_command")

# The same three shapes, pointed at a different question: not "was the tool used" but
# "was there an occasion where it would have applied". Deliberately parallel to
# MATCHER_KEYS - one is the numerator, the other supplies a denominator that is not
# "every session on the machine".
OPPORTUNITY_KEYS = ("opportunity_match", "opportunity_match_skill", "opportunity_match_command")

# Every key a [telemetry] block may carry. Closed on purpose: a mistyped matcher name
# is not a syntax error in TOML, it is a silent zero. `match_commands` or `matches`
# would leave the entry with no matcher at all, the report would print 0 invocations,
# and the zero would read as evidence - the same false zero a matcher of the wrong
# kind produces by another route. The gate reports strays as WARN.
TELEMETRY_KEYS = (
    *MATCHER_KEYS,
    *OPPORTUNITY_KEYS,
    "since",
    "exposed",
    "also_runs_in",
)


def unknown_telemetry_keys(t: dict) -> list[str]:
    """Keys in [telemetry] that the miner will never read. Empty means clean."""
    tel = t.get("telemetry")
    if not isinstance(tel, dict):
        return []
    return sorted(k for k in tel if k not in TELEMETRY_KEYS)


def missing_telemetry(t: dict) -> bool:
    """An own/adopt entry that CAN be measured and declares no matcher.

    The rings are claims about observed state, and without a matcher nothing makes the
    claim checkable over time. Left unchecked, the strongest claims on the board - own
    and adopt - can be the least instrumented, and nothing reports it, because nothing
    looks.

    Same shape as missing_pilot_exit, and same reasoning: the cost of compliance is one
    block per entry, and it is paid when the entry is written rather than a year later
    when the transcripts that would have answered the question are gone. Claude Code
    prunes old transcripts, so an undeclared matcher is not a deferred measurement - it
    is a lost one.
    """
    if t.get("ring") not in ("own", "adopt"):
        return False
    if t.get("artifact") not in MEASURABLE_ARTIFACTS:
        return False
    # An explicit, machine-readable opt-out. A COMMENT documenting a deliberate absence
    # is invisible to the gate, so the absence would be either unenforced or a permanent
    # red, with no third option. The field carries the reason and the gate prints it on
    # every run, which is the point:
    # an undeclared gap goes quiet, a declared one keeps arguing for itself.
    if str(t.get("telemetry_absent") or "").strip():
        return False
    tel = t.get("telemetry")
    return not tel or not any(tel.get(k) for k in MATCHER_KEYS)


# ---------------------------------------------------------------- version sites

# `## [X.Y.Z] - YYYY-MM-DD`, the single grammar every changelog the engine reads uses, so
# one parser can read every repository's changelog. `[Unreleased]` deliberately does not
# match: it names no version, so it cannot be a version site.
RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def release_headings(root: Path | None = None) -> list[tuple[str, str]]:
    """(version, date) for every released heading in CHANGELOG.md, newest first."""
    path = (root or data_root()) / "CHANGELOG.md"
    if not path.is_file():
        return []
    return RELEASE_HEADING.findall(path.read_text(encoding="utf-8"))


# `__version__ = "0.1.0"` at the top level of a module, read as TEXT rather than by
# importing it. Importing would answer for whichever copy of the package is on the path -
# which is precisely the wrong question when the tree being judged is a checkout that is
# not the installed one, and is no question at all in the fallback that runs the package
# straight off a PYTHONPATH. The site is a file, so it is read as a file.
_DUNDER_VERSION = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _package_version_site(base: Path) -> tuple[Path, str | None] | None:
    """The `src/<package>/__init__.py` of the project at `base`, and the version in it.

    `None` when the repository ships no package - which is the ordinary case for a data
    root, not a fault. The package is located from `[project].name`, the key that already
    says what gets built, with the PEP 503 spelling of `-` as `_`.
    """
    pyproject = base / "pyproject.toml"
    try:
        name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None
    init = base / "src" / str(name).replace("-", "_") / "__init__.py"
    if not init.is_file():
        return None
    found = _DUNDER_VERSION.search(init.read_text(encoding="utf-8"))
    return init, (found.group(1) if found else None)


def _normalized(name: str) -> str:
    """A distribution name in the PEP 503 form, which is how `uv.lock` spells it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_version_site(base: Path, name: str) -> str | None:
    """The version `uv.lock` records for the project itself, or `None` when it records none.

    The project's own entry is the `[[package]]` with the project's name whose source is
    the directory itself - `editable = "."` for a package, `virtual = "."` for a project
    that builds nothing. A dependency that happens to share the name comes from somewhere
    else and is not this site. A lock without that entry, or an entry without a version (a
    dynamic version), is a site that does not exist here rather than a disagreement.
    """
    lock = base / "uv.lock"
    try:
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package") or []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # A lock that does not parse is `uv lock --check`'s finding, not this site's.
        return None
    for package in packages:
        if not isinstance(package, dict) or _normalized(str(package.get("name"))) != name:
            continue
        source = package.get("source") or {}
        if source.get("editable") == "." or source.get("virtual") == ".":
            version = package.get("version")
            return str(version) if version else None
    return None


# An install line for a tagged git source: `git+https://github.com/<owner>/<dist>@vX.Y.Z`.
_INSTALL_LINE = re.compile(
    r"git\+https://github\.com/[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+?)(?:\.git)?@v(\d+\.\d+\.\d+)\b"
)


def _install_line_sites(base: Path, name: str) -> list[tuple[str, str]]:
    """`(where, version)` for every install line of THIS project in README.md and docs/.

    A line naming another repository is that repository's business and is skipped; the
    project is recognised by its distribution name. `where` is `path:line`.

    A document in another encoding is still searched: the pattern is plain ASCII, so
    replacing the bytes that do not decode as UTF-8 cannot hide or invent a match.
    """
    documents = [base / "README.md"]
    docs = base / "docs"
    if docs.is_dir():
        documents += sorted(docs.rglob("*.md"))
    found: list[tuple[str, str]] = []
    for path in documents:
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for match in _INSTALL_LINE.finditer(line):
                if _normalized(match.group(1)) == name:
                    found.append((f"{rel}:{lineno}", match.group(2)))
    return found


def version_site_errors(root: Path | None = None) -> list[str]:
    """Disagreements between the version sites. Empty list == they agree.

    THE ANCHOR is `[project].version`, and every other site is compared against it:

    - CHANGELOG.md's newest RELEASED heading, which says what the last release was.
    - `src/<package>/__init__.py`'s `__version__`, the site an installed copy answers from.
    - `uv.lock`'s entry for the project itself, which records the version the lock was
      resolved against.
    - Every install line in README.md and docs/**/*.md that installs THIS project from a
      tagged git source, because that line is what a reader copies: one left at the
      previous tag installs the previous release, and nothing about it looks wrong.

    A SITE THAT DOES NOT EXIST IS SKIPPED, and that is what lets one implementation serve
    two kinds of repository. The same verb is run against a data root, which has a
    `pyproject.toml` and a `CHANGELOG.md` and usually no package and no install lines;
    there, the missing sites are absent rather than failing. What is NOT skipped is a
    package that exists and declares no `__version__`: that is a site that has quietly
    stopped existing, and it is reported.

    `uv run` re-locks before it runs anything, so under `uv run` the lock comparison only
    confirms what `uv lock --check` - which CI runs first - already established. It bites
    where nothing re-locks: `uvx`, an installed `radar`, a direct call.

    One implementation, read from three places - tests/test_version_sites.py, the gate
    (so a scheduled review answers the question too) and the `check-version-sites` verb.
    Two implementations of one fact drift silently.
    """
    base = root or data_root()
    pyproject = base / "pyproject.toml"
    changelog = base / "CHANGELOG.md"
    errs: list[str] = []
    if not changelog.is_file():
        return [f"no CHANGELOG.md at {changelog} - there is no version site to compare against"]
    # The two anchor files are read defensively: an unreadable one is a finding that names
    # the file, not a traceback out of the verb and the gate.
    try:
        heads = release_headings(base)
    except (OSError, UnicodeDecodeError) as e:
        return [f"CHANGELOG.md could not be read ({type(e).__name__}) - it has to be UTF-8 text"]
    if not heads:
        return [
            "CHANGELOG.md carries no `## [X.Y.Z] - YYYY-MM-DD` heading - the version site "
            "this check compares against does not exist"
        ]
    if not pyproject.is_file():
        return [f"no pyproject.toml at {pyproject} - there is no declared version to compare"]
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [f"pyproject.toml could not be read as TOML ({type(e).__name__})"]
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        return [
            "pyproject.toml has no `[project].version` string - the anchor every other "
            "version site is compared against"
        ]
    declared = project["version"]
    name = _normalized(str(project.get("name") or ""))
    newest, date = heads[0]
    if declared != newest:
        errs.append(
            f"pyproject.toml says {declared}, CHANGELOG.md's newest released heading says "
            f"{newest} (dated {date}) - a release rolls both in one metadata-only commit"
        )
    site = _package_version_site(base)
    if site is not None:
        init, in_package = site
        rel = init.relative_to(base).as_posix()
        if in_package is None:
            errs.append(
                f"{rel} declares no `__version__` - a package that ships carries its "
                "version in its own namespace, and this is the site an installed copy "
                "answers from. A site that stops existing stops disagreeing, silently"
            )
        elif in_package != declared:
            errs.append(
                f"pyproject.toml says {declared} and {rel} says {in_package} - they are "
                "the same fact, and a release rolls every site in one metadata-only commit"
            )
    if name:
        locked = _lock_version_site(base, name)
        if locked is not None and locked != declared:
            errs.append(
                f"pyproject.toml says {declared} and uv.lock records {locked} for the project "
                "itself - run `uv lock` and commit the result with the version bump"
            )
        for where, installs in _install_line_sites(base, name):
            if installs != declared:
                errs.append(
                    f"{where} installs v{installs} of this project and pyproject.toml says "
                    f"{declared} - a reader who copies the line gets the other release"
                )
    versions = [tuple(int(p) for p in v.split(".")) for v, _ in heads]
    if versions != sorted(versions, reverse=True):
        errs.append(
            f"the release headings are not in descending version order: {versions} - a "
            "heading that does not move the version forward lets one version name two trees"
        )
    return errs


def validate_environment(e: dict) -> list[str]:
    errs = []
    if not e.get("name"):
        errs.append("missing field: name")
    rings = e.get("rings")
    if not rings:
        errs.append("missing field: rings (which rings this environment converges)")
    else:
        for r in rings:
            if r not in RINGS:
                errs.append(f"bad ring in rings: {r}")
    for key in ("require_env", "exclude"):
        v = e.get(key)
        if v is not None and not isinstance(v, list):
            errs.append(f"{key} must be a list")
    # Secrets never live here — only the NAMES of required variables.
    for name in e.get("require_env") or []:
        if "=" in str(name):
            errs.append(f"require_env holds variable NAMES only, never values: {name!r}")
    return errs


# ------------------------------------------------------------------- convergence


def tools_for_environment(env: dict, tools: list[dict] | None = None) -> list[dict]:
    """The tools an environment wants converged, after ring + exclude + per-tool opt-in."""
    tools = tools if tools is not None else load_tools()
    rings = set(env.get("rings") or [])
    excluded = set(env.get("exclude") or [])
    name = env.get("name")
    out = []
    for t in tools:
        # Existence scope first: a tool that does not exist here is not a candidate at
        # all, whatever its ring says. Checked before rings so a site-specific entry never
        # has to pretend to a ring to stay out of the other environments.
        exists_in = t.get("environments")
        if exists_in and name not in exists_in:
            continue
        if t.get("ring") not in rings or t.get("name") in excluded:
            continue
        inst = t.get("install") or {}
        wanted = inst.get("environments")
        if wanted and name not in wanted:
            continue
        out.append(t)
    return out
