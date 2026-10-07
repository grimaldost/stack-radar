"""Create a catalogue, and write entries into one: `radar init` and `radar add`.

THE ONLY TWO VERBS THAT WRITE A CATALOGUE RATHER THAN READ ONE, which is why they share a
module. Everything else in this package starts by resolving a data root and then reports on
what it found; `init` creates the root that the rest of the engine needs in order to run at
all, and `add` writes the first thing into it. Without them a data root is made by hand, and
a catalogue made by hand is shaped by whichever one it was copied from.

`init` IS THE ONE VERB THAT DOES NOT RESOLVE A ROOT. It cannot: it is handed the directory
that is about to become one, and a resolver that searched upwards from there would find
whatever catalogue happens to sit above it and write into that instead. It is also why this
module asks `bootstrap` for the home directory rather than reaching for the machine itself -
the discovery of where a machine keeps its things is that module's job, it is exempted from
the publication check by name for exactly that reason, and two modules that both decided it
would be two answers to one question.

WHAT IS A TEMPLATE AND WHAT IS COMPUTED, because the line is a rule rather than a
convenience. Prose, comments and structure are templates: they are the same for every
catalogue, and a generated file whose comments explain nothing is a file the first reader
deletes. Paths are COMPUTED - the environment profile carries what was found on this
machine, never a placeholder for the operator to fill in - because a template path is a
guess, and a guess in a profile fails later and somewhere else than where it was made.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import os
import re
import sys
import textwrap
import tomllib
from importlib import resources
from pathlib import Path
from typing import NamedTuple

from .bootstrap import discovered_paths, home_dir, starter_profile
from .paths import (
    MARKER,
    DataRootNotFound,
    UnsafeWrite,
    resolve_data_root,
    tools_dir,
    tools_local_dir,
    write_error,
    write_inside,
)
from .radar_lib import (
    AXES_KEY,
    ARTIFACTS,
    DEFAULT_AXES,
    DEFAULT_ENVIRONMENT,
    ENGINE_REPOSITORY,
    RINGS,
    axes_errors,
    begin_command,
    catalogue_axes,
    entry_file_errors,
    file_form,
    framework_errors,
    load_marker,
    missing_pilot_exit,
    missing_telemetry,
    validate,
)
from .render import DEFAULT_BOUNDARY_NOTE

_PACKAGE = __package__ or "stack_radar"
TEMPLATES = "templates"

# The version a catalogue is born at, and the date grammar its changelog heading uses.
INITIAL_VERSION = "0.1.0"

# The environment a fresh catalogue has is radar_lib.DEFAULT_ENVIRONMENT, the name every
# verb falls back to. One profile, named after nothing in particular: a machine's role is
# the operator's to describe, and "default" is the one name that does not pretend to know
# it.

# Where a catalogue's CI installs the engine from: this repository's git source, at the tag
# of the engine version that created the catalogue. A git URL and a tag rather than a
# package name, because a name is resolved against whatever index the runner reaches, and
# whoever publishes under that name first decides what every generated workflow runs. One
# constant, so a fork changes one line; the CI template says how an operator points an
# existing catalogue elsewhere.
ENGINE_SOURCE = f"git+{ENGINE_REPOSITORY}"
DISTRIBUTION = "stack-radar"
_COMMIT = re.compile(r"[0-9a-f]{40}")


class InstalledFrom(NamedTuple):
    """Where this engine was installed from: the commit, and the revision the install
    asked for (a tag, a branch, a commit id, or None when the record gives none)."""

    commit: str
    requested: str | None


def installed_commit() -> InstalledFrom | None:
    """The commit this engine was installed from, when it was installed from its own git
    source, else None.

    An installer records where a distribution came from in its `direct_url.json` (the
    format of PEP 610): for a git source, the repository URL, `vcs_info.commit_id`, the
    full commit the requested revision resolved to, and `vcs_info.requested_revision`. A
    URL other than ENGINE_SOURCE's repository, over https or ssh - a fork, a mirror, a
    local clone - is not this repository's commit to name.
    """
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        return None
    commit, url = vcs.get("commit_id"), data.get("url")
    if not (isinstance(commit, str) and _COMMIT.fullmatch(commit) and isinstance(url, str)):
        return None
    wanted = ENGINE_SOURCE.removeprefix("git+")
    spellings = {wanted, "ssh://git@" + wanted.removeprefix("https://")}
    if url.removesuffix("/").removesuffix(".git") not in spellings:
        return None
    requested = vcs.get("requested_revision")
    return InstalledFrom(commit, requested if isinstance(requested, str) else None)


def engine_spec(engine_version: str, commit: str | None = None) -> str:
    """The `uvx --from` value the generated CI uses: the source, pinned to the commit the
    running engine was installed from when it knows it, else to the release tag.

    A commit, because a tag can be moved to other code and a commit cannot - the same rule
    the generated pre-commit config and dependabot file follow for third-party code.
    """
    return f"{ENGINE_SOURCE}@{commit or f'v{engine_version}'}"


def engine_pin_note(engine_version: str, commit: str | None, *, tagged: bool = False) -> str:
    """The CI header's paragraph on how the engine steps are pinned, as comment lines.

    `tagged`: the install asked for the release tag, so `commit` is that tag's commit. An
    engine installed from a branch or another commit is pinned to its commit all the same,
    and the note says only that the install did not ask for the tag: the engine cannot
    tell whether that commit is the release's.
    """
    tag = f"v{engine_version}"
    repo = ENGINE_SOURCE.removeprefix("git+")
    if commit and tagged:
        body = (
            f"the `uvx` steps install the engine from its git source at the commit of {tag}, "
            "the release that created this catalogue, named in the comment beside each "
            "step. A commit rather than the tag, because a tag can be moved to other code "
            "and a commit cannot."
        )
    elif commit:
        body = (
            f"the `uvx` steps install the engine from its git source at the commit {commit} "
            "the engine that wrote this file was installed from (version "
            f"{engine_version}). That install did not ask for the release tag {tag}, so "
            "this commit may differ from the release's. To run the release, replace the "
            f"commit on each step with {tag}'s commit: "
            f"`git ls-remote {repo} {tag}` prints it (for an annotated tag, the line ending "
            "in `^{}`). A commit rather than a tag, because a tag can be moved to other "
            "code and a commit cannot."
        )
    else:
        body = (
            f"the `uvx` steps install the engine from its git source at the tag {tag}, the "
            "release that created this catalogue: the engine that wrote this file was not "
            f"installed with git from {repo}, so it did not know that repository's commit "
            "for the tag. A tag can be moved to "
            f"other code and a commit cannot, so pin the commit: `git ls-remote {repo} "
            f"{tag}` prints it (for an annotated tag, the line ending in `^{{}}`), and it "
            f"replaces `@{tag}` on each step."
        )
    body += (
        " To run another copy - a fork, a mirror, a package index you control - change the "
        "`--from` value on each of them, and keep it pinned to a commit: a bare package "
        "name resolves to whatever is published under that name when the job runs, which "
        "is code this workflow would execute without anyone reading it."
    )
    lines = textwrap.wrap(body, width=85, break_on_hyphens=False, break_long_words=False)
    return "\n".join([f"#   * {lines[0]}", *(f"#     {ln}" for ln in lines[1:])])


# TEMPLATE -> PATH IN THE NEW CATALOGUE, and the indirection is not decoration. Several of
# these are dotfiles, and a template called `.gitignore` inside this package would be read
# by git as a real ignore file for the package directory - so the stored names carry no
# leading dot and a `.tmpl` suffix, which also keeps a linter or a build backend from
# mistaking a template for the config file it is a template OF.
LAYOUT: tuple[tuple[str, str], ...] = (
    ("radar.toml.tmpl", MARKER),
    ("pyproject.toml.tmpl", "pyproject.toml"),
    ("CHANGELOG.md.tmpl", "CHANGELOG.md"),
    ("gitignore.tmpl", ".gitignore"),
    ("gitattributes.tmpl", ".gitattributes"),
    ("pre-commit-config.yaml.tmpl", ".pre-commit-config.yaml"),
    ("ci.yml.tmpl", ".github/workflows/ci.yml"),
    ("dependabot.yml.tmpl", ".github/dependabot.yml"),
    ("githooks-pre-commit.tmpl", ".githooks/pre-commit"),
    ("githooks-commit-msg.tmpl", ".githooks/commit-msg"),
    ("tools-README.md.tmpl", "tools/README.md"),
)

# Git skips a hook without the executable bit on POSIX and says nothing, so a catalogue
# created there would report its lane armed while nothing fired. Set here; on Windows the
# bit does not exist in the filesystem and the hook template says what to run instead.
EXECUTABLE = (".githooks/pre-commit", ".githooks/commit-msg")

# Created even when nothing is written into them yet. `tools/` carries a README so the
# directory survives a commit (git tracks files, not directories) and so the first reader
# finds the entry schema where the entries go.
DIRECTORIES = ("tools", "environments", ".githooks", ".github/workflows")


class AlreadyARoot(Exception):
    """The target is a data root already. `init` creates one; it does not migrate one."""


class WouldReplace(Exception):
    """The target already holds files `init` would write, and `--force` was not given."""


# --------------------------------------------------------------------------- templates


def template(name: str) -> str:
    """One template's text, read from the installed package rather than from a checkout.

    Through `importlib.resources` and not `Path(__file__)`: an installed engine's templates
    travel inside the wheel, and reaching for a sibling path would work in a checkout and
    fail in exactly the installation this engine is built to be.
    """
    return resources.files(_PACKAGE).joinpath(TEMPLATES, name).read_text(encoding="utf-8")


def _toml_string(value: str) -> str:
    """One TOML basic string's contents, escaped. Not a general writer - see `entry_toml`."""
    out = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _project_name(directory_name: str) -> str:
    """A distribution-shaped name from a directory name.

    The catalogue's `pyproject.toml` needs one and it is read by `uv`, so it has to be a
    legal name rather than whatever the directory is called. Lowercased, with every run of
    anything else folded to a single hyphen.
    """
    kept = [c if (c.isascii() and c.isalnum()) else "-" for c in directory_name.lower()]
    name = "-".join(part for part in "".join(kept).split("-") if part)
    return name or "radar-catalogue"


