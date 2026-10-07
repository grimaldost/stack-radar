"""Scoped-name redaction: keep the feedback, drop the name.

THE PROBLEM THIS SOLVES
-----------------------
One rule says the NAME of a private tool must not travel in a tracked repository.
Another says the feedback reports live in the catalogue so they can be synced and
triaged across environments. Those two collide the moment a report about one of your
own tools describes work done on a private project.

Resolving it by not archiving those reports would throw away part of the corpus to
protect a string. Resolving it by archiving them as written puts the string in git. So
neither: the corpus is archived with the name mapped to a stable placeholder, on the way
IN to the control plane.

    inbox (data plane, real names)  --redact-->  archive (git, placeholders)

The mapping lives in the scoped entry itself, under tools.local/, which is
gitignored - so the archive is readable everywhere and reversible only on a machine
that already holds the entry. A reader without it sees `local-lib` and learns
nothing; a reader with it substitutes back and reads the original.

WHAT IS AND IS NOT REDACTED
---------------------------
Only entries loaded from tools.local/ - the "the name cannot travel" mechanism.
An entry in tools/ scoped with `environments = [...]` chose the other trade-off - the
entry is limited to one environment, but its name is not secret: it travels, and its
name is in the repo by design. Redacting it would be redacting a name that is already
committed one directory away.

Both file CONTENT and file NAMES are mapped, because a report called
`<date>-<name>-v1.md` leaks from the directory listing alone.

Deliberately NOT redacted: short prefixes that identify nothing to a third party.
Mapping them would make the corpus unreadable to buy no privacy. That is a judgement,
so it is written down rather than left implicit.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from .paths import data_root, local_overlay
from .radar_lib import load_tools, placeholder_error

# What a scoped entry looks like:
#   redact_as   = "local-lib"            # the placeholder that lands in the archive
#   redact_also = ["othersspelling"]     # extra spellings that mean the same tool
REDACT_AS = "redact_as"
REDACT_ALSO = "redact_also"


def scoped_terms(tools: list[dict] | None = None) -> list[tuple[str, str]]:
    """[(term, placeholder)] for every machine-local entry, longest term first.

    Longest-first matters whenever one spelling of a tool contains another - a
    hyphenated name and its unhyphenated form, say. A shortest-first pass would
    rewrite the tail of the longer spelling and leave a half-mapped hybrid behind,
    which is worse than either outcome: still a leak, and no longer greppable.
    """
    tools = tools if tools is not None else load_tools()
    pairs: list[tuple[str, str]] = []
    for t in tools:
        if not t.get("_local"):
            continue
        name = str(t.get("name") or "")
        if not name:
            continue
        placeholder = str(t.get(REDACT_AS) or "").strip()
        if placeholder_error(t, placeholder):
            # No declared placeholder, or one the entry may not use (radar_lib.
            # placeholder_error), is a configuration error, not a reason to emit the real
            # name, to let a separator into a file name the placeholder replaces part of,
            # or to write the name back. The gate reports it; here the generic placeholder
            # stands in.
            placeholder = "scoped-tool"
        terms = [name] + [str(x) for x in (t.get(REDACT_ALSO) or [])]
        for term in terms:
            if term:
                pairs.append((term, placeholder))
    return sorted(pairs, key=lambda p: -len(p[0]))


def _compiled(
    terms: list[tuple[str, str]],
) -> tuple[re.Pattern[str], dict[str, str]] | None:
    if not terms:
        return None
    lookup = {t.lower(): p for t, p in terms}
    pattern = re.compile("|".join(re.escape(t) for t, _ in terms), re.IGNORECASE)
    return pattern, lookup


def redact_text(text: str, terms: list[tuple[str, str]] | None = None) -> str:
    """Map every scoped term in `text` to its placeholder. Case-insensitive."""
    c = _compiled(terms if terms is not None else scoped_terms())
    if c is None:
        return text
    pattern, lookup = c
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def redact_name(name: str, terms: list[tuple[str, str]] | None = None) -> str:
    """Same mapping, for a filename. Separate function so callers cannot forget one."""
    return redact_text(name, terms)


def has_scoped(text: str, terms: list[tuple[str, str]] | None = None) -> list[str]:
    """The scoped terms present in `text`. Empty means clean. Used by the gate."""
    c = _compiled(terms if terms is not None else scoped_terms())
    if c is None:
        return []
    pattern, _ = c
    return sorted({m.group(0).lower() for m in pattern.finditer(text)})


# ------------------------------------------------------------------ the home directory
#
# The other thing a mined report carries without anyone writing it: the machine's home
# directory, and with it the account name. It arrives in two shapes. As a PATH -
# `claude_home`, `[paths].transcript_index` - in the native spelling, the forward-slash
# one, and in a JSON report the escaped one, where every backslash is doubled and the
# native spelling never occurs at all. And as a SLUG: Claude Code files a session under
# its working directory with every character outside ASCII letters and digits turned into
# `-`, so a session run anywhere under the home is filed under a name that starts with the
# home in that spelling.
#
# Both map to `~`, which leaves a slug reading `~-Documents-<project>`: the part a reader
# needs, without the part that names the machine.
#
# The function is HANDED the home rather than finding it. Only `bootstrap.home_dir()` looks
# the home up (CONTRIBUTING), and a pure function is one the tests can hand a Windows home
# on Linux and the other way round.

HOME_PLACEHOLDER = "~"

# A path spelling is the home only when it is not the head of a longer name: the
# account's `-old` sibling, `.bak` copy or `2` successor is someone else's directory. A
# trailing `.` that ends a sentence still counts as the end of the path.
_PATH_HEAD = r"(?<![\w.-])"
_PATH_TAIL = r"(?![\w-]|\.\w)"
# In a slug the separator IS `-`, so only a letter or digit extends the name. The head
# admits `-` because a scratchpad slug nests a second, whole slug after one.
_SLUG_HEAD = r"(?<![A-Za-z0-9])"
_SLUG_TAIL = r"(?![A-Za-z0-9])"


def slug_form(path: PurePath) -> str:
    """How Claude Code spells `path` as a project-folder name."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _home_pattern(home: PurePath | None) -> re.Pattern[str] | None:
    # A bare anchor - `/`, `C:\` - names no account, and mapping it would turn every path
    # in the report into `~`. Declining is the honest answer, not a guess at the account.
    if home is None or len(home.parts) < 2 or not home.name:
        return None
    paths = {str(home), home.as_posix()}
    paths |= {json.dumps(p)[1:-1] for p in list(paths)}
    alternatives = [
        f"{_PATH_HEAD}{re.escape(p)}{_PATH_TAIL}" for p in sorted(paths, key=len, reverse=True)
    ]
    alternatives.append(f"{_SLUG_HEAD}{re.escape(slug_form(home))}{_SLUG_TAIL}")
    # Case-insensitive: a Windows path is, and the drive letter of a slug follows however
    # the working directory was typed, so either case can appear.
    return re.compile("|".join(alternatives), re.IGNORECASE)


