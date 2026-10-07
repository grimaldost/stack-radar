"""Adoption gate: schema, evidence, licence, staleness/astroturf, and the invariant.

What the gate checks:
- downloads are the decisive signal; stars are never proof;
- registry identity comes from repo metadata, never name-matching;
- every ring change is a dated [[history]] block with a reason, and a move to
  adopt or pilot also names its evidence (schema errors, so FAIL);
- adopt/pilot entries must not go stale;
- the version sites agree;
- and the control-plane invariant holds: no data-plane file references
  this control plane.
Exit 1 on FAIL findings (schema errors, archived repos, invariant breaches);
WARN/FLAG/NOTE are informational.

TWO CLASSES OF CHECK, and the difference decides what a green run means.

Most of what this script asks is answerable from the repository alone: schema, rings,
evidence links, licences, staleness against the committed snapshot, telemetry keys,
pilot_exit rules, version sites. Two are not - the invariant scan and the
scope-leak scan - because each needs something that exists on a MACHINE: the data plane
(`~/.claude`, the governed tool worktrees) and the gitignored `tools.local/` entries.

In CI neither exists. A committed profile points at paths that are absent on a runner
by construction, and a gate that printed "invariant holds - 0 data-plane files scanned"
there would go green - a vacuous pass with the exact wording of a real one. So absence
is DETECTED and NAMED: the run says which checks did not run and why, and everything
repo-only still runs and can still fail. A gate that cannot say what it
skipped is a gate that will one day skip everything and report success.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from .paths import MARKER, data_root, main_worktree
from .radar_lib import (
    TELEMETRY_KEYS,
    axes_errors,
    begin_command,
    catalogue_axes,
    download_registry,
    entry_file_errors,
    github_repo,
    latest_snapshot,
    load_environments,
    load_marker,
    load_tools,
    missing_pilot_exit,
    missing_telemetry,
    reports_profile_errors,
    unknown_telemetry_keys,
    validate,
    version_site_errors,
)
from .redact import has_scoped, scoped_terms
from .trust import git

# The invariant's own falsification test, as code. A data-plane file naming the control
# plane would make a working session depend on this catalogue's presence.
#
# THE NEEDLE IS NOT A LITERAL IN THE CODE. A name written here would be true of one
# catalogue and invisible when it stopped being true. Two ways for it to go wrong, both
# silent and both printing "holds": a renamed catalogue is scanned for a name nothing
# carries, and a needle that is also the name of a published tool fails on every
# legitimate mention of that tool, which is how an operator learns to skip the line. So
# the names come from the catalogue that knows them, `radar.toml [invariant].needles`, and
# the code contributes only what it can derive: the catalogue's own absolute path.
# Session transcripts legitimately contain the catalogue's paths (work on the catalogue
# happens in sessions); they are a log, not a binding, so they are out of scope by
# construction.
#
# THESE APPLY TO THE `<claude_home>` DIRECTORY ROOTS ONLY, which are walked. The governed
# worktrees are enumerated by `git ls-files`, so what they exclude comes from their own
# `.gitignore`. The predicate below matches a path COMPONENT at any depth, so an entry here
# deletes every directory of that name in every root it is applied to. `feedback` is
# deliberately NOT in this set for exactly that reason: it would take a governed
# worktree's own `docs/feedback/` with it. It is excluded as one named root instead - see
# CLAUDE_HOME_OMITTED.
INVARIANT_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "projects",
    "todos",
    "shell-snapshots",
    "statsig",
}
INVARIANT_SUFFIXES = {
    ".md",
    ".toml",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".ps1",
    ".txt",
    ".yaml",
    ".yml",
}
# The `--fast` ceiling, per root - and NOT the default. As a default it would make the
# gate's own sentence false: on a claude_home with more eligible files than the cap, the
# scan reads only part of them and still prints "holds" with no caveat, which is a
# partial scan wearing a complete scan's words. The default is complete; `--fast` opts
# into the cap, and every root it cuts prints TRUNCATED so the two runs can never be
# confused.
INVARIANT_MAX_FILES = 6000

# THE THREE FILES THE ENGINE'S NAME MAY NOT APPEAR IN. Everywhere else
# in the data plane the engine is an ordinary published tool and naming it is ordinary -
# a note, a manifest, a shell history. These three are what makes a session RUN
# something, so they are where the rule that the radar is never an execution
# dependency stays checkable by grep rather than by reading. Mentioning the
# engine is fine; wiring a hook to it is not.
#
# Matched by basename, except the hooks file, which is only that file in that directory:
# `hooks.json` alone is a name anything could take.
EXECUTION_FILE_NAMES = ("settings.json", "CLAUDE.md")
EXECUTION_FILE_SUFFIX = "hooks/hooks.json"

# The data plane inside `<claude_home>`: an allow-list, never a walk of the whole
# directory, because `~/.claude` also holds transcripts, caches and shell snapshots, which
# are logs rather than bindings. These are the generic entries any Claude Code home can
# carry, plus `feedback-targets.toml`, which this engine's `radar render-feedback-targets`
# writes there. A catalogue adds its own, such as a file its own tooling writes there, in
# `radar.toml [invariant].claude_home_paths` (`declared_claude_home_paths()`), so beyond
# the generic entries the engine names only the file its own verb writes. A path, declared
# either way, that does not exist yet is skipped in silence: declaring it before it exists
# is what makes the scan reach it on the day it lands.
CLAUDE_HOME_SUBPATHS = (
    "settings.json",
    "CLAUDE.md",
    "skills",
    "plugins",
    "feedback-targets.toml",
)

# What the allow-list leaves out ON PURPOSE, with its reason, printed on every run. Same
# device as `[invariant].exempt` in the marker, and for the same reason: a declared
# omission keeps arguing for itself, while a name that is simply absent from a tuple is
# indistinguishable from an oversight.
#
# ASCII only, like every other printed string here: a Windows console may still use a
# legacy code page, and a run whose note comes out mojibake is a run nobody reads to the
# end.
CLAUDE_HOME_OMITTED = {
    "feedback": (
        "the mirrored feedback inbox - a report is a LOG of "
        "what a session wrote, the same kind of thing as the transcripts this scan "
        "already skips, "
        "and the reports that carry the name are mirrored records rather than bindings. "
        "Excluded as this one root and not as a path component: the component predicate "
        "matches at any depth and would drop a governed worktree's own `docs/feedback/` "
        "from a scan that promises complete coverage."
    ),
}


class Root(NamedTuple):
    """One place the invariant scan reads, and how it is enumerated.

    `kind` is not cosmetic: a worktree is asked of git and a failure there is a FAIL,
    while a `<claude_home>` root is not a checkout, is walked, and is never a FAIL for
    not being one.
    """

    path: Path
    origin: str  # what declared it, for the per-root line: a tool name, or the marker
    kind: str  # "file" (read whole) | "dir" (walked) | "worktree" (git ls-files)


# KNOWN EXEMPTIONS live in the marker, not here: `radar.toml [invariant.exempt]`, read by
# `declared_exempt()` below, because the list is data. An exemption is not a general
# escape hatch: each one prints as a NOTE on every run precisely so it cannot quietly
# become permanent, and the rule stays "zero references" for everything else.

COPYLEFT = ("GPL", "LGPL", "AGPL", "CC-BY-SA")
SOURCE_AVAILABLE = ("Elastic", "BUSL", "FSL", "PolyForm")
STALE_DAYS = 180
ASTROTURF_RATIO = 0.05  # downloads/month below 5% of stars, with stars > 5k -> flag
ASTROTURF_MIN_STARS = 5000


def profile_paths(override: str | None = None) -> list[dict[str, str]]:
    """Each profile's [paths], with the overlay it names layered on (`load_environments`),
    forward-slashed, with claude_home overridden.

    A `repo` or a [feedback].worktree is written with the profile's placeholders (for
    example `{documents}/<tool>`), because one entry has to resolve on every machine. So a
    root can only be resolved PER PROFILE, and the same entry legitimately resolves on one
    profile and nowhere on the others - which is the normal case, not an error.
    """
    out: list[dict[str, str]] = []
    for env in load_environments():
        paths = {
            str(k): str(v).replace("\\", "/")
            for k, v in (env.get("paths") or {}).items()
            if isinstance(v, str) and v
        }
        if override:
            paths["claude_home"] = str(override).replace("\\", "/")
        out.append(paths)
    if override and not out:
        out.append({"claude_home": str(override).replace("\\", "/")})
    return out


def claude_homes(override: str | None = None) -> list[Path]:
    """Every profile's claude_home, or the single override, as paths (existing or not)."""
    if override:
        return [Path(str(override).replace("\\", "/"))]
    return [Path(p["claude_home"]) for p in profile_paths() if p.get("claude_home")]