def compatible_range(engine_version: str) -> str:
    """The `requires_framework` a new catalogue is given: minor-compatible with this engine.

    Minor rather than exact, because an exact pin would make every patch release of the
    engine an edit to every catalogue; minor rather than major, because the field the engine
    silently does not know about is the failure mode this range exists to catch, and a minor
    is where a field arrives.

    An engine version this cannot parse yields a floor alone rather than a guessed ceiling: a
    range nobody can read is refused by the checker anyway, and refusing it here with a
    plausible-looking upper bound would be the worse failure.
    """
    parts = engine_version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return f">={engine_version}"
    return f">={engine_version},<{major}.{minor + 1}.0"


def substitutions(
    *,
    title: str,
    needle: str,
    requires: str,
    engine_version: str,
    today: str,
    project: str,
    installed: InstalledFrom | None = None,
) -> dict[str, str]:
    """`__MARKER__` -> value, for every template.

    MARKERS AND NOT `str.format`, for a mechanical reason: the CI template carries GitHub's
    `${{ ... }}` expressions, which `format` reads as fields and refuses. The same reason
    `render.py` substitutes its HTML template this way.

    The three free-text values are escaped as TOML strings because that is the only kind of
    file they are substituted into; the rest are versions, dates and a name already
    constrained to a safe shape.
    """
    commit = installed.commit if installed else None
    # The tag is named beside a step only when the install asked for that tag.
    tagged = installed is not None and installed.requested == f"v{engine_version}"
    return {
        "__TITLE__": _toml_string(title),
        "__NEEDLE__": _toml_string(needle),
        "__BOUNDARY_NOTE__": _toml_string(DEFAULT_BOUNDARY_NOTE),
        "__DEFAULT_ENVIRONMENT__": DEFAULT_ENVIRONMENT,
        "__REQUIRES_FRAMEWORK__": requires,
        "__VERSION__": INITIAL_VERSION,
        "__ENGINE_VERSION__": engine_version,
        "__ENGINE_SPEC__": engine_spec(engine_version, commit),
        "__ENGINE_COMMENT__": f" # v{engine_version}" if tagged else "",
        "__ENGINE_PIN_NOTE__": engine_pin_note(engine_version, commit, tagged=tagged),
        "__DATE__": today,
        "__PROJECT_NAME__": project,
    }


