"""Reconcile the data-plane feedback inbox with the control-plane archive.

WHY THIS EXISTS (two rules that collide, and how both hold)
-----------------------------------------------------------
The feedback archive lives in the catalogue, so it travels between machines and can be
triaged across tools. The control-plane invariant forbids any data-plane file from
pointing into the catalogue, because that would make a working session depend on the
control plane: delete the catalogue and the feedback directory a consumer writes to would
vanish.

Both hold at once with an inbox:

    session -> writes -> INBOX   {feedback_root}/<tool>/      (data plane, self-sufficient)
                            |
                       ingest v  (this command)
                         ARCHIVE feedback/<tool>/             (control plane, synced, indexed)
                            |
                       mirror v  (this command)
                         INBOX   full corpus + INDEX.md

The inbox is what the rendered `feedback-targets.toml` registers, so nothing in the
data plane names the catalogue. The archive is what git carries between environments.
Reports are append-only with datestamped names, so the merge is a union; INDEX.md is
regenerated, never merged - whenever it is missing or older than a report it lists.

"Append-only" is about the corpus, not about each file: a report may be extended after
it was ingested (a session adds a closing section, say). An inbox copy that EXTENDS the
archived one is an amendment, and the archive takes the longer copy. Only two copies of
which neither extends the other are a conflict - two reports written under one name - and
those are refused rather than resolved.

Every write lands inside the directory it belongs to: an inbox copy under
`feedback_root/<tool>`, an archived copy under `feedback/<tool>`. Both the tool's name
and a report's mapped name come from catalogue data, so each destination is resolved and
refused when it lands anywhere else. The tool's name must also be one plain path
component under the schema's name rule (`radar_lib.NAME_RULE`). A tool whose archive is
reached through a symbolic link - `feedback/` or `feedback/<tool>` - is refused whole:
nothing is ingested, mirrored or indexed for it, since each report would still resolve
inside the directory the link leads to.

The index builder is a script the catalogue names, run with this interpreter, so it runs
only once the operator has approved that script as it stands (trust.py), in an empty
scratch directory like every catalogue command. A builder that fails or times out is a
FAIL: the index it should have written is then behind the archive.

If the catalogue disappears: the inbox keeps its last mirrored state and sessions keep
appending to it. Degraded (no cross-environment merge) but fully functional.

    radar sync-feedback --env <name> [--check] [--no-mirror] [--trust-commands]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import NamedTuple

from .bootstrap import home_dir
from .paths import feedback_dir, is_link, write_error, write_inside
from .radar_lib import (
    NAME_RULE,
    begin_command,
    default_environment,
    environment_name,
    is_plain_name,
    load_environment,
    load_tools,
    reports_profile_errors,
    tools_for_environment,
)
from .redact import extra_homes, redact_homes, redact_name, redact_text, scoped_terms
from .trust import (
    FLAG,
    INDEX_BUILDER,
    Trust,
    command_env,
    scratch_dir,
    strictly_inside,
)


class Builder(NamedTuple):
    """The index builder that resolved, and the entry that declares it."""

    tool: str
    path: Path


# The index builder lives in whichever own-ring worktree declares it, never a name
# hardcoded here: a tool entry carries `[feedback].index_builder`, relative to its own
# `worktree`, and this command stays honest when the checkout moves or the entry that
# ships the builder gets renamed. Entries are tried in the order `load_tools()` already
# loads them (by file name), and the first one whose declared path RESOLVES wins - not
# the first that merely declares one. A dead key (the worktree moved, the file was
# renamed) has to fall through to the next candidate rather than stop the search, or a
# single stale entry would blind every other own-ring tool's index regeneration.
def index_builder(tools: list[dict], paths: dict) -> Builder | None:
    for t in tools:
        fb = t.get("feedback") or {}
        rel = fb.get("index_builder")
        if not rel:
            continue
        wt = fb.get("worktree") or t.get("repo") or ""
        for key, val in paths.items():
            wt = str(wt).replace("{" + key + "}", str(val))
        cand = Path(str(wt).replace("\\", "/")) / str(rel)
        if cand.is_file():
            return Builder(str(t.get("name")), cand)
    return None


def reports(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.name != "INDEX.md")


def index_stale(archive: Path) -> bool:
    """Whether the archive's INDEX.md is missing or older than a report it should list.

    Decided from the archive as it stands, not from what one run ingested: a run that
    ingested reports while the index builder was pending leaves the index behind, and the
    next run - the one with `--trust-commands`, say - ingests nothing new. Keyed on this
    run alone, that run would never ask for the builder and the index would stay stale.
    """
    held = reports(archive)
    if not held:
        return False
    try:
        built = (archive / "INDEX.md").stat().st_mtime
    except OSError:
        return True
    return any(p.stat().st_mtime > built for p in held)


def _redacted(
    p: Path, terms: list[tuple[str, str]], homes: Sequence[PurePath | None] = ()
) -> bytes:
    """File content with the home directories and the scoped names mapped out, in that
    order - the order `field_report` writes in, so a scoped term cannot split a home path.
    Bytes in, bytes out: reading and writing text on Windows would translate newlines and
    make every comparison of an already-ingested report differ from itself."""
    raw = p.read_bytes().decode("utf-8", "surrogateescape")
    return redact_text(redact_homes(raw, homes), terms).encode("utf-8", "surrogateescape")


@dataclass
class Ingested:
    """What one inbox -> archive pass found, by archived report name."""

    new: list[str] = field(default_factory=list)
    # The inbox copy extends the archived one: the archive takes the longer copy.
    amended: list[str] = field(default_factory=list)
    # The archived copy extends the inbox one (amended on another machine): the inbox is
    # behind, and the mirror refreshes it. (inbox file, archived file).
    behind: list[tuple[Path, Path]] = field(default_factory=list)
    # Neither copy extends the other: two reports under one name.
    conflict: list[str] = field(default_factory=list)
    # A mapped name that would land outside the archive directory.
    refused: list[str] = field(default_factory=list)
    # A destination the engine does not write: a symbolic link, or a path below one
    # (`paths.write_error`). The reason, as printed.
    unsafe: list[str] = field(default_factory=list)


def ingest(
    inbox: Path,
    archive: Path,
    terms: list[tuple[str, str]],
    check: bool,
    homes: Sequence[PurePath | None] = (),
    root: Path | None = None,
) -> Ingested:
    """inbox -> archive, mapping scoped names (redact.py) out of both the content AND the
    filename, and the home directories out of the content.

    The filename matters as much as the content: a report called `<date>-<name>-v1.md`
    leaks from the directory listing alone. The home is the other thing a session writes
    without anyone choosing to: a report quotes the path it worked in, and the archive is
    tracked. The inbox keeps the real paths, because it lives on the machine that owns
    them.

    `homes` is a LIST rather than one home because the inbox merges corpora: a report
    that reaches it was not necessarily written here, and the homes this machine cannot
    find are declared (`redact.extra_homes`) instead of discovered.

    The comparison is against the REDACTED source, not the raw one. Comparing raw
    bytes would report every already-ingested report as a conflict forever, since the
    archived copy is by construction not byte-equal to what the inbox holds. And it is a
    prefix comparison, not only an equality: a report extended after it was ingested is
    the same report grown, which the archive should follow, not a second report that
    happens to share a name.

    Every write goes through `paths.write_inside` under `root` (the data root; the
    archive itself when not given), so no archived copy is written through a link.
    """
    out = Ingested()
    for p in reports(inbox):
        name = redact_name(p.name, terms)
        target = archive / name
        # The mapped name comes from a placeholder the catalogue declares, so it is
        # resolved before anything is written under it.
        if not strictly_inside(target, archive):
            out.refused.append(name)
            continue
        problem = write_error(root or archive, target)
        if problem:
            out.unsafe.append(problem)
            continue
        body = _redacted(p, terms, homes)
        if target.is_file():
            held = target.read_bytes()
            if held == body:
                continue
            if body.startswith(held):
                out.amended.append(name)
                if not check:
                    write_inside(root or archive, target, body)
            elif held.startswith(body):
                out.behind.append((p, target))
            else:
                out.conflict.append(name)
            continue
        out.new.append(name)
        if not check:
            write_inside(root or archive, target, body)
    return out


def archived_file(p: Path, archive: Path) -> bool:
    """Whether `p` is a report the archive really holds: a regular file, not a symbolic
    link, resolving inside the archive. A link a pull request committed there would
    otherwise have its target's content copied into the inbox, and from there archived."""
    return not is_link(p) and strictly_inside(p, archive) and p.is_file()