def expand(value: str, paths: dict[str, str]) -> str:
    """Fill the profile's `{key}` placeholders, the way an [install] command is filled.

    An unknown key is left written as it stands rather than guessed at. That is what
    makes it reportable: `{documents}/<tool>` with no `documents` in the profile is not a
    directory, so the entry lands in the unresolved list with the string it tried.
    """
    out = str(value).replace("\\", "/")
    for key, v in paths.items():
        out = out.replace("{" + key + "}", v)
    return out


# A `{key}` that `expand` left as written because the profile does not declare it.
UNFILLED = re.compile(r"\{[A-Za-z0-9_]+\}")


def declared_needles(marker: dict) -> list[str]:
    """`radar.toml [invariant].needles`, cleaned. The catalogue's own name(s), as data.

    Empty is a legal catalogue and a WEAKER scan, which is the whole reason the caller
    says so out loud: the data root's absolute path is still a needle, so the run still
    prints "holds" and the sentence is still true - just about half of what the reader
    assumes it is about.
    """
    raw = (marker.get("invariant") or {}).get("needles") or []
    if isinstance(raw, str):  # one name written without the brackets
        raw = [raw]
    return [str(n).strip() for n in raw if isinstance(n, str) and str(n).strip()]


def declared_claude_home_paths(marker: dict) -> tuple[list[str], list[str]]:
    """`radar.toml [invariant].claude_home_paths`, cleaned, and notes on what was dropped.

    Sub-paths RELATIVE to each profile's claude_home, scanned in addition to the engine's
    generic CLAUDE_HOME_SUBPATHS. They are data for the same reason the needles are: which
    files a catalogue's own tooling writes into the data plane is true of that catalogue,
    and a constant here would be true of one setup and dead for every other.

    An absolute path or one that climbs out with `..` is dropped and named, never
    followed: the list says where INSIDE claude_home to look, and a value that points
    elsewhere is a mistake the operator should see rather than a scan of somewhere else.
    """
    raw = (marker.get("invariant") or {}).get("claude_home_paths") or []
    if isinstance(raw, str):  # one path written without the brackets
        raw = [raw]
    if not isinstance(raw, list):
        return [], [
            f"[NOTE] invariant: `[invariant].claude_home_paths` is {type(raw).__name__}, not "
            "a list of sub-paths - IGNORED; only the generic claude_home files are scanned"
        ]
    kept: list[str] = []
    notes: list[str] = []
    for value in raw:
        sub = str(value).strip().replace("\\", "/") if isinstance(value, str) else ""
        if not sub:
            notes.append(
                f"[NOTE] invariant: claude_home_paths entry {value!r} is not a path - IGNORED"
            )
            continue
        if sub.startswith("/") or re.match(r"^[A-Za-z]:", sub) or ".." in sub.split("/"):
            notes.append(
                f"[NOTE] invariant: claude_home_paths entry {sub!r} is not a path inside "
                "claude_home (absolute, or climbing out with `..`) - IGNORED"
            )
            continue
        if sub not in kept and sub not in CLAUDE_HOME_SUBPATHS:
            kept.append(sub)
    return kept, notes


def declared_exempt(marker: dict) -> tuple[dict[str, str], list[str]]:
    """`radar.toml [invariant].exempt`, as {path-suffix: reason}.

    Returns (exemptions, notes-about-what-was-discarded). A SECOND RETURN VALUE rather than
    a quietly smaller dict, for the reason every check in this file gives one: a declaration
    the code dropped looks exactly like a declaration nobody wrote, and the operator finds
    out when the gate goes red on the file they thought they had exempted.

    DATA, FOR THE SAME REASON THE NEEDLES ARE. A constant here would be true of one
    catalogue and invisible on the day it stops being. Worse, an exemption spells out a
    path inside a repository that is not this one, so as a constant it would put a name
    that belongs to one catalogue into the engine's source - what the publication check
    exists to refuse.

    The distinction the grep cannot make: a file that lists the needle among terms it
    FORBIDS is asserting the radar's absence, not depending on its presence. That reading
    is true of a given file and would be an easy lie elsewhere, so exemptions are named one
    at a time, by the catalogue that knows them, rather than inferred from context.

    A key is an `endswith` suffix. Prefer dir+basename over a worktree-rooted path when the
    same module is served from more than one place - a worktree AND every versioned copy in
    a plugin cache, say - or the key matches the original and misses the copies.

    Empty is legal and means no exemption, which is the stricter reading.
    """
    raw = (marker.get("invariant") or {}).get("exempt") or {}
    if not isinstance(raw, dict):
        return {}, [
            f"[NOTE] invariant: `[invariant].exempt` is {type(raw).__name__}, not a table - "
            'IGNORED. It is written `[invariant.exempt]` with one `"path/suffix" = '
            '"reason"` row per exemption. No file is exempt while it is the wrong shape, so '
            "the scan is STRICTER than declared rather than looser, and says so here."
        ]
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for k, v in raw.items():
        key = str(k).strip() if isinstance(k, str) else ""
        if not key:
            dropped.append("[NOTE] invariant: an exemption with an empty key - IGNORED.")
            continue
        if not isinstance(v, str):
            # A nested table is the typo this catches: `[invariant.exempt.foo]` parses, and
            # without this branch it would become a LIVE exemption keyed `foo` whose reason
            # is the repr of a dict - an accountability mechanism turned into noise by a typo.
            dropped.append(
                f"[NOTE] invariant: exemption {key!r} has a {type(v).__name__} where a reason "
                "string belongs - IGNORED. A nested `[invariant.exempt.<key>]` table is the "
                "usual cause; the row is written inside `[invariant.exempt]`, not under it."
            )
            continue
        reason = v.strip()
        if not reason:
            dropped.append(
                f"[NOTE] invariant: exemption {key!r} declares no reason - IGNORED. The reason "
                "is the whole accountability of an exemption; it is printed on every run."
            )
            continue
        kept[key] = reason
    return kept, dropped


def _same_path(a: Path, b: Path) -> bool:
    """normcase, not lower(): case folding is right on Windows and wrong on POSIX."""
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def same_checkout(declared: str, other: str, profiles: list[dict[str, str]]) -> bool:
    """Do two DECLARED paths name the same directory under some profile?

    Both sides are written with the profile's placeholders - an entry's `repo` reads like
    `{documents}/<checkout>`, and `[publish].framework_worktree` is written the same way -
    so a literal string compare is not merely fragile, it never matches at all. Expand
    under each profile and resolve both sides.

    Existence is deliberately NOT required. The comparison answers "are these the same
    checkout", which is true on a machine that has not cloned it, and the publication
    check's exception has to hold there too or it holds only where it is not needed.
    """
    if not declared or not other:
        return False
    if str(declared).startswith(("http://", "https://", "git@")):
        return False
    for paths in profiles:
        a, b = expand(str(declared), paths), expand(str(other), paths)
        # A relative path names no checkout - the data-plane roots and the publication
        # check refuse one - so it is never the same checkout as anything.
        if not (os.path.isabs(a) and os.path.isabs(b)):
            continue
        if _same_path(Path(a), Path(b)):
            return True
    return False


def framework_entry(
    tools: list[dict], worktree: str, profiles: list[dict[str, str]]
) -> dict | None:
    """The `own` entry whose `repo` IS the declared engine checkout, or None.

    BY IDENTITY, NEVER BY NAME, and that is the load-bearing word. The checkout being
    published can itself be an `own` entry in the catalogue, under its own name, so a
    name-matching exception would exempt any entry that happened to be called
    the same thing - and would still have to be told the name from somewhere. Matching on
    `repo` means one fact decides it and there is no second key to drift.

    The consequence binds the entry itself: while the exception
    matches on `repo`, the engine's entry stays `visibility = "private"`, because the
    public-entry rule forces `repo` to a URL and a URL is not a checkout.
    """
    for t in tools:
        if t.get("ring") != "own":
            continue
        if same_checkout(str(t.get("repo") or ""), worktree, profiles):
            return t
    return None


def is_execution_file(p: Path) -> bool:
    """One of the three files the engine's name is kept out of."""
    return p.name in EXECUTION_FILE_NAMES or p.as_posix().endswith(EXECUTION_FILE_SUFFIX)