def _fill(text: str, values: dict[str, str]) -> str:
    for marker, value in values.items():
        text = text.replace(marker, value)
    return text


def _write(root: Path, path: Path, text: str) -> None:
    """Write LF, always, whatever platform this runs on, and only inside `root`.

    `.githooks/*` are `#!/bin/sh` scripts git executes directly, and a CR carried into the
    interpreter path fails in a way that reads as "the hook is broken" rather than "the file
    was written with the wrong line endings". Applied to every file rather than to the two
    that need it, because a generated tree with two conventions in it is a diff nobody can
    read on the machine that did not create it. `write_inside` writes the text as given
    and raises UnsafeWrite for a symbolic link on the way.
    """
    write_inside(root, path, text)


# -------------------------------------------------------------------------------- init


def create(
    target: Path,
    *,
    title: str | None = None,
    home: Path | None = None,
    engine_version: str | None = None,
    today: datetime.date | None = None,
    force: bool = False,
) -> Path:
    """Write a whole empty catalogue at `target` and return its resolved path.

    A target that already holds any file this would write is refused, naming each, unless
    `force`: `init` is often pointed at a directory that holds a project already, and
    replacing its `.gitignore` or `pyproject.toml` loses what they said - an ignore rule
    for a secret is then gone before the printed `git add -A`.

    `home` and `today` are parameters rather than reads so the tree can be created against a
    synthetic machine. That is not a test hook bolted on: the profile this writes is the one
    thing here that is computed from the machine, so a function that could only ever be run
    against the real one could not be checked for the property it exists to have.
    """
    from . import __version__

    root = Path(str(target).replace("\\", "/")).expanduser().resolve()
    if (root / MARKER).is_file():
        raise AlreadyARoot(
            f"{root} already carries {MARKER}, so it is already a data root. `init` creates "
            f"a catalogue and will not write over one - there is no way for it to tell an "
            f"empty tree from a catalogue whose entries are somewhere else yet"
        )
    version = engine_version or __version__
    values = substitutions(
        title=title or root.name,
        needle=root.name,
        requires=compatible_range(version),
        engine_version=version,
        today=(today or datetime.date.today()).isoformat(),
        project=_project_name(root.name),
        # The installed commit is the running engine's, so it pins only its own version.
        installed=installed_commit() if engine_version is None else None,
    )
    files = {root / relative: _fill(template(name), values) for name, relative in LAYOUT}
    files[root / "environments" / f"{DEFAULT_ENVIRONMENT}.toml"] = starter_profile(
        DEFAULT_ENVIRONMENT, discovered_paths(home or home_dir()), home or home_dir()
    )
    # Every destination is judged before anything is created, so a refused one leaves
    # the directory as it was.
    refused = [err for err in (write_error(root, path) for path in files) if err]
    if refused:
        raise UnsafeWrite("; ".join(refused))
    present = [p.relative_to(root).as_posix() for p in files if os.path.lexists(p)]
    if present and not force:
        raise WouldReplace(
            f"{root.as_posix()} already holds {len(present)} file(s) `radar init` would "
            f"write: {', '.join(present)}. Nothing was written. Move them aside, or re-run "
            "with --force to replace them"
        )
    for relative in DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        _write(root, path, text)
    for relative in EXECUTABLE:
        path = root / relative
        path.chmod(path.stat().st_mode | 0o111)
    return root


