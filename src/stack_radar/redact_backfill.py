"""Map scoped names out of what is ALREADY tracked. One-shot, idempotent, re-runnable.

Redaction on ingest (sync_feedback.py) and on write (field_report.py) fixes the flow.
It does nothing for the files a catalogue committed before the flow existed - an
archived feedback report, a generated field report, or any other tracked text that
quoted a scoped name. This walks those.

Two kinds of leak, and both matter:

- CONTENT: the name in prose, in a code path, or in a mined session slug.
- FILENAME: a report can carry it in its own name, which is visible from a directory
  listing without opening anything. Renames go through `git mv` so the history of a
  report follows it.

THE HOME DIRECTORY, a third leak. `field_report` maps the home out of what it writes and
`sync_feedback` out of what it ingests (`redact.redact_home`), and every file committed
before they did can still carry the account: a field report in `claude_home` and each
project slug, an archived feedback report in a path it quotes, a spec in where a session
ran, a profile in the paths it declares. So the home pass walks every tracked text file,
the same reach as the scoped-name pass. A profile keeps working afterwards because every
reader of a profile expands a leading `~` (`radar_lib.expand_home`). The home is asked of
`bootstrap.home_dir()` at run time, never named here.

A HOME THIS MACHINE DOES NOT HAVE is the fourth. A report written on another workstation
and synced in carries that machine's home, and `home_dir()` answers with this account
whatever the report says - so those homes are DECLARED, in the gitignored
`redact.local.toml` at the data root (`redact.extra_homes`), and go through the same
mapping. No file, no entries: the pass is exactly what it was.

`--check` writes nothing and exits 1 when any pass would change a file, so the answer is
the exit code and not a sentence someone has to read - which is what lets a commit hook
run it.

WHAT THIS DOES NOT DO: rewrite git history. Rewriting already-pushed history to scrub
a string buys little - every clone keeps the old commits. A catalogue that is made
public starts a fresh history from the current tree rather than filtering the old one.

    radar redact-backfill [--check]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .bootstrap import home_dir
from .paths import is_link, write_error, write_inside
from .radar_lib import begin_command
from .redact import (
    EXTRA_HOMES_FILE,
    extra_homes,
    has_scoped,
    redact_homes,
    redact_name,
    redact_text,
    scoped_terms,
)
from .trust import git

# Files that must keep the real name to do their job. tools.local/ is untracked so it
# never reaches here, but being explicit costs nothing and states the intent: the
# mapping itself is the one place the name is allowed to live.
SKIP_DIRS = {"tools.local", ".git"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2"}


def tracked(root: Path) -> list[Path]:
    """Every file git tracks in the data root.

    `cwd=root`, never the process's working directory: the backfill rewrites the tracked
    files of the CATALOGUE, and once the root is resolved from a marker the two are no
    longer the same place - run from a subdirectory, `git ls-files` would enumerate that
    subtree and the walk would silently cover part of the repository.

    A git that cannot answer is a FAIL and not an empty list, the same rule the invariant
    scan follows: an enumeration that failed and one that found nothing produce the
    same "0 files" sentence, and only one of them means the tree is clean.

    git runs through `trust.git`, so the configuration a repository turns into a command
    (an fsmonitor, a diff program) is emptied on the command line and cannot run while the
    tracked files of a pull-request branch are read.
    """
    try:
        out = git(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[FAIL] git could not be run in {root} ({exc}); nothing was scanned")
        sys.exit(1)
    if out.returncode != 0:
        print(f"[FAIL] git ls-files failed in {root}; is it a git repository?")
        sys.exit(1)
    return [root / n for n in out.stdout.split(chr(0)) if n]


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="radar redact-backfill",
        description="Map machine-local tool names and the home directory out of every "
        "tracked text file, and out of tracked file names.",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="report only; write nothing, and exit 1 if any file would change",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)

    terms = scoped_terms()
    if not terms:
        # Not a reason to stop: the home pass below does not depend on a machine-local
        # entry existing.
        print("[NOTE] no machine-local entries declare a name; the scoped-name pass is empty")
    # This machine's home, plus every home DECLARED in the gitignored file at the root -
    # the ones a report written on another workstation carries and `home_dir()` cannot
    # find, because the account is not this one's.
    declared = extra_homes(root)
    homes = [home_dir(), *declared]
    if declared:
        print(f"[NOTE] {len(declared)} extra home(s) declared in {EXTRA_HOMES_FILE}")

    renamed: list[tuple[str, str]] = []
    rewritten: list[str] = []
    homed: list[str] = []
    collisions: list[str] = []
    refused: list[str] = []
    blocked: list[str] = []

    for f in tracked(root):
        rel = f.relative_to(root)
        if set(rel.parts) & SKIP_DIRS or rel.suffix.lower() in BINARY_SUFFIXES:
            continue
        # A tracked symbolic link, or a file below one, is neither read nor rewritten:
        # its content is wherever the link points, which may be outside the catalogue.
        unsafe = write_error(root, f)
        if unsafe:
            refused.append(unsafe)
            continue

        # Content first, then the rename: doing it the other way round would leave the
        # rewrite reading a path that no longer exists.
        if f.is_file():
            try:
                raw = f.read_bytes().decode("utf-8", "surrogateescape")
            except OSError:
                continue
            # The homes before the scoped names, in the same order the writers apply
            # them, so a scoped term cannot split a home path.
            new = redact_homes(raw, homes)
            if new != raw:
                homed.append(rel.as_posix())
            if terms and has_scoped(new, terms):
                new = redact_text(new, terms)
                rewritten.append(str(rel))
            if new != raw and not args.check:
                write_inside(root, f, new.encode("utf-8", "surrogateescape"))

        # EVERY path component, not only the file's own name. A tracked path can carry the
        # private name as a DIRECTORY component (a report directory named for the entry),
        # and the gate reports a leak anywhere in the path, so the rename that clears it
        # maps each component.
        new_rel = Path(*[redact_name(part, terms) for part in rel.parts])
        if new_rel == rel:
            continue
        target = root / new_rel
        # The destination is held to the rule every write follows: a link at a mapped
        # directory component would carry `git mv`, and the directory created for it, out
        # of the data root.
        unsafe = write_error(root, target)
        if unsafe:
            blocked.append(f"{rel.as_posix()} not renamed: {unsafe}")
            continue
        if target.exists() or is_link(target):
            # Two source names mapping onto one is a real possibility once a tool has
            # more than one spelling. It is never resolved silently: a lost report is
            # worse than a leak that is still reported.
            collisions.append(f"{rel.as_posix()} -> {new_rel.as_posix()} (target already exists)")
            continue
        # A mapped directory component can already be a FILE, and then no directory can
        # be made there.
        in_the_way = [d for d in new_rel.parents if (root / d).is_file()]
        if in_the_way:
            collisions.append(
                f"{rel.as_posix()} -> {new_rel.as_posix()} "
                f"({in_the_way[0].as_posix()} is a file, not a directory)"
            )
            continue
        renamed.append((rel.as_posix(), new_rel.as_posix()))
        if not args.check:
            # git mv needs the destination directory to exist, and a mapped directory
            # component is a new one.
            target.parent.mkdir(parents=True, exist_ok=True)
            mv = git(
                ["git", "mv", "--", rel.as_posix(), new_rel.as_posix()],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if mv.returncode != 0:
                f.rename(target)  # not yet staged; a plain rename is equivalent

    verb = "would rewrite" if args.check else "rewrote"
    verb2 = "would rename" if args.check else "renamed"
    verb3 = "would map" if args.check else "mapped"
    for c in collisions:
        print(f"[FAIL] collision: {c}")
    for r in refused:
        print(f"[FAIL] {r} - not read or rewritten")
    for b in blocked:
        print(f"[FAIL] {b}")
    for old, new in renamed:
        print(f"[{verb2.split()[-1]}] {old} -> {new}")
    for rel in homed:
        print(f"[home] {rel}")
    print(
        f"\n{verb} {len(rewritten)} file(s) - {verb2} {len(renamed)} - "
        f"{len(collisions)} collision(s)"
    )
    print(f"{verb3} the home directory out of {len(homed)} file(s)")
    # The feedback indexes list the archive's file names; nothing else this renames is
    # indexed, so the note is printed only when a renamed path is under feedback/.
    if not args.check and any(old.startswith("feedback/") for old, _ in renamed):
        print(
            "[NOTE] feedback/ filenames changed; regenerate the indexes "
            "(`radar sync-feedback` does it on the next ingest)"
        )
    if collisions or refused or blocked:
        return 1
    if args.check and (rewritten or renamed or homed):
        print("[FAIL] --check: the tracked tree still carries what this script maps out")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