def mirror(
    archive: Path, inbox: Path, terms: list[tuple[str, str]], check: bool
) -> tuple[list[str], list[str]]:
    """archive -> inbox, for reports this machine has never seen. Returns (the reports
    mirrored, the archive entries refused because they are not `archived_file`s).

    Redaction makes this direction asymmetric: an archived report the inbox already
    holds under its UNREDACTED name is not new, and copying it would leave the corpus
    duplicated under two spellings. So coverage is decided on the redacted name.
    """
    covered = {redact_name(p.name, terms) for p in reports(inbox)}
    new: list[str] = []
    refused: list[str] = []
    for p in reports(archive):
        if p.name in covered or (inbox / p.name).is_file():
            continue
        if not archived_file(p, archive):
            refused.append(p.name)
            continue
        new.append(p.name)
        if not check:
            inbox.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, inbox / p.name)
    return new, refused


BUILDER_TIMEOUT = 300


def run_builder(script: Path, archive: Path) -> str | None:
    """Run the approved index builder on one archive directory. None when it succeeded,
    else what went wrong.

    Like every catalogue command it runs in an empty scratch directory with
    PYTHONSAFEPATH set (trust.py): the approval covers this one script, so neither the
    operator's working directory nor the script's own directory is on its import path.
    """
    try:
        with scratch_dir() as cwd:
            done = subprocess.run(  # noqa: S603 - an approved script, no shell
                [sys.executable, str(Path(script).resolve()), str(Path(archive).resolve())],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=BUILDER_TIMEOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=command_env(),
            )
    except subprocess.TimeoutExpired:
        return f"timed out after {BUILDER_TIMEOUT}s"
    except OSError as exc:
        return f"could not run: {exc}"
    if done.returncode != 0:
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        return f"exited {done.returncode}" + (f": {lines[-1].strip()[:110]}" if lines else "")
    return None