def next_steps(root: Path) -> list[str]:
    """The commands that take a new root to its first green CI run, in the order to run them.

    THE ORDER IS THE CONTENT. The generated workflow's first step is `uv lock --check`, so
    `uv.lock` has to be in the first commit, which puts `uv lock` before `git add`. Git on
    Windows records the hooks without the executable bit, and a mode fixed after the commit
    is a change nobody commits, so the fix goes between `git add` and `git commit`. The
    commit lane is armed after the first commit, so creating the catalogue does not need
    the lane's own tooling installed yet.

    `init` does not write the lock itself: a lock is the resolved set of the dev
    dependencies, which takes a resolver, and this engine has no runtime dependencies.

    Each step is one line holding one command and nothing else, and every argument that
    may hold a space is in double quotes: that much reads the same to sh, PowerShell and
    cmd, except that cmd changes drive only with `cd /d`, which STEP_NOTES says. A
    backslash continuation is sh syntax only, and so is a trailing `# comment` in cmd,
    which hands the `#` and every word after it to the command as arguments - so the
    reasons are printed below the list (`STEP_NOTES`), never beside a step. Double quotes
    are the one quoting all three shells share, and sh still expands `$` and a backtick
    inside them, so a directory whose name holds either needs quoting by hand there.
    """
    return [
        f'cd "{root.as_posix()}"',
        "uv lock",
        "git init",
        "git add -A",
        f"git update-index --chmod=+x {' '.join(EXECUTABLE)}",
        'git commit -m "chore: create the catalogue"',
        "git config core.hooksPath .githooks",
        "radar add <name> --repo <url> --axis <axis> --ring observe --license <spdx> "
        '--visibility public --artifact cli --note "<one line>" --reason "<why it entered>"',
        "radar gate",
        "radar render",
    ]