def data_plane_roots(
    tools: list[dict],
    claude_home: str | None = None,
    subpaths: tuple[str, ...] | list[str] = CLAUDE_HOME_SUBPATHS,
) -> tuple[list[Root], list[str]]:
    """The places the invariant covers, AND the `own` entries that did not become one.

    Empty roots means there is no data plane reachable from here - a CI runner, or a
    fresh clone that has not been bootstrapped. The caller must treat that as "not
    checked", never as "checked and clean"; main() does.

    The second half of the return value exists because the first half can lie by
    omission. A `repo` written as `{documents}/<tool>` is not a directory until the
    placeholder is expanded, and a root that silently fails to resolve drops out of the
    scan without a word while the gate still says "holds" over the rest. A root that cannot
    be resolved has to say so by name, so that an absent root shows up in the note instead
    of simply not existing.
    """
    profiles = profile_paths(claude_home)
    here = data_root()
    roots: list[Root] = []
    seen: set[str] = set()

    def add(path: Path, origin: str, kind: str) -> None:
        # Two profiles can name the same literal directory, and an entry can repeat one.
        # normcase, not lower(): case folding is right on Windows and wrong on POSIX.
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            roots.append(Root(path, origin, kind))

    for base in claude_homes(claude_home):
        for sub in subpaths:
            p = base / sub
            if p.exists():
                add(p, f"<claude_home>/{sub}", "file" if p.is_file() else "dir")

    unresolved: list[str] = []
    for t in tools:
        # Every root of this kind comes from an `own` entry: [feedback] is only valid
        # there (radar_lib.validate), and a `repo` is only read as a worktree there.
        if t.get("ring") != "own":
            continue
        name = str(t.get("name") or t.get("_file"))
        where = str(t.get("_file"))
        declared = (t.get("feedback") or {}).get("worktree") or t.get("repo")
        if not declared:
            unresolved.append(
                f"{name} ({where}): neither `repo` nor [feedback].worktree names a "
                "local checkout, so there is no tree to scan"
            )
            continue
        if str(declared).startswith(("http://", "https://", "git@")):
            unresolved.append(
                f"{name} ({where}): `repo` is a remote URL only and the entry declares no "
                "[feedback].worktree, so there is no local tree to scan"
            )
            continue
        tried: list[str] = []
        resolved = is_self = relative = False
        # With no profile at all the value is still read once, unexpanded, so a relative
        # one is refused by name rather than reported as tried nowhere.
        for paths in profiles or [{}]:
            raw = expand(str(declared), paths)
            cand = Path(raw)
            tried.append(cand.as_posix())
            # A RELATIVE `repo`/worktree is refused, never enumerated, wherever it points:
            # read against the data root it can name a directory the catalogue itself
            # commits (a `vendor/evil` laid out as a git repository), and enumerating that
            # means running git on a tree a pull request controls. A governed checkout is
            # a separate tree, named by an absolute path or a `{documents}` placeholder
            # that expands to one. `.` is the data root itself, and is named as such. A
            # placeholder this profile does not declare is not enumerated either; the
            # reason names what was tried.
            if not os.path.isabs(raw):
                if UNFILLED.search(raw):
                    continue
                if (here / cand).resolve() == here:
                    is_self = True
                else:
                    relative = True
                continue
            if not cand.is_dir():
                continue
            if cand.resolve() == here:
                is_self = True
                continue
            resolved = True
            add(cand, name, "worktree")
        if resolved:
            continue
        if relative:
            unresolved.append(
                f"{name} ({where}): {declared} is a relative path, and a relative path is "
                "refused - read against the data root it can name a directory this "
                "catalogue commits, so it is not enumerated. Name the checkout by an "
                "absolute path or a profile placeholder"
            )
        elif is_self:
            unresolved.append(
                f"{name} ({where}): resolves to this catalogue - the control plane is "
                "not part of the data plane it governs"
            )
        else:
            unresolved.append(
                f"{name} ({where}): {declared} is not a directory in any profile "
                f"(tried {', '.join(sorted(set(tried)))})"
            )
    return roots, unresolved


def unreachable_homes(homes: list[Path], subpaths: tuple[str, ...] | list[str]) -> str:
    """Why no claude_home contributed a root, one clause per home, saying what is true.

    A home that EXISTS and holds none of the scanned entries is a different finding from
    a home that is absent: calling the first "no claude_home" sends the reader to look for
    a path problem that is not there. So an existing home lists what was looked for.
    """
    if not homes:
        return "no profile declares a claude_home"
    looked_for = ", ".join(subpaths)
    clauses = []
    for home in homes:
        if home.is_dir():
            clauses.append(
                f"none of the scanned files exist under {home} (looked for: {looked_for})"
            )
        else:
            clauses.append(f"no claude_home exists at {home}")
    return "; ".join(clauses)


def worktree_refusal(root: Path, *, inside_ok: bool = False) -> str | None:
    """Why `root` is not handed to git for enumeration, or None when it may be.

    A tree a catalogue or a profile names is enumerated only when it holds a `.git` itself
    (the rule `repo_version` follows): a directory laid out as a bare repository has none,
    and neither has a directory inside a checkout, which git would enumerate through
    whatever repository it found above it. `inside_ok` is for the data root, the tree the
    gate is run in: whoever runs the gate chose it, so it may also sit anywhere inside a
    worktree, and git reads that worktree's repository.
    """
    if (root / ".git").exists():
        return None
    if inside_ok:
        if any((p / ".git").exists() for p in root.parents):
            return None
        return (
            f"{root} is not inside a git worktree - neither it nor a directory above it "
            "holds a .git"
        )
    return (
        f"{root} holds no .git; only the top level of a git worktree is enumerated, so a "
        "directory inside a checkout, or one laid out as a bare repository, is not read"
    )


def enumeration_failure(root: Path, *, inside_ok: bool = False) -> str:
    """Why `git_ls_files(root, inside_ok=...)` answered None, as a clause for a FAIL line."""
    return worktree_refusal(root, inside_ok=inside_ok) or (
        f"`git ls-files` failed in {root} - git is absent, or could not read the repository"
    )