def redact_home(text: str, home: PurePath | None) -> str:
    """Map every spelling of `home` in `text` to `~`. None, or a bare root, maps nothing."""
    pattern = _home_pattern(home)
    if pattern is None:
        return text
    return pattern.sub(HOME_PLACEHOLDER, text)


# ------------------------------------------------------- the homes this machine is not
#
# A report written on ANOTHER workstation and synced here carries that machine's home, and
# `Path.home()` cannot find it: the account is not this one's. So the other homes are
# DECLARED, in a file the data root keeps out of git - an account name is exactly the kind
# of string this module exists to withhold, and a tracked declaration would publish it in
# order to say it must not be published.
#
# A FILE OF ITS OWN rather than an entry under `tools.local/`, which is the other
# gitignored declaration in a catalogue: everything in that directory is parsed as a
# CATALOGUE ENTRY (`radar_lib.load_tools` tags each one `_local`, the entry schema
# validates it and the gate judges it), so a home declared there would have to be given a
# name, a ring and an axis to be allowed to sit in it. An account is not a tool.
#
# The declared homes go through `redact_home`, unchanged: one mapping, so a home this
# machine owns and one it does not are mapped to the same `~` by the same rules.

EXTRA_HOMES_FILE = "redact.local.toml"
EXTRA_HOMES_KEY = "homes"


def _declared(spelling: str) -> PurePath:
    """A declared home in the flavour it is WRITTEN in, not the one this machine runs.

    `PurePath` is the local flavour, so a Windows home declared on a POSIX runner would
    come back as a POSIX path and its native spelling - the one with backslashes - would
    never be matched, although that is the spelling most of a mined corpus uses. The
    flavour is therefore read off the string: a drive letter or a backslash means Windows.
    """
    s = spelling.strip()
    if re.match(r"^[A-Za-z]:[\\/]", s) or "\\" in s:
        return PureWindowsPath(s)
    return PurePosixPath(s)


def extra_homes(root: Path | None = None) -> list[PurePath]:
    """The home directories declared in `<data root>/redact.local.toml`.

    No file, no `homes` key, an empty list, blank entries: no extra homes and no error.
    The file is optional, and a catalogue whose reports were all written on one machine
    never needs one. A file that does not PARSE is a different thing and raises, because a
    declaration that could not be read must not look like one that declared nothing -
    that is the shape every silent leak in this module has taken.
    """
    # In a linked git worktree without its own copy, the main worktree's is read: the
    # file is gitignored, so git never copies it into a second checkout.
    f = local_overlay(root or data_root(), EXTRA_HOMES_FILE)
    if not f.is_file():
        return []
    with f.open("rb") as fh:
        declared = tomllib.load(fh).get(EXTRA_HOMES_KEY) or []
    # One home written as a bare string rather than a list of one: iterating it would
    # yield a home per CHARACTER, every one of them too short to match anything.
    if isinstance(declared, str):
        declared = [declared]
    return [_declared(s) for s in declared if isinstance(s, str) and s.strip()]


def redact_homes(text: str, homes: Iterable[PurePath | None]) -> str:
    """`redact_home` for each home in turn, LONGEST FIRST.

    Longest-first for the same reason `scoped_terms` sorts: where one declared home sits
    inside another, the shorter one matches first and leaves the tail of the longer spelled
    out, which still names the directory and is no longer greppable.
    """
    for home in sorted(homes, key=lambda h: len(str(h)) if h else 0, reverse=True):
        text = redact_home(text, home)
    return text