# Why the steps above run in that order, printed after them rather than beside them.
STEP_NOTES = (
    "`uv lock` runs before the first commit because CI's first step checks uv.lock.",
    "`git update-index --chmod=+x` runs before it too: git on Windows records the hooks "
    "without the executable bit.",
    "`git config core.hooksPath` arms the commit lane once the catalogue exists. It "
    "points git at the tracked .githooks/, so those scripts run on every commit, and a "
    "hook a checked-out branch adds there runs too: review a change to .githooks/, "
    ".pre-commit-config.yaml, uv.lock, pyproject.toml, uv.toml or .python-version in a "
    "pull request as a code change, like a change to an [install] command.",
    "In cmd, a `cd` to another drive needs `cd /d`, or the drive does not change and the "
    "steps after it run in the old directory.",
    "`radar gate` should report 0 FAIL, and `radar render` writes README.md and "
    "docs/radar/index.html.",
)


def _enclosing_root(root: Path) -> Path | None:
    """A data root ABOVE the one just created, if any. Reported, never refused."""
    return next((p for p in root.parents if (p / MARKER).is_file()), None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="radar init",
        description="Create a radar data root: the marker, an environment profile with this "
        "machine's discovered paths, an empty catalogue, the commit lane and a CI workflow.",
    )
    ap.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="where to create the catalogue (created if absent; default: here)",
    )
    ap.add_argument(
        "--title",
        help="the title every generated artefact carries (default: the directory's name). A "
        "datum the catalogue holds, so it can be changed later in radar.toml",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="replace the files the directory already holds that init would write "
        "(without it, init names them and writes nothing). Never a radar.toml: a data "
        "root is not re-created",
    )
    args = ap.parse_args(argv)

    try:
        root = create(Path(args.directory), title=args.title, force=args.force)
    except (AlreadyARoot, UnsafeWrite, WouldReplace) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[FAIL] could not create a catalogue at {args.directory}: {exc}", file=sys.stderr)
        return 2

    marker = tomllib.loads((root / MARKER).read_text(encoding="utf-8"))
    print(f"[OK] data root created at {root.as_posix()}")
    for _name, relative in LAYOUT:
        print(f"       wrote {relative}")
    print(f"       wrote environments/{DEFAULT_ENVIRONMENT}.toml (paths discovered here)")

    outer = _enclosing_root(root)
    if outer is not None:
        # Reported rather than refused: the outer catalogue is untouched, and the operator
        # may well mean it. What they cannot see without being told is that a command run
        # inside the new root now finds THIS marker, because the search stops at the nearest
        # one - so the outer catalogue becomes unreachable from in here.
        print(
            f"[NOTE] {outer.as_posix()} is also a data root. The search stops at the nearest "
            f"{MARKER}, so every command run inside the new root judges the new catalogue and "
            "the enclosing one is not reachable from here"
        )

    from . import __version__ as version

    drift = framework_errors(version, str(marker.get("requires_framework") or ""))
    if drift:
        # The range above is derived from this engine's version, so the two disagree only
        # when that version could not be parsed and the range is a floor alone. Printed with
        # both numbers rather than left to surface as a mismatch on the next command.
        print(
            f"[NOTE] this engine is version {version} and the new catalogue declares "
            f"`requires_framework = {marker.get('requires_framework')!r}`, which the engine "
            f"does not satisfy ({'; '.join(drift)}). Widen the range before running another "
            "command"
        )

    print("\nnext:")
    for step in next_steps(root):
        print(f"  - {step}")
    print()
    for note in STEP_NOTES:
        print(f"  {note}")
    return 0


# --------------------------------------------------------------------------------- add