def git_ls_files(root: Path, *, inside_ok: bool = False) -> list[Path] | None:
    """Every file git tracks under `root`, plus every un-ignored file it does not yet.

    None - never an empty list - when git could not answer at all: git absent, `root` is
    not a worktree, or it is not enumerable (`worktree_refusal`). The distinction is the
    whole point of the return type. A root that cannot be enumerated and comes back as
    `[]` prints "0 scanned of 0 eligible", which is the wording of a clean root; the caller
    turns None into a FAIL naming the root and `enumeration_failure`'s reason.

    git is run through `trust.git`, so the config a repository turns into a command is
    emptied (`GIT_SAFE_CONFIG`) and the program is found on PATH and refused if it is a
    batch file cmd.exe would read an argument of.

    `--others --exclude-standard` is not padding. With `--cached` alone a NEW file is
    invisible until it is committed, so a check first fires on the commit AFTER the one
    that introduced the leak.
    """
    if worktree_refusal(root, inside_ok=inside_ok):
        return None
    try:
        out = git(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [root / n for n in out.stdout.split(chr(0)) if n]


def walk_files(root: Path) -> list[Path]:
    """Every file under a `<claude_home>` root. These are not checkouts: there is no git
    to ask, so the directory is walked and INVARIANT_SKIP_DIRS carries the exclusions."""
    files: list[Path] = []
    for p in root.rglob("*"):
        if any(part in INVARIANT_SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            files.append(p)
    return files


def catalogue_locations() -> list[Path]:
    """Every absolute path this catalogue is checked out at, as far as the root can tell.

    The data root, and - when the data root is a linked git worktree - the main worktree
    too. Both are the same catalogue: a data-plane file that names the main checkout
    depends on this catalogue exactly as much as one that names the linked one does, and
    the main checkout is also where a linked worktree reads the gitignored `tools.local/`
    and `redact.local.toml` from (`paths.local_overlay`), so it is a needle whenever it
    exists.
    """
    here = data_root()
    main = main_worktree(here)
    return [here] if main is None else [here, main]


def location_spellings(location: Path) -> list[str]:
    """The ways a data-plane file can write `location`: with forward slashes, in the
    platform's own spelling, and in that spelling as a JSON or TOML string escapes it
    (every backslash doubled, as settings.json writes a Windows path)."""
    native = str(location)
    return list(dict.fromkeys([location.as_posix(), native, native.replace("\\", "\\\\")]))


def names_location(text: str, location: str) -> bool:
    """Whether `text` names the directory `location`, not merely a sibling of it.

    A path is matched only where it ends at a path boundary. A plain substring test would
    also match `<location>-other/x` or `<location>.bak`, which are other directories that
    share a prefix, and a checkout kept next to a governed tool of a similar name would
    FAIL for a reference it does not make. Anything but a name character after the path
    - a separator, a quote, whitespace, punctuation or the end of the text - ends it.

    On Windows the match ignores case, as the file system does: a path spelled in lower
    case names the same directory as the one spelled with capitals.
    """
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.search(re.escape(location) + r"(?![A-Za-z0-9._-])", text, flags) is not None


def check_invariant(
    roots: list[Root],
    names: list[str],
    framework_name: str | None = None,
    fast: bool = False,
    exempt: dict[str, str] | None = None,
) -> list[str]:
    """Grep the data plane for references to this catalogue. Returns FAIL messages.

    Takes the roots rather than computing them so the caller decides - and can see -
    whether there was a data plane to scan at all. It takes `names` for the same reason:
    they come from `radar.toml`, and a function that read them itself
    would be a second reader of the marker with its own idea of what an empty list means.

    `framework_name` is the engine's, and it is a needle in the three EXECUTION files
    only. None means the engine could not be identified - until somebody writes its
    `own` entry the catalogue has none - and the caller says so rather than letting the
    narrower check disappear into a green run.

    WHAT THE NOTE SAYS, and why it is a table rather than a word. A single number for
    the whole scan would make a root that contributed nothing look exactly like a clean
    one, and would hide what a per-root ceiling left unread behind the word "holds". So
    every root prints scanned/eligible under its own name, `--fast` marks the ones it cut
    with TRUNCATED, and a worktree git cannot enumerate is a FAIL naming the root rather
    than a quiet "0 of 0".
    """
    needles = tuple(names)
    locations = tuple(s for p in catalogue_locations() for s in location_spellings(p))
    exempt_map = exempt or {}
    fails: list[str] = []
    exempted: list[str] = []
    lines: list[str] = []
    scanned_total = eligible_total = 0
    started = time.monotonic()
    for root in roots:
        if root.kind == "worktree":
            found = git_ls_files(root.path)
            if found is None:
                fails.append(
                    f"[FAIL] invariant: data-plane root could not be enumerated "
                    f"({root.origin}) - {enumeration_failure(root.path)}. The root was NOT "
                    "scanned, which is not the same as scanning it clean"
                )
                lines.append(f"[NOTE]   {root.origin}: NOT ENUMERABLE - {root.path}")
                continue
            candidates = found
        elif root.kind == "dir":
            candidates = walk_files(root.path)
        else:
            # A file sub-path was named one by one, so it is eligible whatever it is
            # called: the allow-list already decided it belongs in the scan.
            candidates = [root.path]
        eligible = [
            p for p in candidates if root.kind == "file" or p.suffix.lower() in INVARIANT_SUFFIXES
        ]
        batch = eligible[:INVARIANT_MAX_FILES] if fast else eligible
        cut = " TRUNCATED (--fast)" if len(batch) < len(eligible) else ""
        lines.append(
            f"[NOTE]   {root.origin}: {len(batch)} scanned / {len(eligible)} eligible"
            f" - {root.path}{cut}"
        )
        scanned_total += len(batch)
        eligible_total += len(eligible)
        for p in batch:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # The engine's name joins the needles for THIS FILE only when the file is one
            # of the three that make a session run something.
            here = needles + ((framework_name,) if framework_name and is_execution_file(p) else ())
            hit = next((n for n in here if n in text), None) or next(
                (loc for loc in locations if names_location(text, loc)), None
            )
            if not hit:
                continue
            posix = p.as_posix()
            hit_exempt = next((k for k in exempt_map if posix.endswith(k)), None)
            if hit_exempt:
                exempted.append(hit_exempt)
                continue
            fails.append(
                f"[FAIL] invariant: data-plane file references the control plane ({hit!r}): {p}"
            )
    elapsed = time.monotonic() - started
    for name, why in sorted(CLAUDE_HOME_OMITTED.items()):
        print(f"[NOTE] invariant: <claude_home>/{name} is outside the scan by declaration - {why}")
    for e in sorted(set(exempted)):
        print(f"[NOTE] invariant exemption in effect - {e}: {exempt_map[e]}")
    verdict = (
        "holds"
        if not fails
        else f"{len(fails)} finding(s)"  # never the word "holds" over a scan that bit
    )
    print(
        f"[NOTE] invariant: {verdict} - {scanned_total} of {eligible_total} "
        f"eligible data-plane files scanned across {len(roots)} root(s) in {elapsed:.1f}s, "
        f"{len(fails)} reference(s), {len(set(exempted))} exemption(s)"
    )
    for line in lines:
        print(line)
    return fails


def tracked_files(root: Path | None = None) -> list[Path] | None:
    """Every file git tracks, plus every un-ignored file it does not yet.

    `--others --exclude-standard` is not padding. With `--cached` alone a NEW file is
    invisible until it is committed, so the check first fires on the commit AFTER the
    one that introduced the leak. Ignored files stay out: tools.local/ is where the
    name is supposed to live.

    Asking git rather than globbing directories is the point: a hand-maintained list of
    where to look for a leak goes stale the first time a directory is added - generated
    reports, helper scripts - and nothing reports that it has.

    The enumeration itself lives in `git_ls_files`, which the invariant scan shares, and
    `None` - never an empty list - travels all the way out of here too. The root is
    whatever `radar.toml` was found by walking up from the working directory, which can
    perfectly well be a directory git knows nothing about - an exported tree, a bundle
    unpacked without `.git`, a `--data-root` pointed at a copy. An empty list there would
    say "0 tracked files, no leaks", which is the wording of a clean scan produced by not
    scanning. The caller turns `None` into a FAIL naming the root, exactly as the
    invariant scan does for a worktree.

    The data root may sit anywhere inside a worktree (`inside_ok`): unlike a tree a
    catalogue names, it is the tree the gate was run in, chosen by whoever ran it.
    """
    return git_ls_files(root or data_root(), inside_ok=True)


def check_scope_leak(tools: list[dict]) -> list[str]:
    """No tracked file may name a machine-local tool - in its content or its filename.

    The gitignore and `radar render`'s include_local=False stop scoped ENTRIES from reaching
    a tracked file. Neither stops PROSE: a `note` or a history `reason` in a committed
    entry that mentions a private tool by name - an evidence line citing the private
    project a number came from, say - gets rendered into README.md and pushed.

    So the isolation is verified rather than assumed, on the same principle as the
    invariant scan: an invariant that nobody verifies does not exist. Two properties keep
    the check as strong as it reads:

    1. It asks git what is tracked rather than scanning a hand-listed set of directories,
       which misses whatever was added after the list was written - an archive of
       reports, generated field reports (a session slug carries the project path it ran
       in), a module's own docstrings.
    2. It reads file NAMES as well as content: a report carrying the name in its own
       filename leaks from a directory listing with the file unopened.

    Scope is `_local` entries only. An entry in tools/ scoped with `environments = [...]`
    took the other side of the trade-off - limited to one environment, but not secret:
    it travels, and its name is in the repo by design - so flagging it would mean
    failing on a name committed one directory away, permanently.
    """
    scoped = {str(t["name"]) for t in tools if t.get("_local")}
    if not scoped:
        return []
    terms = scoped_terms(tools)
    missing = [
        str(t["name"])
        for t in tools
        if t.get("_local") and not str(t.get("redact_as") or "").strip()
    ]
    for name in missing:
        print(
            f"[WARN] {name}: machine-local entry declares no `redact_as`; ingestion "
            "will fall back to a generic placeholder and the corpus loses which tool "
            "it referred to"
        )
    root = data_root()
    found = tracked_files(root)
    if found is None:
        return [
            "[FAIL] scope leak: the data root could not be enumerated - "
            f"{enumeration_failure(root, inside_ok=True)}. The tracked files were NOT "
            "scanned, which is not the same as scanning them clean"
        ]
    fails = []
    for f in found:
        rel = f.relative_to(root)
        hit_name = has_scoped(str(rel), terms)
        if hit_name:
            fails.append(
                f"[FAIL] scope leak: tracked FILENAME contains {hit_name[0]!r}: {rel} "
                "- run `radar redact-backfill` to map it out"
            )
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = has_scoped(text, terms)
        if hit:
            fails.append(
                f"[FAIL] scope leak: tracked file names the scoped tool {hit[0]!r}: "
                f"{rel} - rewrite the prose to describe it without naming it, or run "
                "`radar redact-backfill` for generated/archived content"
            )
    return fails


# --------------------------------------------------------------- the publication check


class Forbidden(NamedTuple):
    """One string that must not appear in the engine's checkout, and why not.

    The reason travels with the term because the check's whole value is in the FAIL line:
    "`widget` appears in README.md" is a puzzle, while "`widget` is the name of an `own`
    catalogue entry (tools/widget.toml)" says both what went wrong and which file to go
    and change.
    """

    term: str
    why: str


# The two FORM rules, which are the only part of this check a public CI can run: they
# name nothing private, so they are shapes rather than a list.
#
# THE FIRST: code does not find the home directory by itself. Finding it is how a machine
# path gets into code without a literal, and `bootstrap.py` is the one module whose JOB is
# to find it; everywhere else the path comes from a profile's [paths]. The rule is checked
# on the PARSED module, never on its text: a text match fires on every comment, docstring,
# message and test that merely names the call, which is where most mentions of it are.
# `home_finding_calls` lists what counts; PATH_HOME is only the fallback for a file that
# does not parse.
PATH_HOME = re.compile(r"Path\s*\.\s*home\s*\(\s*\)")
PATH_HOME_EXEMPT = "bootstrap.py"
# The variables the home is read from: HOME on POSIX, USERPROFILE (or HOMEDRIVE plus
# HOMEPATH) on Windows. Reading any of them is finding the home by another route.
HOME_ENV_VARS = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"})
_PATH_CLASSES = frozenset({"Path", "PosixPath", "WindowsPath"})
_OS_PATH_MODULES = frozenset({"os.path", "posixpath", "ntpath"})
# `C:/Users/<segment>`, either separator. The segment is captured so it can be judged:
# the literal is only a leak when it names a real account. A segment ends at whitespace,
# except for Windows' two folder names that contain a space, which are read whole when the
# segment ends right after them.
USER_LITERAL = re.compile(
    r"(?i)c:[\\/]users[\\/]"
    r"((?:default user|all users)(?![^\\/\s\"'`,;)\]])|[^\\/\s\"'`,;)\]]*)"
)
# What makes the segment documentation rather than a leak. Spelled out rather than
# guessed at with a wildcard, because the whole point of the rule is that a name it does
# not recognise is reported: a placeholder convention nobody declared is a real name to
# everybody reading it.
PLACEHOLDER_SEGMENT = re.compile(
    r"(?i)^(?:\{[^{}]*\}|<[^<>]*>|%[a-z_][a-z0-9_]*%|\$\{?[a-z_][a-z0-9_]*\}?"
    r"|user|username|your-user|your-name|you|name|example"
    # Windows' own folders under C:/Users, which no account is named after.
    r"|public|default|default user|all users)$"
)


def publishable_terms(
    tools: list[dict], marker: dict, engine: dict | None
) -> tuple[list[Forbidden], str | None]:
    """Everything that must not travel, and the note explaining the one exception.

    THE DERIVED LIST is the point. A hand-written list of forbidden names goes stale the
    first time an entry is added and nothing reports that it has - the same reason
    `check_scope_leak` asks git rather than a list of directories. So the `own` names are
    read off the catalogue on every run, which also reaches own entries whose `repo` is a
    remote URL and which therefore never become a scan root at all.
    """
    here = data_root()
    out: list[Forbidden] = [
        Forbidden(n, f"the catalogue's name ({MARKER} [invariant].needles)")
        for n in declared_needles(marker)
    ]
    out.append(Forbidden(here.as_posix(), "the catalogue's absolute path"))
    if str(here) != here.as_posix():
        # Windows writes the same path both ways and a leak takes whichever spelling the
        # tool that wrote it prefers.
        out.append(Forbidden(str(here), "the catalogue's absolute path, in the native spelling"))
    main = main_worktree(here)
    if main is not None:
        out.append(Forbidden(main.as_posix(), "the catalogue's main worktree path"))
        if str(main) != main.as_posix():
            out.append(
                Forbidden(str(main), "the catalogue's main worktree path, in the native spelling")
            )
    for term, _placeholder in scoped_terms(tools):
        out.append(
            Forbidden(
                term,
                "the name of a machine-local tool (tools.local/ - a name that must not travel)",
            )
        )
    exempt_file = str(engine.get("_file")) if engine else None
    for t in tools:
        if t.get("ring") != "own" or (exempt_file and str(t.get("_file")) == exempt_file):
            continue
        name = str(t.get("name") or "").strip()
        if name:
            out.append(Forbidden(name, f"the name of an `own` catalogue entry ({t.get('_file')})"))
    for extra in (marker.get("publish") or {}).get("forbid_extra") or []:
        if isinstance(extra, str) and extra.strip():
            out.append(Forbidden(extra.strip(), f"{MARKER} [publish].forbid_extra"))

    # DE-DUPLICATED, first reason wins. A machine-local entry at ring `own` is reached by
    # two of the branches above - it is a scoped name AND an `own` name - and the same
    # string forbidden twice prints the same leak as two findings, which inflates the
    # count the note ends on. First wins because the branches are ordered by how much the
    # reason says: "the name cannot travel at all" outranks "it names an entry".
    seen: set[str] = set()
    deduped: list[Forbidden] = []
    for f in out:
        key = f.term.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    out = deduped

    note = None
    if engine:
        note = (
            f"[NOTE] publication check: {str(engine.get('name'))!r} ({engine.get('_file')}) is "
            "the engine's own entry - its `repo` resolves to the declared "
            "framework_worktree - so its name is not a forbidden term. Without the "
            "exception the check would fail on every run, on the engine's own "
            "pyproject.toml and banners. It matches by IDENTITY, on `repo`, which is why "
            'the entry stays `visibility = "private"`: a public entry needs `repo` in URL '
            "form and a URL is not a checkout."
        )
    return out, note


def _dotted(node: ast.expr) -> str:
    """`a.b.c` for a chain of names and attributes, "" for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _home_var(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and node.value in HOME_ENV_VARS:
        return str(node.value)
    return None


_PATH_OWNERS = _PATH_CLASSES | {f"pathlib.{c}" for c in _PATH_CLASSES}
_EXPANDUSER = {"expanduser"} | {f"{m}.expanduser" for m in _OS_PATH_MODULES}
# Every call that returns a variable's value. `setdefault` and `pop` return it too, so
# they read the home as surely as `get` does.
_GETENV = {
    "os.getenv",
    "getenv",
    *(f"{m}.{f}" for m in ("os.environ", "environ") for f in ("get", "setdefault", "pop")),
}
_ENVIRON = {"os.environ", "environ"}


class _HomeFinder:
    """The reading of one parsed module that `home_finding_calls` reports from.

    Two facts are gathered from the whole module before any call is judged: the import
    aliases (`from pathlib import Path as P` makes `P` mean `Path`), and the names bound to
    a `~` string anywhere in it (`D = "~/x"` makes `Path(D).expanduser()` a `~` written in
    the code, reached through a name).
    """

    def __init__(self, tree: ast.Module) -> None:
        self.aliases: dict[str, str] = {}
        self.tilde_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.aliases[a.asname or a.name] = self._from_import(node.module, a.name)
            elif isinstance(node, ast.Assign) and self._tilde_literal(node.value):
                self.tilde_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and self._tilde_literal(node.value)
            ):
                self.tilde_names.add(node.target.id)

    @staticmethod
    def _from_import(module: str, name: str) -> str:
        # `from pathlib import Path` and `from os.path import expanduser` are used bare, and
        # the rules below already know the bare spellings; a module is kept dotted.
        if module in {"pathlib", "os", *_OS_PATH_MODULES} and name != "path":
            return name
        return f"{module}.{name}"

    def name(self, node: ast.expr) -> str:
        """`_dotted(node)` with its first segment read through the module's aliases."""
        dotted = _dotted(node)
        head, dot, rest = dotted.partition(".")
        return self.aliases.get(head, head) + dot + rest if dotted else ""

    def _tilde_literal(self, node: ast.expr | None) -> bool:
        """A string written in the code that starts with `~`: plain, an f-string, a
        concatenation whose left end is one (`"~" + "/x"`), or a name bound to one."""
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and node.value.startswith("~")
        if isinstance(node, ast.JoinedStr) and node.values:
            return self._tilde_literal(node.values[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._tilde_literal(node.left)
        if isinstance(node, ast.Name):
            return node.id in self.tilde_names
        return False

    def call(self, node: ast.Call) -> str | None:
        """What `node` is, when it is a call that finds the home directory; else None.

        EXPANDUSER IS FLAGGED ONLY ON A `~` WRITTEN IN THE CODE. `os.path.expanduser("~/x")`
        and `Path("~/x").expanduser()` are the code deciding to look in the home, which is
        `Path.home()` by another spelling. `Path(given).expanduser()` on a value the
        operator typed - a `--data-root`, the directory `radar init` is pointed at - is
        different: the operator named their own home, and the code only honours the
        spelling. Flagging that would forbid accepting `~` on a command line, so the rule
        is drawn on the shape of the argument rather than on which file the call sits in.
        """
        func = self.name(node.func)
        first = node.args[0] if node.args else None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "home" and not node.args:
            owner = self.name(node.func.value)
            if owner in _PATH_OWNERS:
                return f"{owner}.home()"
        if func in _EXPANDUSER and self._tilde_literal(first):
            return f"{func}() on a literal `~` path"
        if isinstance(node.func, ast.Attribute) and node.func.attr == "expanduser":
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Call)
                and self.name(receiver.func).rpartition(".")[2] in _PATH_CLASSES
                and receiver.args
                and self._tilde_literal(receiver.args[0])
            ):
                return "Path(...).expanduser() on a literal `~` path"
        if func in _GETENV:
            var = _home_var(first)
            if var:
                return f"{func}({var!r})"
        return None


def home_finding_calls(source: str) -> list[tuple[int, str]]:
    """(line, what) for every place Python `source` finds the home directory itself.

    Real calls and reads only - `Path.home()` (or `Path.home` handed on uncalled, as a
    `default_factory` is), `expanduser` on a `~` written in the code, and a read of HOME /
    USERPROFILE / HOMEDRIVE / HOMEPATH from the environment. Import aliases are followed.
    A comment, a docstring, a string that names the call and an assignment TO one of those
    variables (a test handing a subprocess its own home) are not findings. A file that does
    not parse falls back to the text rule for `Path.home()`, because a check that skipped
    what it could not parse would pass exactly the file nobody can read.

    The rule reads one module's syntax, so it does not see a home found through
    `getattr`, a `~` passed in from another module or built at run time, or a path helper
    wrapped in a function of the code's own; those are left to review.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [
            (
                source.count("\n", 0, m.start()) + 1,
                "Path.home() (text match; the file does not parse)",
            )
            for m in PATH_HOME.finditer(source)
        ]
    finder = _HomeFinder(tree)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            what = finder.call(node)
            if what:
                hits.append((node.lineno, what))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "home"
            and isinstance(node.ctx, ast.Load)
            and id(node) not in called
            and finder.name(node.value) in _PATH_OWNERS
        ):
            hits.append((node.lineno, f"{finder.name(node.value)}.home passed uncalled"))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and finder.name(node.value) in _ENVIRON
        ):
            var = _home_var(node.slice)
            if var:
                hits.append((node.lineno, f"{finder.name(node.value)}[{var!r}]"))
    return sorted(hits)


def _user_literal_hits(text: str) -> list[str]:
    """The `C:/Users/<name>` literals in `text` that name an account rather than a slot."""
    hits = []
    for m in USER_LITERAL.finditer(text):
        segment = m.group(1)
        if segment and not PLACEHOLDER_SEGMENT.match(segment):
            hits.append(m.group(0))
    return sorted(set(hits))


def check_publishable(
    tools: list[dict], marker: dict, claude_home: str | None = None
) -> tuple[list[str], str | None]:
    """Is the engine's checkout fit to be published? Returns (FAILs, why-it-did-not-run).

    ONLY THE CATALOGUE CAN ASK THIS. The list of what must not travel - the catalogue's
    name and path, the machine-local tool names, the derived list of every `own` entry,
    the operator's own extras - exists in the catalogue and nowhere else, so the published
    checkout's own CI is left checking FORMS (no real `C:/Users/<account>`, no home-finding
    outside bootstrap) and never names. That asymmetry is not a gap to close later; a
    public CI that could check the names would have to hold them.

    A SECOND RETURN VALUE rather than an empty list, for the reason every check in this
    file eventually needed one: "0 findings" out of a check that read nothing is the exact
    wording of a pass. The key may be undeclared (main() then prints one line saying the
    check is off), it names a checkout a CI runner never has, and `--no-data-plane`
    rehearses that - each way not to run has to print as a non-run rather than a green
    line.
    """
    declared = str((marker.get("publish") or {}).get("framework_worktree") or "").strip()
    if not declared:
        return [], (
            f"`[publish].framework_worktree` is not declared in {MARKER}, so no engine "
            "checkout was read. Nothing was compared against the list of what must not "
            "travel, which is not the same as comparing it and finding nothing"
        )
    # The same profiles main() identified the engine with. Passed rather than recomputed
    # so the two readings of "which entry is the engine" cannot answer differently on a
    # profile whose `repo` happens to be written with `{claude_home}`.
    profiles = profile_paths(claude_home)
    tried: list[str] = []
    root: Path | None = None
    relative = False
    for paths in profiles or [{}]:
        raw = expand(declared, paths)
        cand = Path(raw)
        tried.append(cand.as_posix())
        # The engine checkout is a separate tree. A RELATIVE framework_worktree is refused
        # wherever it points, the same refusal the data-plane roots make: read against the
        # data root it can name a directory the catalogue commits, and enumerating that
        # would run git on a tree a pull request controls. A real checkout is named by an
        # absolute path or a placeholder. One whose placeholder no profile declares is
        # unresolved and not enumerated either.
        if not os.path.isabs(raw):
            relative = relative or not UNFILLED.search(raw)
            continue
        if not cand.is_dir():
            continue
        root = cand
        break
    if root is None:
        if relative:
            return [], (
                f"`[publish].framework_worktree` is {declared!r}, a relative path, and a "
                "relative path is refused - read against the data root it can name a "
                "directory this catalogue commits, so nothing was enumerated. Name the "
                "engine checkout by an absolute path or a profile placeholder"
            )
        return [], (
            f"`[publish].framework_worktree` is {declared!r}, which is not a directory in "
            f"any profile (tried {', '.join(sorted(set(tried)))}). The checkout is absent "
            "here - on a runner it always is - so the list of what must not travel was "
            "compared against nothing"
        )

    found = git_ls_files(root)
    if found is None:
        return [
            "[FAIL] publication check: the engine checkout could not be enumerated - "
            f"{enumeration_failure(root)}. Nothing was read, which is not the same as "
            "reading it clean"
        ], None

    engine = framework_entry(tools, declared, profiles)
    terms, exception_note = publishable_terms(tools, marker, engine)
    if exception_note:
        print(exception_note)
    lowered = [(f, f.term.lower()) for f in terms]
    fails: list[str] = []
    for p in found:
        rel = p.relative_to(root).as_posix()
        for f, low in lowered:
            if low in rel.lower():
                fails.append(
                    f"[FAIL] publication check: FILENAME carries {f.term!r} - {f.why}: {rel}"
                )
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low_text = text.lower()
        for f, low in lowered:
            if low in low_text:
                fails.append(f"[FAIL] publication check: {rel} carries {f.term!r} - {f.why}")
        if p.name.lower().endswith(".py") and p.name != PATH_HOME_EXEMPT:
            for line, what in home_finding_calls(text):
                fails.append(
                    f"[FAIL] publication check: {rel} calls `{what}` (line {line}) - only "
                    f"`{PATH_HOME_EXEMPT}` may find the home directory, because that is its "
                    "job. Everywhere else the machine path belongs to the profile's [paths]"
                )
        for literal in _user_literal_hits(text):
            fails.append(
                f"[FAIL] publication check: {rel} carries the literal {literal!r} - a real "
                "account name in a published tree. Write the placeholder the profile fills "
                "(`C:/Users/{user}/...`) or read the path from [paths]"
            )
    print(
        f"[NOTE] publication check: {len(found)} file(s) in {root} read against "
        f"{len(terms)} forbidden term(s) and the two form rules - {len(fails)} finding(s)"
    )
    return fails, None


def measurable(t: dict) -> bool:
    """True when `radar snapshot` records facts for the entry.

    It looks up a github.com `repo` and the download count of a download registry. A
    `github:` registry alone gets neither, so an entry with only that has nothing a
    snapshot could fill in, and calling it unmeasured would ask for a fix that never clears.
    """
    return bool(github_repo(t.get("repo")) or download_registry(t))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar gate",
        description="The adoption gate: schema, evidence, licences, staleness, version "
        "sites, and the control-plane invariant (no data-plane file references the "
        "catalogue).",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--claude-home",
        help="override every profile's [paths].claude_home for this run. Normally not "
        "needed: the profile, or the overlay `radar bootstrap --write` writes beside it, "
        "supplies the path",
    )
    ap.add_argument(
        "--no-data-plane",
        action="store_true",
        help="answer as a CI runner does: no claude_home, no governed worktree checked "
        "out, no gitignored tools.local/. A rehearsal of the run CI will make - on a "
        "developer machine every one of those is present, so --claude-home alone does "
        "NOT reproduce it",
    )
    ap.add_argument(
        "--fast",
        action="store_true",
        help=f"cap the invariant scan at {INVARIANT_MAX_FILES} files per root. Every root "
        "it cuts prints TRUNCATED: a fast run proves less than the default complete one, "
        "and has to say so where the number is read",
    )
    return ap


@reports_profile_errors
def main() -> None:
    args = build_parser().parse_args()
    # First, and before anything reads a catalogue: without a data root there is nothing
    # to judge, and that leaves through exit 2 rather than the 1 a finding costs.
    begin_command(args.data_root)
    marker = load_marker()
    # tools.local/ is gitignored, so a CI checkout does not have it. Dropping it under
    # --no-data-plane is what makes the rehearsal faithful rather than approximate.
    tools = load_tools(include_local=not args.no_data_plane)
    snap_name, snap = latest_snapshot()
    today = datetime.date.today()
    fails = warns = flags = 0
    no_exit: list[str] = []
    no_telemetry: list[str] = []
    # validate() can only check the SHAPE of `environments`; only here is the set of real
    # profile names known. A tool scoped to an environment that does not exist is silently
    # invisible everywhere — the worst failure mode, because nothing reports it.
    known_envs = {e.get("name") for e in load_environments()}
    local_entries = [t.get("name", t["_file"]) for t in tools if t.get("_local")]
    # The axes an entry may declare are the catalogue's, when radar.toml declares them.
    for e in axes_errors(marker):
        print(f"[FAIL] radar.toml: {e}")
        fails += 1
    axes = catalogue_axes(marker)
    # The name is the file: the rule `radar add` writes by, held here for a file written
    # by hand or arriving in a pull request - a second file claiming a tool's name would
    # otherwise put the tool in two ring tables of every rendered page.
    for name, why in entry_file_errors(tools):
        print(f"[FAIL] {name}: {why}")
        fails += 1
    for t in tools:
        name = t.get("name", t["_file"])
        for e in validate(t, axes):
            print(f"[FAIL] {name}: {e}")
            fails += 1
        stray = sorted(set(t.get("environments") or []) - known_envs)
        if stray:
            print(
                f"[FAIL] {name}: scoped to unknown environment(s) {stray} - "
                "the tool would be invisible in every profile"
            )
            fails += 1
        if missing_pilot_exit(t):
            no_exit.append(name)
        if missing_telemetry(t):
            no_telemetry.append(name)
        # A mistyped key in [telemetry] is invisible in TOML and produces a report full
        # of zeros that reads like evidence of disuse. WARN rather than FAIL: the set of
        # known keys grows, and a stray key breaks nothing except the measurement it was
        # meant to configure.
        stray_keys = unknown_telemetry_keys(t)
        if stray_keys:
            print(
                f"[WARN] {name}: [telemetry] has unknown key(s) {stray_keys} - nothing "
                "reads them, so a mistyped matcher name measures nothing and reports a "
                f"zero. Known keys: {', '.join(TELEMETRY_KEYS)}"
            )
            warns += 1
        absent = str(t.get("telemetry_absent") or "").strip()
        if absent and t.get("ring") in ("own", "adopt"):
            # Printed every run, on purpose. A declared gap that goes quiet is just an
            # undeclared gap with extra steps.
            print(f"[NOTE] {name}: telemetry deliberately absent - {absent}")
        lic = t.get("license", "")
        if any(k in lic for k in COPYLEFT):
            print(f"[NOTE] {name}: copyleft license ({lic})")
        elif any(k in lic for k in SOURCE_AVAILABLE):
            print(f"[NOTE] {name}: source-available/noncommercial license ({lic})")
        m = snap.get(name)
        if not m:
            # An entry with something to measure and no facts reads as UNMEASURED, never
            # as clean: with no push date and no download figure, none of the checks
            # below can fire, and silence would look the same as a healthy upstream.
            if snap_name and measurable(t):
                why = (
                    "machine-local entries are never written to the tracked snapshot"
                    if t.get("_local")
                    else f"{snap_name} holds none for it; `radar snapshot` measures it"
                )
                print(f"[NOTE] {name}: no snapshot facts - unmeasured ({why})")
            continue
        # A failed lookup keeps the facts measured earlier, marked repo_stale. Every
        # message that rests on a carried fact says so, and says since when.
        carried = (
            f" (carried from {m.get('repo_as_of')}; the repository lookup has failed since)"
            if m.get("repo_stale")
            else ""
        )
        status = m.get("repo_status")
        if status is not None:
            reason = str(m.get("repo_error") or "").strip()
            gone = str(status) == "404"
            if gone and t.get("ring") in ("own", "adopt", "pilot"):
                # A repository that no longer answers, at a ring that claims use, is the
                # upstream disappearing under an entry - a WARN rather than a FAIL, since a
                # rename or a visibility change answers 404 too.
                print(
                    f"[WARN] {name}: upstream repository not found (HTTP 404) at ring "
                    f"{t['ring']}" + (f" - {reason}" if reason else "") + carried
                )
                warns += 1
            else:
                print(
                    f"[NOTE] {name}: repository lookup failed ({status}"
                    + (f": {reason}" if reason else "")
                    + ")"
                    + carried
                )
        if m.get("archived"):
            # A dead upstream is a FAILURE only where the entry claims you use it. At
            # `discard` an archived repo is the judgement being confirmed, and at
            # `observe` it is a watchlist item resolving itself — reporting either as a
            # gate failure trains the reader to ignore the word FAIL.
            if t.get("ring") in ("own", "adopt", "pilot"):
                print(f"[FAIL] {name}: repo archived at ring {t['ring']}{carried}")
                fails += 1
            else:
                print(f"[NOTE] {name}: repo archived (already {t.get('ring')})")
        pushed = m.get("pushed")
        if pushed and t.get("ring") in ("adopt", "pilot"):
            age = (today - datetime.date.fromisoformat(pushed)).days
            if age > STALE_DAYS:
                print(
                    f"[WARN] {name}: last push {age}d ago at ring {t['ring']}"
                    + (
                        f" (push date carried from {m.get('repo_as_of')}; the repository "
                        "lookup has failed since)"
                        if m.get("repo_stale")
                        else ""
                    )
                )
                warns += 1
        # The registry that publishes a download count, or None. `github:` releases are
        # tags and carry no count, so for these checks it is the same as no registry.
        registry = download_registry(t)
        stars, dl = m.get("stars"), m.get("downloads_month")
        # Unknown downloads on a high-star entry is NOT a pass. The astroturf test needs
        # downloads to fire, so a failed registry lookup silently exonerates exactly the
        # entries the test exists for: a refresh that loses figures makes the flag count
        # fall while the gate knows strictly less. Absence of the number is reported as
        # absence, never as health.
        # Gated on a DECLARED download registry: an entry with none has no download
        # figure by nature (distributed through git, or through tags on github:), and
        # flagging those buries the real signal under false positives. The flag means
        # "a number was expected and is missing", not "no number exists".
        if registry and stars and stars > ASTROTURF_MIN_STARS and dl is None:
            print(
                f"[FLAG] {name}: {stars} stars and NO download figure - the astroturf "
                "test cannot run; treat as unverified, not as clean"
            )
            flags += 1
        elif not registry and dl is not None:
            # A figure in the snapshot with no download registry behind it any more: the
            # entry's own tools/*.toml stopped naming one (removed, or changed to a kind
            # that publishes no count). The snapshot's carry-forward can keep a figure
            # measured under a registry that no longer applies, so the gate has no
            # figure it can trust here. It neither flags nor stays silent - the same
            # discipline as every other check this file cannot run: it says so.
            print(
                f"[NOTE] {name}: {dl}/mo downloads in the snapshot but no download "
                "`registry` (pypi: or npm:) is declared - the astroturf test cannot run; "
                "the figure is not evidence about this entry"
            )
        elif m.get("downloads_stale"):
            print(
                f"[NOTE] {name}: download figure is carried forward from "
                f"{m.get('downloads_as_of')} (registry lookup failed since)"
            )
        if (
            registry
            and stars
            and dl is not None
            and stars > ASTROTURF_MIN_STARS
            and dl < stars * ASTROTURF_RATIO
        ):
            print(f"[FLAG] {name}: astroturf signature - {dl}/mo downloads vs {stars} stars")
            flags += 1
    if not snap_name:
        # No snapshot at all: every tracked entry with something to measure is
        # unmeasured. One line for the lot, rather than one per entry, since the cause is
        # one. Machine-local entries get their own clause: a snapshot never holds them.
        unmeasured = [t for t in tools if measurable(t)]
        local = sum(1 for t in unmeasured if t.get("_local"))
        tracked = len(unmeasured) - local
        if tracked:
            print(
                f"[NOTE] no snapshot yet - {tracked} entr"
                f"{'y' if tracked == 1 else 'ies'} with a GitHub repository or a "
                "download registry unmeasured; `radar snapshot` writes one"
            )
        if local:
            print(
                f"[NOTE] {local} machine-local entr{'y' if local == 1 else 'ies'} with a "
                "GitHub repository or a download registry unmeasured - machine-local "
                "entries are never written to the tracked snapshot"
            )
    if no_exit:
        # A FAIL, not a backlog. A pilot is an experiment, and refusing to state in advance
        # what would end it is how pilots accumulate with none of them measured. The
        # criteria cost one block per new experiment, so the rule blocks.
        fails += len(no_exit)
        for name in sorted(no_exit):
            print(
                f"[FAIL] {name}: ring=pilot with no [pilot_exit] - state what would adopt "
                "or decline it before the field data exists, or set ring=observe"
            )

    if no_telemetry:
        # A FAIL rather than a WARN: a warning nobody is obliged to act on is read less
        # every run, and the transcripts that would have answered the question are pruned
        # while it is ignored.
        #
        # It is not a demand for a matcher on everything: MEASURABLE_ARTIFACTS excludes
        # the kinds a transcript cannot see, because forcing a matcher there produces
        # one that cannot match, and a zero that cannot mean anything is worse than an
        # honest gap.
        fails += len(no_telemetry)
        for name in sorted(no_telemetry):
            print(
                f"[FAIL] {name}: ring={'own/adopt'} with no [telemetry] matcher - a ring "
                "claims observed state, so declare how the claim is checked (match / "
                "match_skill / match_command), or document the absence if the artifact "
                "leaves no tool call"
            )

    # A repo-only check, and one a release process asks for: the version sites agree, by
    # something that fails rather than by convention. Shares its one implementation with
    # the `check-version-sites` verb.
    for err in version_site_errors():
        print(f"[FAIL] version sites: {err}")
        fails += 1

    if local_entries:
        print(
            f"[NOTE] {len(local_entries)} machine-local entr"
            f"{'y' if len(local_entries) == 1 else 'ies'} loaded from tools.local/ "
            f"({', '.join(sorted(local_entries))}) - judged by the gate, left out of "
            "every tracked artefact `radar render` writes"
        )

    # ------------------------------------------------------------ machine-dependent half
    #
    # Named, never silently skipped. Each of these needs something that lives on a
    # machine, and on a CI runner none of it is there: the profiles' claude_home does not
    # exist, no governed worktree is checked out, and tools.local/ is gitignored so it is
    # not in the checkout at all. Reporting that as a pass is how a gate becomes
    # decoration - which is precisely the failure an unverified invariant
    # decays into.
    roots: list[Root] = []
    unresolved: list[str] = []
    # The generic claude_home entries plus the catalogue's own, in that order.
    declared_subpaths, subpath_notes = declared_claude_home_paths(marker)
    subpaths = (*CLAUDE_HOME_SUBPATHS, *declared_subpaths)
    for note in subpath_notes:
        print(note)
    if not args.no_data_plane:
        roots, unresolved = data_plane_roots(tools, args.claude_home, subpaths)
    skipped: list[str] = []

    # The needles, as data. An empty list is legal and WEAKER, and the
    # gate says which of the two it is scanning with, because both print "holds".
    names = declared_needles(marker)
    if not names:
        print(
            f"[NOTE] invariant: {MARKER} declares no [invariant].needles, so the "
            "scan runs on only the data root's absolute path. A path is the strong half of "
            "the needle and a name is the half that survives a move, so this catches a "
            "file that hard-codes the location and misses every file that refers to the "
            "catalogue by name"
        )

    # The engine's name, for the three execution files and for nothing else.
    # Identified by `repo`, never by a second key that could disagree.
    declared_worktree = str((marker.get("publish") or {}).get("framework_worktree") or "").strip()
    engine = (
        framework_entry(tools, declared_worktree, profile_paths(args.claude_home))
        if declared_worktree
        else None
    )
    framework_name = str(engine.get("name")) if engine else None
    if framework_name:
        print(
            f"[NOTE] invariant: {framework_name!r} is a needle in settings.json, any "
            "hooks/hooks.json and CLAUDE.md and nowhere else - naming the "
            "engine in the data plane is ordinary, binding a hook to it is what would make "
            "a session depend on it"
        )
    elif declared_worktree:
        print(
            "[NOTE] invariant: the engine's name is NOT a needle in settings.json, "
            f"hooks/hooks.json or CLAUDE.md - no `own` entry's `repo` resolves to "
            f"{declared_worktree!r}, so the engine has no name in this catalogue yet "
            "(writing that entry gives it one)"
        )

    # THE PUBLICATION CHECK IS OPT-IN. It exists for a catalogue that also maintains a
    # checkout meant for publication, and it is declared by naming that checkout. A
    # catalogue that declares none has nothing to publish, so the gate says in one line
    # that the check is off and why, and says nothing else about it: a paragraph on every
    # run about a check the catalogue never asked for is noise that teaches the reader to
    # skip the output.
    pub_fails: list[str] = []
    pub_skip: str | None = None
    if not declared_worktree:
        pub_off = True
    else:
        pub_off = False
        if args.no_data_plane:
            pub_skip = (
                "--no-data-plane was asked for, so the declared checkout was not read even "
                "where one exists"
            )
        else:
            pub_fails, pub_skip = check_publishable(tools, marker, args.claude_home)
    for msg in pub_fails:
        print(msg)
        fails += 1

    if local_entries:
        for msg in check_scope_leak(tools):
            print(msg)
            fails += 1
    else:
        because = (
            "--no-data-plane was asked for, so tools.local/ was not loaded"
            if args.no_data_plane
            else "this checkout has none (the directory is gitignored)"
        )
        skipped.append(
            "the scope-leak scan over tracked files - it searches for the NAMES declared "
            f"by tools.local/ entries; {because}. With no scoped name to look for it "
            "cannot fail, so a green run says nothing about leaks"
        )

    if unresolved:
        # Printed whether or not anything else became a root. An `own` entry that does
        # not resolve to a tree is invisible unless the absence is named: a root that
        # simply does not exist looks the same as one that was scanned clean, and a
        # refused path left unnamed reads as "no governed tool worktree is checked out".
        print(
            f"[NOTE] invariant: {len(unresolved)} `own` entr"
            f"{'y' if len(unresolved) == 1 else 'ies'} did not become a data-plane root:"
        )
        for u in unresolved:
            print(f"[NOTE]   not a root: {u}")
    if roots:
        exempt_map, exempt_notes = declared_exempt(marker)
        for note in exempt_notes:
            print(note)
        for msg in check_invariant(roots, names, framework_name, fast=args.fast, exempt=exempt_map):
            print(msg)
            fails += 1
    elif args.no_data_plane:
        skipped.append(
            "the invariant scan - --no-data-plane was asked for, so the data "
            "plane was not looked at even where one exists"
        )
    else:
        skipped.append(
            "the invariant scan - no data plane is reachable from here: "
            + unreachable_homes(claude_homes(args.claude_home), subpaths)
            + ", and no governed tool worktree is checked out"
        )

    # Why the checks did not run, said once for all of them only when it is true of all:
    # when the invariant scan ran, the data plane was read, and each skipped check below
    # gives its own reason.
    if args.no_data_plane:
        cause = "the data plane is deliberately ignored (--no-data-plane) - "
    elif not roots:
        cause = "the data plane is not reachable from here - "
    else:
        cause = ""
    if skipped:
        print(
            f"\n[NOTE] {cause}{len(skipped)} check(s) did NOT run. "
            "Named rather than counted, because a scan of nothing prints the same "
            "'0 references' a real one does:"
        )
        for s in skipped:
            print(f"[NOTE]   skipped: {s}")
        print(
            "[NOTE]   not applicable: machine probes - this gate runs none. `radar apply` "
            "runs the installation probes and `radar versions` the version probes."
        )

    # Reported on its own line rather than folded into the list above, because it is a
    # different absence: the publication check is not missing a DATA PLANE, it is missing
    # the declared checkout, and on a day when one is present it runs with no data plane
    # at all.
    if pub_off:
        print("[NOTE] publication check: off ([publish].framework_worktree is not declared)")
    elif pub_skip:
        print(
            f"\n[NOTE] the publication check did NOT run - {pub_skip}. This is not a pass: "
            "the list of what must not travel is derived from this catalogue and was "
            "compared against nothing"
        )

    print(
        f"\n{len(tools)} tools checked against snapshot {snap_name or '(none)'}"
        f" - {fails} FAIL, {warns} WARN, {flags} astroturf FLAG"
        + (
            f", {len(skipped)} check(s) skipped" + (" without a data plane" if cause else "")
            if skipped
            else ""
        )
        + (", publication check NOT run" if pub_skip else "")
    )
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