@reports_profile_errors
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="radar sync-feedback",
        description="Move feedback reports from each tool's inbox into the catalogue's "
        "archive, mirror archived reports this machine has not seen back into the inbox, and "
        "rebuild each tool's INDEX.md with its approved index builder.",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--env",
        type=environment_name,
        help="environment name (default: the top-level default_environment key of radar.toml)",
    )
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument(
        "--no-mirror",
        action="store_true",
        help="ingest only; do not push the merged corpus back to the inbox",
    )
    ap.add_argument(
        FLAG,
        dest="trust_commands",
        action="store_true",
        help="approve, record and run a pending [feedback].index_builder script an "
        "earlier `radar sync-feedback` run listed with the same id; one no such run "
        "listed, or changed since, is listed and not run",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)
    args.env = args.env or default_environment(root)
    trust = Trust(root, approve=args.trust_commands, verb="sync-feedback")

    env = load_environment(args.env)
    if env is None:
        print(f"[FAIL] no environment named {args.env!r}")
        return 1
    paths = env.get("paths") or {}
    feedback_root = paths.get("feedback_root")
    if not feedback_root:
        print(f"[FAIL] {args.env}: [paths].feedback_root is not set")
        return 1
    inbox_root = Path(str(feedback_root).replace("\\", "/"))

    tools = load_tools()
    # Targets are scoped to the environment, not global. Selecting them from every entry
    # would be a leak rather than a cosmetic bug: the radar's `feedback/` archive is
    # TRACKED, so the first report written for an environment-scoped tool would create
    # `feedback/<scoped-name>/` and carry the name into git - exactly what tools.local/
    # and the gate's scope-leak check exist to prevent.
    #
    # The index builder still sees every tool: it resolves paths for whatever it is given,
    # and narrowing it would break nothing but gains nothing either.
    targets = [t for t in tools_for_environment(env, tools) if (t.get("feedback") or {}).get("dir")]
    if not targets:
        print(f"[WARN] no tool in {args.env!r} declares [feedback]; nothing to sync")
        return 0

    # A scoped name never reaches the archive; see redact.py for why the alternative
    # (not archiving those reports at all) is worse.
    terms = scoped_terms(tools)
    # This machine's home, plus the ones declared for the machines whose reports reach
    # this inbox and whose accounts `home_dir()` cannot find (redact.extra_homes).
    homes = [home_dir(), *extra_homes(root)]
    builder = index_builder(tools, paths)
    drift = 0
    conflicts = 0
    # Refusals are counted apart from conflicts: a name or a report that is refused is
    # not two reports disagreeing, and the summary should not say it is.
    refused = 0
    builder_fails = 0
    archive_root = feedback_dir(root)
    archives: list[Path] = []

    for t in sorted(targets, key=lambda x: x["name"]):
        name = t["name"]
        inbox = inbox_root / name
        archive = archive_root / name
        # The name is catalogue data and names a directory in both places. It must be one
        # plain path component - `a/b` or `</x>` would nest or fail mid-write - and,
        # resolved, a directory under the one it belongs to: `../x` would move the inbox
        # and the archive somewhere else, and `.` would make them the roots that hold
        # every tool's; both are written to below.
        if not is_plain_name(name):
            print(
                f"[FAIL] {name}: this tool name is not a plain file name ({NAME_RULE}) - "
                "refused, nothing read or written for it"
            )
            refused += 1
            continue
        if not strictly_inside(inbox, inbox_root) or not strictly_inside(archive, archive_root):
            print(
                f"[FAIL] {name}: this tool name resolves outside feedback_root or the "
                "archive, or onto one of them - refused, nothing read or written for it"
            )
            refused += 1
            continue
        # The same rule every write into the data root answers to, asked once for the
        # whole target: a link at feedback/ or at feedback/<name>, or a path that resolves
        # outside the data root, would carry the mirror, the index builder and the INDEX.md
        # copy to a directory the catalogue does not hold, while each report still
        # resolves inside the resolved archive.
        problem = write_error(root, archive / "INDEX.md")
        if problem:
            print(f"[FAIL] {name}: {problem} - refused, nothing read or written for it")
            refused += 1
            continue
        archives.append(archive)

        got = ingest(inbox, archive, terms, args.check, homes, root)
        conflicts += len(got.conflict)
        refused += len(got.refused) + len(got.unsafe)
        for c in got.unsafe:
            print(f"[FAIL] {name}: {c} - refused")
        for c in got.conflict:
            # Same name, and neither copy extends the other: two different reports were
            # written under one name. Never resolved automatically - either could be the
            # one that matters.
            print(
                f"[FAIL] {name}: {c} differs between inbox and archive and neither copy "
                "extends the other - two reports share one name; rename one report"
            )
        for c in got.refused:
            print(f"[FAIL] {name}: {c} would be archived outside {archive.as_posix()} - refused")
        for c in got.amended:
            where = (archive / c).relative_to(root).as_posix()
            tail = " (--check: not written)" if args.check else ""
            print(f"[AMENDED] {where} - extended after it was ingested{tail}")

        for src, held in got.behind:
            if args.check or args.no_mirror:
                print(
                    f"[NOTE] {name}: the inbox copy of {held.name} is an earlier version of "
                    "the archived one" + ("" if args.no_mirror else " (--check: not written)")
                )
                continue
            shutil.copyfile(held, src)
            print(f"[AMENDED] {src.as_posix()} - refreshed from the archive's longer copy")

        mirrored: list[str] = []
        if not args.no_mirror:
            mirrored, not_mirrored = mirror(archive, inbox, terms, args.check)
            refused += len(not_mirrored)
            for c in not_mirrored:
                print(
                    f"[FAIL] {name}: feedback/{name}/{c} is a symbolic link or resolves "
                    "outside the archive - not mirrored"
                )

        changed = len(got.new) + len(got.amended) + len(got.behind) + len(mirrored)
        if got.new or mirrored:
            verb = "would ingest" if args.check else "ingested"
            verb2 = "would mirror" if args.check else "mirrored"
            bits = []
            if got.new:
                bits.append(f"{verb} {len(got.new)}")
            if mirrored:
                bits.append(f"{verb2} {len(mirrored)}")
            print(f"[NOTE] {name}: {', '.join(bits)}")
        drift += changed

        stale = bool(got.new or got.amended) or index_stale(archive)
        if stale and args.check and builder and not (got.new or got.amended):
            print(f"[NOTE] {name}: INDEX.md is behind the archive (--check: not rebuilt)")
        if stale and not args.check and builder:
            # A script the catalogue names, run with this interpreter: approved as it
            # stands, or not run at all.
            if trust.allows(trust.script(builder.tool, INDEX_BUILDER, builder.path)):
                problem = run_builder(builder.path, archive)
                if problem:
                    print(f"[FAIL] {name}: index builder {problem}; INDEX.md may be stale")
                    builder_fails += 1
            # The inbox gets the regenerated index too, so a session that reads INDEX.md
            # for earlier reports on the same problem sees the full cross-environment
            # corpus and not just what this machine wrote.
            idx = archive / "INDEX.md"
            if idx.exists() and not args.no_mirror:
                if archived_file(idx, archive):
                    inbox.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(idx, inbox / "INDEX.md")
                else:
                    print(
                        f"[FAIL] {name}: feedback/{name}/INDEX.md is a symbolic link or "
                        "resolves outside the archive - not mirrored"
                    )
                    refused += 1

    if builder is None:
        print(
            "[WARN] no tool entry resolves a [feedback].index_builder; indexes were not regenerated"
        )

    total = sum(len(reports(archive)) for archive in archives)
    print(
        f"\n{len(targets)} target(s) - archive holds {total} report(s)"
        f" - {drift} file(s) {'out of sync' if args.check else 'synced'}"
        f" - {conflicts} conflict(s)" + (f" - {refused} refused" if refused else "")
    )
    trust.closing()
    if conflicts or refused or builder_fails or trust.skipped:
        return 1
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