def build_entry(args: argparse.Namespace, today: datetime.date | None = None) -> dict:
    """The entry, as the dict `validate()` judges. Optional blocks appear only when asked for.

    Absent rather than empty, throughout: an empty `[telemetry]` table is an entry that
    declares a matcher and has none, which reports a zero that reads as evidence of disuse -
    the failure the closed key set and the matcher rules both exist to prevent.
    """
    entry: dict = {
        "name": args.name,
        "repo": args.repo,
        "axis": args.axis,
        "artifact": args.artifact,
        "ring": args.ring,
        "license": args.license,
        "visibility": args.visibility,
        "note": args.note,
    }
    if args.registry:
        entry["registry"] = args.registry
    if args.eval_status:
        entry["eval_status"] = args.eval_status
    if args.telemetry_absent:
        entry["telemetry_absent"] = args.telemetry_absent

    telemetry: dict = {}
    for key, value in (
        ("match", args.telemetry_match),
        ("match_skill", args.telemetry_match_skill),
        ("match_command", args.telemetry_match_command),
    ):
        if value:
            telemetry[key] = list(value)
    if telemetry:
        date = (today or datetime.date.today()).isoformat()
        telemetry["since"] = args.telemetry_since or date
        entry["telemetry"] = telemetry

    exit_criteria = {
        "review_after_days": args.pilot_review_after_days,
        "adopt_if": args.pilot_adopt_if,
        "decline_if": args.pilot_decline_if,
    }
    if any(v is not None for v in exit_criteria.values()):
        entry["pilot_exit"] = {k: v for k, v in exit_criteria.items() if v is not None}

    history: dict = {
        "date": args.date or (today or datetime.date.today()).isoformat(),
        "ring": args.ring,
        "reason": args.reason,
    }
    if args.evidence:
        history["evidence"] = args.evidence
    entry["history"] = [history]
    return entry


def entry_errors(entry: dict, axes: list[str] | None = None) -> list[str]:
    """Everything that would stop this entry, schema and gate alike.

    `validate()` IS NOT THE WHOLE GATE, and writing an entry that only satisfies it would
    hand the operator a catalogue that fails on the very next command. Two of the gate's
    rules live outside the schema on purpose - they are judgements about the claim an entry
    makes rather than about its shape - and they are imported rather than restated, because
    a second opinion about one rule is two rules that will one day disagree.
    """
    errs = list(validate(entry, axes))
    if missing_pilot_exit(entry):
        errs.append(
            "ring=pilot with no [pilot_exit]: say what would adopt it and what would "
            "decline it BEFORE the field data exists (--pilot-adopt-if, --pilot-decline-if, "
            "--pilot-review-after-days), or enter it at --ring observe"
        )
    if missing_telemetry(entry):
        errs.append(
            "ring=own/adopt with no [telemetry] matcher: a ring is a claim about observed "
            "state, so declare how the claim is checked (--telemetry-match, "
            "--telemetry-match-skill, --telemetry-match-command) or record why the artifact "
            "leaves no trace a session can see (--telemetry-absent)"
        )
    return errs


def entry_toml(entry: dict) -> str:
    """One entry as TOML text.

    HAND-WRITTEN AND DELIBERATELY NARROW. The engine has no runtime dependencies - that is
    what lets it be installed anywhere, and run straight off a PYTHONPATH where it cannot be
    installed at all - so there is no TOML writer to call. A general one is a small library's
    worth of edge cases; the entry schema is CLOSED, so this covers exactly its shapes and
    raises on anything else rather than emitting something that parses as the wrong thing.

    The result is read back and compared to this dict by the caller, which is what makes
    that narrowness safe to rely on.
    """
    scalars = [(k, v) for k, v in entry.items() if not isinstance(v, (dict, list))]
    lists = [(k, v) for k, v in entry.items() if isinstance(v, list) and k != "history"]
    tables = [(k, v) for k, v in entry.items() if isinstance(v, dict)]
    lines = [f"{k} = {_value(v)}" for k, v in scalars]
    lines += [f"{k} = {_value(v)}" for k, v in lists]
    for name, table in tables:
        lines += ["", f"[{name}]"]
        lines += [f"{k} = {_value(v)}" for k, v in table.items()]
    for record in entry.get("history") or []:
        lines += ["", "[[history]]"]
        lines += [f"{k} = {_value(v)}" for k, v in record.items()]
    return "\n".join(lines) + "\n"


def _value(value: object) -> str:
    if isinstance(value, bool):  # before int: bool is an int in Python
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f'"{_toml_string(value)}"'
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(
        f"an entry field of type {type(value).__name__} is outside the closed entry schema "
        f"this writer covers: {value!r}"
    )


