"""Fail a change that moves the radar's code without recording the change.

THE OPT-OUT IS HALF THE RULE. A change that nothing a changelog reader could notice
declares itself with a `Changelog: none (<reason>)` trailer; without an escape hatch a
gate like this gets bypassed rather than obeyed. Each commit is judged on its own, merge
resolutions included, because judging a whole pushed range at once, or skipping merge
commits outright, is wrong in the ways covered below under "Judged per commit" through
"root commits".

What is specific to this engine, and why:

- `ENGINE_PREFIXES` is `src/`: what this repository ships as software.
- `CONTRACT_SURFACES` names what something else reads: the report schema, the closed
  `[telemetry]` key set and the schema it validates, and the gate's exit codes.

BOTH ARE DEFAULTS, and the defaults are this repository's own. One engine serves any
number of catalogues, and a catalogue's answer is different: its code is not `src/` and
usually is not code at all - its content is entries, which already record themselves in a
dated `[[history]]` block with a reason (and evidence at adopt and pilot) that the schema
checks, so demanding a changelog line as well would put one fact in two places. So a
repository that carries a `radar.toml` declares its own in `[changelog]`, and `watched()`
reads it. Inheriting `src/` into a repository that has no `src/` is the failure mode worth
naming: a prefix that matches nothing charges nobody with anything, and the gate passes
every run in silence while watching an empty set. An empty declaration is refused for the
same reason, loudly, rather than being read as "watch nothing".

Three checks and one advisory, all over the merge-base diff:

- **Record or declare.** Judged per commit: a commit whose own diff touches a watched
  prefix must be recorded by the pull request's `CHANGELOG.md` diff, or that commit carries the
  trailer `Changelog: none (<reason>)` - the opt-out for a change nothing a changelog
  reader could notice (comment wording, an internal rename). Touching `CHANGELOG.md` is
  not recording, and neither is rearranging what it already said: a record is one added
  line whose whitespace-collapsed form the same diff does not also remove, so an
  appended blank line, a trailing space or a re-indent on an existing line, a CRLF
  conversion and a reordering of existing bullets all record nothing (see `_records`). A
  trailer declares only the commit it rides on, not the whole range. A merge commit is
  not skipped, and it is charged only with its *resolution* - the content an automatic
  merge of its parents would not have produced (`git merge-tree --write-tree`, diffed
  against the merge's own tree; git 2.38 or newer, and the gate fails naming the git it
  ran rather than guessing on an older one). Unrelated histories are the exception:
  there is no automatic merge to compare against, so the combined diff stands in - a
  conservative approximation that under-charges a resolution taking one root verbatim. A
  hand resolution that edits a watched prefix needs the same recording as any other; a
  clean auto-merge, including the synthetic `refs/pull/N/merge` CI checks out,
  contributes nothing and is charged nothing. An octopus merge is outside
  `--write-tree`'s two-parent scope and keeps the combined (`--cc`) diff, which
  OVER-charges: an octopus git merged cleanly is charged with the files its branches
  both touched, though no one authored the result. An octopus merge is rare, and the
  trailer on the octopus commit is the remedy when one occurs.
  Granularity has a cost of its own: an intermediate commit's engine change that a later
  commit in the same range reverts still needs its own trailer or entry, even though the
  range's final diff never shows it - where pull requests integrate by merge commit rather
  than squash, a WIP commit like that stays in the history, so it stays attributable. A
  non-merge commit is diffed with `--root`, so a root commit (a repository's first commit,
  or the bottom of an unrelated-histories merge) contributes its paths instead of being
  silently skipped for having no parent.
- **A release heading moves the version forward.** When the diff adds a `## [X.Y.Z]`
  heading, `pyproject.toml` at HEAD must carry exactly `X.Y.Z` and the merge-base
  version must be smaller. That the version SITES agree is `tests/test_version_sites.py`'s
  job; this pins the heading to an actual bump.
- **Section headings keep the vocabulary.** An added `### ` heading must be one of
  Added / Changed / Deprecated / Removed / Fixed / Security. Only added lines are read,
  so history is grandfathered.
- **Consumer-affecting marker (advisory).** A diff touching a contract surface should say
  `(consumer-affecting)` somewhere in its CHANGELOG addition. Advisory deliberately: most
  edits to those files do not move the contract, so it prints a workflow warning and
  never fails. Escalate only once it has proven quiet on ordinary changes.

Stdlib only, like everything else here; `tests/test_changelog_gate.py` holds the red
proofs.

    radar changelog-gate origin/main
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Aliased: `MARKER` is already this module's `(consumer-affecting)` string.
from .paths import MARKER as MARKER_FILE
from .trust import git

RECORD = "CHANGELOG.md"

# What THIS repository ships as software, and the default for any repository that does not
# declare its own. See the module docstring for why a catalogue's answer differs.
ENGINE_PREFIXES = ("src/",)

# The files defining a surface something else keys on. `radar_lib.py` holds the entry
# schema and the closed [telemetry] key set that every entry is validated against;
# `field_report.py` writes the `field/<tool>/*.json` document another tool (or a later
# reader of an older report) parses; `gate.py` owns the exit code a CI step branches on.
CONTRACT_SURFACES = (
    "src/stack_radar/radar_lib.py",
    "src/stack_radar/field_report.py",
    "src/stack_radar/gate.py",
)

MARKER = "(consumer-affecting)"

# The reason is required, not decoration: a bare `Changelog: none` records that the rule
# was skipped, while the parenthesis records why, which is the reviewable part. Casing is
# folded - `changelog: None (...)` states the same decision, and rejecting it over case
# would produce the misleading error rather than the safer one.
_TRAILER = re.compile(r"^Changelog: none \(.+\)$", re.MULTILINE | re.IGNORECASE)
_ADDED_HEADING = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)

# The Keep a Changelog section vocabulary - the only `### ` headings this file uses.
SECTION_VOCABULARY = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_SECTION_HEADING = re.compile(r"^### (.*)$", re.MULTILINE)

# What `git merge-tree --write-tree` prints on its first line when it wrote a tree -
# SHA-1 or SHA-256, so the object id is 40 or 64 hex digits.
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# The ONE non-tree outcome that is about this merge rather than about this git, and so
# the one that may be answered with the combined diff instead of a failure. Everything
# else raises - see _auto_merge_tree, and note the direction: an unrecognised message
# must fail, never fall back, or the machine that cannot detect the bug is the machine
# that silently keeps it.
#
# Matching the message and not the exit code is deliberate: git 2.38 exits 128 for this,
# for a bad rev, and for a repository it cannot read. Matching a guess at what an OLD git
# says does not work either: `git merge-tree --write-tree A B` is four arguments, which is
# exactly what pre-2.38 merge-tree expects, so it never prints usage - it reaches
# get_tree_descriptor and dies `unknown rev --write-tree`, which no old-git pattern here
# would have caught. The gate runs merge-tree under LC_ALL=C so this text is the text.
_SAFE_REFUSAL = re.compile(r"refusing to merge unrelated histories", re.I)

Version = tuple[int, int, int]


class DeclarationRefused(Exception):
    """A `[changelog]` block that cannot be obeyed as written."""


def watched(root: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(prefixes, contract surfaces)` for the repository at `root`, defaulting to cwd.

    THE REPOSITORY OF THE WORKING DIRECTORY, and not a resolved data root: this verb and
    `check-version-sites` judge whatever tree they are run inside, which
    is how this engine's own CI runs them in a repository that has no marker at all.
    Consulting `RADAR_DATA_ROOT` here would let an exported variable point the gate at one
    repository while it read another one's commits - so the marker is looked for in the
    directory being judged, and nowhere else.
    """
    marker = (root or Path.cwd()) / MARKER_FILE
    if not marker.is_file():
        return ENGINE_PREFIXES, CONTRACT_SURFACES
    try:
        declared = tomllib.loads(marker.read_text(encoding="utf-8")).get("changelog") or {}
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationRefused(
            f"{marker}: {MARKER_FILE} is not valid TOML ({exc}), so the watched paths it may "
            "declare could not be read"
        ) from exc

    def named(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        if key not in declared:
            return default
        value = declared[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise DeclarationRefused(
                f"{marker}: [changelog].{key} must be a list of strings, and an unreadable "
                f"declaration is refused rather than ignored - ignoring it would silently "
                f"restore {default}, which is this engine's answer and not this repository's"
            )
        if not value:
            raise DeclarationRefused(
                f"{marker}: [changelog].{key} is empty. A gate watching nothing charges "
                f"nobody with anything and passes every run in silence; omit the key to "
                f"take the default ({', '.join(default)}) rather than declaring no watch"
            )
        return tuple(value)

    return named("prefixes", ENGINE_PREFIXES), named("contract_surfaces", CONTRACT_SURFACES)


def evaluate(
    changed: list[str],
    commits: list[tuple[list[str], str]],
    changelog_added: str,
    changelog_removed: str,
    base_version: Version | None,
    head_version: Version | None,
    *,
    prefixes: tuple[str, ...] = ENGINE_PREFIXES,
    surfaces: tuple[str, ...] = CONTRACT_SURFACES,
) -> tuple[list[str], list[str]]:
    """The whole decision, pure: `(errors, warnings)` for one range's diff.

    `changed` is the merge-base changed-file list, read by the contract-surface advisory
    alone - it judges the range's final shape rather than any one commit. (The
    release-heading and section-vocabulary checks read `changelog_added`; the
    record-or-declare check reads `commits`.) `commits` is one `(paths, message)` pair
    per commit in the range, merges included: `paths` that commit's *own* diff (a
    merge's own resolution, an ordinary commit's diff against its parent), `message`
    that commit's own message - the record-or-declare check needs this per-commit, not
    range-wide, or a trailer on one commit exempts every commit, and a commit's own
    engine touch (a merge's own conflict resolution included) could be missed inside the
    range's aggregate. `changelog_added` and `changelog_removed` are the two sides of
    CHANGELOG.md's diff across the whole range (without the `+`/`-` prefixes) - a shared
    record, since one entry can cover several commits - and the versions come from
    `pyproject.toml` at each end. Both sides are needed because an added line is a
    record only if the same diff does not also remove it: see `_records`.
    """
    errors: list[str] = []
    warnings: list[str] = []
    paths = [path.strip().replace("\\", "/") for path in changed if path.strip()]
    recorded = _records(changelog_added, changelog_removed)

    for raw_paths, message in commits:
        commit_paths = [path.strip().replace("\\", "/") for path in raw_paths if path.strip()]
        engine_paths = [path for path in commit_paths if path.startswith(prefixes)]
        if engine_paths and not recorded and not _TRAILER.search(message):
            listed = ", ".join(engine_paths[:5]) + (" ..." if len(engine_paths) > 5 else "")
            subject = next(iter(message.splitlines()), "(empty message)")
            errors.append(
                f'commit "{subject}" changes the radar\'s code ({listed}) without the '
                f"diff adding lines to {RECORD}. Record the change under the open "
                f"release heading, or declare the exemption on that commit with a "
                f"trailer: Changelog: none (<reason>)"
            )

    headings = _ADDED_HEADING.findall(changelog_added)
    if headings:
        # Unpacked rather than built by comprehension: the pattern has exactly three
        # groups, but a generator only ever produces a `tuple[int, ...]`, and `Version`
        # is three. Saying so here is what lets the type checker see the rest.
        major, minor, patch = (int(part) for part in headings[0])
        cut: Version = (major, minor, patch)
        if head_version is not None and cut != head_version:
            errors.append(
                f"the diff adds a release heading [{_dotted(cut)}] but pyproject.toml is "
                f"at {_dotted(head_version)}; the heading and the version site move together"
            )
        if base_version is not None and cut <= base_version:
            errors.append(
                f"the diff adds a release heading [{_dotted(cut)}] that does not move the "
                f"version forward from {_dotted(base_version)}"
            )

    rogue = [
        name.strip()
        for name in _SECTION_HEADING.findall(changelog_added)
        if name.strip() not in SECTION_VOCABULARY
    ]
    if rogue:
        listed = ", ".join(f"### {name}" for name in rogue)
        errors.append(
            f"the diff adds a CHANGELOG section heading outside the vocabulary ({listed}); "
            f"this file uses exactly {' / '.join(SECTION_VOCABULARY)}"
        )

    touched_surfaces = [path for path in paths if path in surfaces]
    if touched_surfaces and MARKER not in changelog_added:
        warnings.append(
            f"the diff touches a contract surface ({', '.join(touched_surfaces)}) and its "
            f"CHANGELOG addition never says {MARKER}. If the change adds or renames a "
            f"report field, a [telemetry] key, or an exit code, mark the entry; if not, "
            f"ignore this - it is advisory while it earns (or loses) its escalation."
        )

    return errors, warnings


def _dotted(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _collapsed(blob: str) -> list[str]:
    """Each line with every run of whitespace collapsed to one space and the ends
    trimmed, so a re-indent, a trailing space and a `\\r` line terminator all fold onto
    the same form as the line they churn."""
    return [" ".join(line.split()) for line in blob.splitlines()]


def _records(added: str, removed: str) -> bool:
    """Whether a CHANGELOG diff records anything.

    A record is one added line that is non-blank once collapsed *and* that the same
    diff does not also remove in collapsed form. Stripping the added blob alone is not
    enough: a trailing space on an existing line adds a line that survives `strip()`
    while telling a changelog reader nothing it could not already read. Reordering,
    re-indenting and converting the file's line endings are the same shape.
    """
    gone = set(_collapsed(removed))
    return any(line and line not in gone for line in _collapsed(added))


def _git(*args: str) -> str:
    # Through `trust.git`: this verb runs on an unreviewed branch (its own CI, a
    # catalogue's), and the diff and merge-tree calls below would otherwise run an
    # external diff or textconv a committed config names. `GIT_SAFE_CONFIG` empties those.
    return git(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def _auto_merge_tree(written: subprocess.CompletedProcess[str], git_version: str) -> str | None:
    """The tree id `git merge-tree --write-tree` wrote, `None` if there is no automatic
    merge to compare against, or a loud failure if this git cannot compute one.

    Three outcomes, and the difference between the last two is the whole point:

    Exit 1 is a *conflicted* automatic merge, not an error: merge-tree still writes a
    tree - the conflict markers are in it - and that tree is exactly what tells a hand
    resolution from an automatic one, so it is used like any other.

    `refusing to merge unrelated histories` is a property of the *merge*, and it is not
    an error at all: there is no automatic merge of unrelated histories, so there is
    nothing to diff the result against. `None` says that, and the caller falls back to
    the combined diff. That fallback is a conservative approximation and not an
    equivalent reading: `-c` lists what differs from *every* parent, so a resolution
    that takes one root's file verbatim matches that root and goes uncharged, where the
    module's own rule ("no automatic merge, so all of it is authored") would charge it.
    Accepted because the alternative - charging both roots entire - is worse, and
    because the trailer remains available on the merge.

    Anything else raises, naming the git it ran under, and the job says why. That
    includes a git too old for `--write-tree`, which is a property of the *machine* and
    holds for every merge in the range: falling back there would give the combined-diff
    answer on exactly the machines that cannot tell it is wrong. The classification is by the
    message and in this direction on purpose: keying on a guess at what an old git
    prints is the wrong direction, because `git merge-tree --write-tree A B` is the four
    arguments pre-2.38 merge-tree expects, so it never prints usage - it dies `unknown
    rev --write-tree`, which such a guess does not match, so it would fall through to the
    silent fallback. An unrecognised message therefore fails loudly.
    """
    first = next(iter(written.stdout.splitlines()), "").strip()
    if _OBJECT_ID.fullmatch(first):
        return first
    said = written.stderr.strip() or written.stdout.strip() or "(no output)"
    if _SAFE_REFUSAL.search(said):
        return None
    headline = next(iter(said.splitlines()), said)
    raise RuntimeError(
        f"git merge-tree --write-tree wrote no tree (exit {written.returncode}) under "
        f"{git_version.strip()}, and did not refuse for a reason this gate can read as "
        f"a fact about the merge. Charging a merge commit with its resolution needs git "
        f"2.38 or newer. It said: {headline}"
    )


def _diff_side(diff: str, sign: str) -> str:
    """One side of a unified diff without its prefixes: `+` for the added lines, `-`
    for the removed, each skipping that side's `+++`/`---` file header."""
    header = sign * 3
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith(sign) and not line.startswith(header)
    )


def _version_of(pyproject: str) -> Version | None:
    raw = tomllib.loads(pyproject)["project"]["version"]
    parts = raw.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return (major, minor, patch)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar changelog-gate",
        description="Fail a change that moves this repository's code without recording it.",
    )
    ap.add_argument(
        "base_ref",
        nargs="?",
        default="origin/main",
        help="the ref this range is judged against (default: origin/main)",
    )
    return ap


def _command_failure(exc: subprocess.CalledProcessError) -> str:
    """One line naming what a failed git call said, for a `[FAIL]` that never traces back
    through a stack a caller cannot act on."""
    cmd = " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
    said = (exc.stderr or exc.stdout or "").strip()
    headline = next(iter(said.splitlines()), f"exit {exc.returncode}")
    return f"`{cmd}` failed: {headline}"


def main(argv: list[str]) -> int:
    # Read before argparse ever sees it: a base ref that starts with `-` is not this
    # positional's job to reject gracefully - argparse would read it as an unknown
    # option and print its own usage error, not the one-line [FAIL] a caller of this
    # gate can act on. `-h`/`--help` are the one exception, and stay routed to argparse.
    candidate = next((a for a in argv if a not in ("-h", "--help")), None)
    if candidate is not None and candidate.startswith("-"):
        print(f"[FAIL] changelog-gate: base ref {candidate!r} looks like an option, not a ref")
        return 2

    args = _build_parser().parse_args(argv)
    base_ref = args.base_ref

    try:
        prefixes, surfaces = watched()
    except DeclarationRefused as exc:
        print(f"::error::{exc}")
        return 2

    try:
        merge_base = _git("merge-base", base_ref, "HEAD").strip()

        changed = _git("diff", "--name-only", f"{merge_base}..HEAD").splitlines()

        git_version = _git("--version")
        commit_shas = _git("log", "--format=%H", f"{merge_base}..HEAD").split()
        commits: list[tuple[list[str], str]] = []
        for sha in commit_shas:
            parents = _git("log", "-1", "--format=%P", sha).split()
            if len(parents) == 2:
                # A merge's own contribution is its resolution: what an automatic merge of
                # its parents would *not* have produced. `git merge-tree --write-tree` writes
                # that automatic merge (conflict markers included) and prints its tree, so
                # diffing that tree against the merge's own tree leaves exactly the content a
                # human put there. `--cc --name-only` cannot make this distinction - in
                # name-only mode `--cc` is `-c`, which lists every file whose result differs
                # from all parents, and a clean auto-merge of two hunks in one file differs
                # from both. `--cc` would charge content nobody authored, and on the
                # synthetic refs/pull/N/merge CI checks out no trailer can be added: once the
                # base branch touches the same engine file, the pull request would fail with
                # no remedy its author could apply.
                written = git(
                    ["git", "merge-tree", "--write-tree", parents[0], parents[1]],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    # _SAFE_REFUSAL reads git's own words, so they must not be localized.
                    env={**os.environ, "LC_ALL": "C"},
                )
                auto = _auto_merge_tree(written, git_version)
                if auto is None:
                    # git refused this pair outright (unrelated histories), so there is no
                    # automatic merge to diff against. The combined diff is the conservative
                    # approximation left: it under-charges a resolution that takes one root's
                    # file verbatim, and the trailer stays available for the rest.
                    own_paths = _git(
                        "diff-tree", "--cc", "--no-commit-id", "--name-only", "-r", sha
                    ).splitlines()
                else:
                    own_paths = _git(
                        "diff-tree", "--no-commit-id", "--name-only", "-r", auto, sha
                    ).splitlines()
            elif len(parents) > 2:
                # An octopus is outside `--write-tree`'s two-parent scope (and reducing it to
                # its first two parents would charge it the other branches' content), so it
                # keeps the combined diff. This OVER-charges: `-c` lists files differing from
                # every parent, which for a clean automatic octopus over two hunks of one
                # file is still nobody's authorship. Left as the conservative reading because
                # a wrong charge has a remedy the author can apply (the trailer) and a missed
                # one does not, and because an octopus merge is rare.
                own_paths = _git(
                    "diff-tree", "--cc", "--no-commit-id", "--name-only", "-r", sha
                ).splitlines()
            else:
                own_paths = _git(
                    "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha
                ).splitlines()
            own_message = _git("log", "-1", "--format=%B", sha)
            commits.append((own_paths, own_message))

        # --no-ext-diff --no-textconv: this is a content diff, so a repository's
        # diff.external or a diff driver's textconv program would otherwise run on the
        # branch being judged. (GIT_SAFE_CONFIG cannot empty diff.external - an empty one
        # is itself spawned - so it is disabled by flag here, the one content diff.)
        changelog_diff = _git(
            "diff", "--no-ext-diff", "--no-textconv", f"{merge_base}..HEAD", "--", RECORD
        )
        changelog_added = _diff_side(changelog_diff, "+")
        changelog_removed = _diff_side(changelog_diff, "-")

        head_version = _version_of(Path("pyproject.toml").read_text(encoding="utf-8"))
        try:
            base_version = _version_of(_git("show", f"{merge_base}:pyproject.toml"))
        except subprocess.CalledProcessError:  # a history with no pyproject at the base
            base_version = None
    except subprocess.CalledProcessError as exc:
        print(f"[FAIL] changelog-gate: {_command_failure(exc)}")
        return 2
    except RuntimeError as exc:
        # `_auto_merge_tree` raises this for a git too old (or otherwise unreadable)
        # to compute an automatic merge - a fact about the machine, not a traceback
        # the caller of this gate could do anything with.
        print(f"[FAIL] changelog-gate: {exc}")
        return 2

    errors, warnings = evaluate(
        changed,
        commits,
        changelog_added,
        changelog_removed,
        base_version,
        head_version,
        prefixes=prefixes,
        surfaces=surfaces,
    )
    for warning in warnings:
        print(f"::warning::{warning}")
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        # The watched set is printed on the way out, not only on failure. A gate that says
        # "OK" without saying what it looked at reads the same whether it watched three
        # prefixes or none, and watching none is the way this check fails silently.
        print(
            f"OK: the change is recorded, declared, or carries no recording obligation "
            f"(watched: {', '.join(prefixes)})."
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