def build_parser(axes: list[str] | None = None) -> argparse.ArgumentParser:
    """`radar add`'s parser. `axes` is the catalogue's axis set (`catalogue_axes`), which
    `--axis` accepts and its help lists; DEFAULT_AXES when not given."""
    axes = axes or list(DEFAULT_AXES)
    ap = argparse.ArgumentParser(
        prog="radar add",
        description="Write one validated tool entry, with its first dated history block, "
        "into the catalogue's tools/ directory.",
    )
    ap.add_argument(
        "name",
        help="the tool's name, which is also its file name, tools/<name>.toml - so it is "
        "written lowercase: letters, digits, '.', '_' and single hyphens, none at either "
        "end - and no other entry may claim it",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI and "
        "nothing else; it still has to name a directory that carries the marker",
    )
    required = ap.add_argument_group("the eight required fields")
    required.add_argument("--repo", required=True, help="URL, or a local path for a private entry")
    required.add_argument(
        "--axis",
        required=True,
        choices=axes,
        metavar="AXIS",
        help=f"which problem it addresses, one of the catalogue's axes ({AXES_KEY} in "
        f"radar.toml, else the default set): {', '.join(axes)}",
    )
    required.add_argument("--ring", required=True, choices=RINGS, help="how committed you are")
    required.add_argument("--license", required=True, help="SPDX identifier, as upstream states it")
    required.add_argument("--visibility", required=True, choices=("public", "private"))
    required.add_argument(
        "--artifact",
        required=True,
        choices=ARTIFACTS,
        help="what you install and where it runs - context cost and blast radius follow from it",
    )
    required.add_argument("--note", required=True, help="one line: what it is and why it is here")

    history = ap.add_argument_group("the first history block")
    history.add_argument("--reason", required=True, help="why the entry enters at this ring")
    history.add_argument("--date", help="YYYY-MM-DD (default: today)")
    history.add_argument(
        "--evidence",
        help="what the reason rests on: a link, a run, a report. Required for --ring adopt "
        "and --ring pilot",
    )

    optional = ap.add_argument_group("optional fields")
    optional.add_argument(
        "--registry",
        help="where upstream releases are read: pypi:<name>, npm:<name>, or "
        "github:<owner>/<repo> (releases are the repository's git tags, with no download "
        "count). Needed by a `latest` version policy",
    )
    # The vocabulary is not repeated here; `radar_lib.validate` owns it, and the help
    # points at the schema.
    optional.add_argument(
        "--eval-status",
        metavar="STATUS",
        help="how the entry's value was established: `unmeasured` (the default) or "
        "`measured:<what was measured>`; the entry schema holds the full set",
    )
    optional.add_argument(
        "--telemetry-match", action="append", default=[], metavar="GLOB", help="MCP tool names"
    )
    optional.add_argument(
        "--telemetry-match-skill",
        action="append",
        default=[],
        metavar="GLOB",
        help="skill ids, for a tool reached through its skills",
    )
    optional.add_argument(
        "--telemetry-match-command",
        action="append",
        default=[],
        metavar="GLOB",
        help="globs for a CLI, matched against each shell command's resolved chain of "
        "executables, so `uv run ruff check .` matches `ruff`",
    )
    optional.add_argument(
        "--telemetry-since",
        help="YYYY-MM-DD (default: today); needs a matcher, since the window is recorded "
        "only in a [telemetry] block",
    )
    optional.add_argument(
        "--telemetry-absent",
        metavar="WHY",
        help="record that this artifact leaves no trace a session can see, and why",
    )
    optional.add_argument("--pilot-adopt-if", metavar="PROSE")
    optional.add_argument("--pilot-decline-if", metavar="PROSE")
    optional.add_argument("--pilot-review-after-days", type=int, metavar="N")
    optional.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing entry file; without it an existing file is refused",
    )
    return ap


# The rings whose entry claims the tool is in use, which is a claim that rests on
# something. `validate()` refuses a history block at these rings without evidence; asking
# for it here, before a root is even resolved, names the flag instead of the field.
RINGS_NEEDING_EVIDENCE = ("adopt", "pilot")


def catalogue_axes_for(argv: list[str] | None) -> list[str]:
    """The axes of the catalogue `radar add` will write into, found before the arguments
    are parsed, so `--axis` accepts them and `--help` lists them: the data root named by
    `--data-root` or found from the working directory. DEFAULT_AXES when there is none -
    the command then stops on the missing data root, after parsing."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--data-root")
    known, _ = pre.parse_known_args(argv)
    try:
        return catalogue_axes(root=resolve_data_root(known.data_root).path)
    except (DataRootNotFound, OSError):
        return list(DEFAULT_AXES)


def _named_entries(root: Path) -> tuple[list[dict], list[str]]:
    """Each entry in tools/ and tools.local/ as the name rule reads it (`name`, `_file`),
    and a line for each file that does not parse.

    A file that does not parse is counted as claiming its own stem, the name the rule
    gives it, so it still takes part in the check for a second claim. The gate fails it.
    """
    entries: list[dict] = []
    unreadable: list[str] = []
    folders = [(tools_dir(root), "")]
    local = tools_local_dir(root)
    if local.exists():
        folders.append((local, "tools.local/"))
    for folder, prefix in folders:
        for f in sorted(folder.glob("*.toml")):
            label = prefix + f.name
            try:
                with f.open("rb") as fh:
                    name = tomllib.load(fh).get("name")
            except (tomllib.TOMLDecodeError, OSError) as exc:
                unreadable.append(f"{prefix or 'tools/'}{f.name} does not parse ({exc})")
                name = f.stem
            entries.append({"name": name, "_file": label})
    return entries, unreadable


def add(argv: list[str] | None = None) -> int:
    parser = build_parser(catalogue_axes_for(argv))
    args = parser.parse_args(argv)
    if args.ring in RINGS_NEEDING_EVIDENCE and not (args.evidence or "").strip():
        parser.error(
            f"--ring {args.ring} needs --evidence: an entry at {args.ring} claims the tool "
            "is in use, and the claim has to say what it rests on"
        )
    if args.telemetry_since and not (
        args.telemetry_match or args.telemetry_match_skill or args.telemetry_match_command
    ):
        # The window is written only inside a [telemetry] block, and the block exists only
        # with a matcher, so without one the flag would be dropped and the operator would
        # believe a window was recorded when none is. Refused by name instead.
        parser.error(
            "--telemetry-since needs a matcher: the window is recorded only in a "
            "[telemetry] block, which exists only with --telemetry-match, "
            "--telemetry-match-skill or --telemetry-match-command"
        )
    root = begin_command(args.data_root)
    marker_errs = axes_errors(load_marker(root))
    if marker_errs:
        for err in marker_errs:
            print(f"[FAIL] radar.toml: {err} - nothing written")
        return 1

    entry = build_entry(args)
    errs = entry_errors(entry, catalogue_axes(root=root))
    # The name IS the file: an entry lives at tools/<name>.toml, with the name in the form
    # a file name takes, and no other entry claims it - the rule the gate holds every file
    # to (`entry_file_errors`). The file this writes, when it exists, is replaced rather
    # than counted as a second claim.
    if not errs:
        own = f"{file_form(args.name)}.toml"
        named, unreadable = _named_entries(root)
        for line in unreadable:
            print(f"[WARN] {line}: it counts as claiming the name its file gives")
        others = [t for t in named if t.get("_file") != own]
        errs += [
            why
            for name, why in entry_file_errors([*others, {**entry, "_file": own}])
            if name.lower() == args.name.lower()
        ]
    if errs:
        for err in errs:
            print(f"[FAIL] {args.name!r}: {err}")
        print(
            "\nnothing written. An entry is validated BEFORE it lands rather than after: a "
            "rejected file left on disk is one `git add` away from being committed, and the "
            "gate would be what found it - one commit too late"
        )
        return 1

    path = tools_dir(root) / f"{file_form(args.name)}.toml"
    if path.exists() and not args.force:
        print(
            f"[FAIL] {path.relative_to(root).as_posix()} exists. Edit it, or pass --force to "
            "overwrite it - an entry carries its own history, and rewriting the file from "
            "flags discards every block already in it",
            file=sys.stderr,
        )
        return 1
    try:
        _write(root, path, entry_toml(entry))
    except UnsafeWrite as exc:
        print(f"[FAIL] {exc} - nothing written", file=sys.stderr)
        return 1

    # READ BACK, and compare. The writer above is this module's own and narrow by design, so
    # the one thing that must not be assumed is that what landed parses back to what was
    # judged. A file that does not is removed rather than reported: a half-written entry the
    # operator has been told about is still a file in the catalogue.
    landed = tomllib.loads(path.read_text(encoding="utf-8"))
    if landed != entry:
        path.unlink()
        print(
            f"[FAIL] {path.name} did not read back as the entry that was validated, so it was "
            f"removed. This is a defect in the engine's entry writer, not in the arguments",
            file=sys.stderr,
        )
        return 1

    # Bare commands, and the explanation on a line of its own: cmd hands a trailing `#`
    # and every word after it to the command as arguments (see next_steps).
    print(f"[OK] wrote {path.relative_to(root).as_posix()} - {entry['ring']} / {entry['axis']}")
    print("next:")
    print("  - radar gate")
    print("  - radar render")
    print()
    print("`radar gate` judges the entry, and `radar render` writes it into the artefacts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
