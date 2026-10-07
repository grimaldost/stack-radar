"""Converge an environment onto its profile: plan by default, execute only on request.

This is the radar's hand on the stack. It observes the machine with each tool's own
`[install].check` probe, diffs that against what `environments/<name>.toml` wants, and
prints a plan. Installs it proposes and (with --apply --yes) executes; removals it only
ever proposes, because the radar does not know what else on this machine depends on a
package, and a wrong uninstall costs more than a missing tool.

The asymmetry is the point of the control-plane invariant: what apply writes into the
data plane is a plain `uv tool` / `npm -g` / `claude mcp` artefact that keeps working
with the radar deleted, and nothing it writes names the radar. apply itself creates no
files in the catalogue or the data plane. The one thing it records is an approval, in
the catalogue's git directory, when `--trust-commands` asks it to (see trust.py).

A probe and an install are commands written in the catalogue, so each runs only once the
operator has approved that exact command. `--check` is therefore not free of side
effects: it runs each entry's APPROVED check probe, and writes nothing else.

    radar apply --env <name> --check        # plan: runs approved check probes only
    radar apply --env <name> --apply        # show what it would run, stop
    radar apply --env <name> --apply --yes  # run the approved installs
    ... --trust-commands                    # approve, record and run what a run listed

Exit 0 when the environment is converged, or when every install an `--apply --yes` run
executed exited 0 and nothing else needs action; 1 when anything needs action, an install
failed, or a pending command was not run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .radar_lib import (
    RINGS,
    begin_command,
    committed_environments,
    environment_name,
    load_profile,
    load_tools,
    reports_profile_errors,
    tools_for_environment,
    validate_environment,
)
from .trust import (
    APPLY,
    CHECK,
    FLAG,
    Trust,
    batch_refusal,
    catalogue_argv,
    command_env,
    executable,
    scratch_dir,
)

CHECK_TIMEOUT = 60
APPLY_TIMEOUT = 900

PRESENT = "yes"
ABSENT = "no"
UNKNOWN = "unknown"
UNPROBED = "-"

# The detail an observation carries when its probe was pending approval and did not run.
# Presence is then UNKNOWN - never present and never absent - because nothing was looked at.
NOT_APPROVED = "check not approved"

MCP_KINDS = ("mcp-server", "mcp-plugin")
# Kinds `radar apply` may execute. Everything else is verify-only or printed as a suggestion.
EXECUTABLE_KINDS = (
    "uv-tool",
    "npm-tool",
    "pip",
    "claude-plugin",
    "mcp-server",
    "mcp-plugin",
)

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


# ----------------------------------------------------------------------- reporting


@dataclass
class Row:
    name: str
    ring: str
    wanted: str
    present: str
    action: str


class Report:
    """Findings as [FAIL]/[WARN]/[NOTE] lines. FAIL is the only class that fails the run
    on its own."""

    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0
        self.drift = 0

    def fail(self, subject: str, msg: str) -> None:
        print(f"[FAIL] {subject}: {msg}")
        self.fails += 1

    def warn(self, subject: str, msg: str) -> None:
        print(f"[WARN] {subject}: {msg}")
        self.warns += 1

    def note(self, subject: str, msg: str) -> None:
        print(f"[NOTE] {subject}: {msg}")


# --------------------------------------------------------------------- placeholders


def expand(template: str, paths: dict[str, str]) -> str:
    """Fill {key} from the profile's [paths]. Unknown keys are an error, not a guess."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in paths:
            raise KeyError(key)
        return str(paths[key])

    return PLACEHOLDER.sub(sub, template)


# ----------------------------------------------------------------------- observation


def run_command(cmd: str, timeout: int, root: Path | None = None) -> tuple[str, int | None, str]:
    """Run one catalogue command, no shell. Returns (outcome, returncode, detail).

    outcome: "ran" (returncode is meaningful), "missing" (not on PATH), "error".
    No shell means no pipes and no expansion, which keeps a probe the same command on
    every machine. The command is split, its data-root paths made absolute and its
    program found by trust.py, the same code that decides what an approval covers, so
    what runs is what was approved: argv[0] is looked up on PATH with PATHEXT applied and
    never in a working directory. It runs in an empty scratch directory with
    PYTHONSAFEPATH set, never in the data root (`root`), so nothing in the catalogue that
    the approval did not hash can change what it does. The caller gates the call on
    approval; this function only runs. The one shell it cannot avoid is cmd.exe, which
    Windows runs for a `.bat` or `.cmd` program: such a command runs only when
    `trust.batch_refusal` finds nothing in its path or arguments that cmd.exe would read as
    syntax.
    """
    base = root or Path.cwd()
    try:
        argv = catalogue_argv(cmd, base)
    except ValueError as exc:
        return "error", None, f"unparseable command: {exc}"
    if not argv:
        return "error", None, "empty command"
    exe = executable(argv[0], base)
    if exe is None:
        return "missing", None, f"{argv[0]} not on PATH"
    # The gate refuses such a command before it gets here; this keeps the runner from
    # ever handing one to cmd.exe on its own.
    refused = batch_refusal(exe, argv[1:])
    if refused:
        return "error", None, refused
    try:
        with scratch_dir() as cwd:
            proc = subprocess.run(  # noqa: S603 - argv from an approved command, no shell
                [exe, *argv[1:]],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=command_env(),
            )
    except subprocess.TimeoutExpired:
        return "error", None, f"timed out after {timeout}s"
    except OSError as exc:
        return "error", None, f"could not run: {exc}"
    first = ""
    for stream in (proc.stdout, proc.stderr):
        line = (stream or "").strip().splitlines()
        if line:
            first = line[0].strip()[:110]
            break
    return "ran", proc.returncode, first


def probe(cmd: str, timeout: int, root: Path | None = None) -> tuple[str, str]:
    """Observe presence. A probe that cannot run is UNKNOWN, never silently absent."""
    outcome, rc, detail = run_command(cmd, timeout, root)
    if outcome == "missing":
        return ABSENT, detail
    if outcome == "error":
        return UNKNOWN, detail
    return (PRESENT if rc == 0 else ABSENT), (detail or f"exit {rc}")


def approved_probe(
    trust: Trust, name: str, cmd: str, timeout: int, field: str = CHECK
) -> tuple[str, str]:
    """`probe`, behind the approval gate. A command that may not run observes nothing:
    UNKNOWN, with NOT_APPROVED as the detail, and the gate has already said why."""
    if not trust.allows(trust.command(name, field, cmd)):
        return UNKNOWN, NOT_APPROVED
    return probe(cmd, timeout, trust.root)


def policy_flags(env: dict) -> tuple[bool, bool]:
    """(allow_network_install, allow_external_mcp), each False unless the profile sets it.

    A profile with no [flags] table, or with one key missing, installs nothing over the
    network and registers no external MCP server - the safe reading, and the documented
    one. The starter profile writes both keys out as false; a hand-written profile that
    leaves them out gets the same answer. Only a TOML `true` counts, so a string such as
    "false" does not switch anything on.
    """
    flags = env.get("flags") or {}
    return (
        flags.get("allow_network_install") is True,
        flags.get("allow_external_mcp") is True,
    )


# ------------------------------------------------------------------------- planning


@dataclass
class Job:
    name: str
    command: str


# Paths the radar CREATES rather than paths the machine must already have. Counting
# these as drift would make --check unsatisfiable: it could not go green until the
# renderer had run, and the renderer's output is not this command's business. Their
# PARENT is what has to exist; freshness of the artefact belongs to
# `radar render-feedback-targets --check`.
OUTPUT_PATHS = {"feedback_targets", "feedback_root"}


def check_paths(env: dict, rep: Report) -> dict[str, str]:
    paths = {k: str(v) for k, v in (env.get("paths") or {}).items()}
    for key, value in sorted(paths.items()):
        if os.path.exists(value):
            continue
        if key in OUTPUT_PATHS:
            parent = os.path.dirname(value.rstrip("/\\")) or value
            if not os.path.isdir(parent):
                rep.warn("paths", f"{key}: parent {parent} does not exist")
                rep.drift += 1
            continue
        rep.warn("paths", f"{key} declared as {value} does not exist")
        rep.drift += 1
    return paths


def check_env_vars(env: dict, rep: Report) -> None:
    names = list(env.get("require_env") or [])
    if not names:
        return
    missing = [n for n in names if not os.environ.get(str(n))]
    for n in missing:
        # Presence only. The value is never read, printed, or logged.
        rep.fail("require_env", f"{n} is not set in this environment")
    rep.note("require_env", f"{len(names) - len(missing)}/{len(names)} variables present")


def plan(
    env: dict,
    paths: dict[str, str],
    rep: Report,
    timeout: int,
    verbose: bool,
    trust: Trust,
) -> tuple[list[Row], list[Job], list[Job]]:
    """Diff the machine against the profile. Returns (rows, installs, manual steps)."""
    tools = load_tools()
    wanted = tools_for_environment(env, tools)
    wanted_names = {t["name"] for t in wanted}
    allow_network, allow_mcp = policy_flags(env)
    excluded = set(env.get("exclude") or [])

    rows: list[Row] = []
    installs: list[Job] = []
    manual: list[Job] = []
    unmanaged: list[str] = []

    def observe(name: str, inst: dict) -> tuple[str, str]:
        raw = inst.get("check")
        if not raw:
            return UNPROBED, "no check probe declared"
        try:
            cmd = expand(str(raw), paths)
        except KeyError as exc:
            rep.fail(name, f"check references unknown [paths] key: {exc.args[0]}")
            return UNKNOWN, "unresolved placeholder"
        status, detail = approved_probe(trust, name, cmd, timeout)
        if verbose and detail != NOT_APPROVED:
            print(f"       probe {name}: {cmd} -> {status} ({detail})")
        return status, detail

    for t in sorted(wanted, key=lambda t: (RINGS.index(t["ring"]), t["name"])):
        name = t["name"]
        inst = t.get("install")
        if inst is None:
            unmanaged.append(name)
            rows.append(Row(name, t["ring"], "yes", UNPROBED, "unmanaged"))
            continue
        kind = str(inst.get("kind"))

        if kind in MCP_KINDS and not allow_mcp:
            rep.note(name, f"skipped: allow_external_mcp=false (kind={kind})")
            rows.append(Row(name, t["ring"], "yes", UNPROBED, "skip: policy"))
            continue
        if kind == "none":
            rows.append(Row(name, t["ring"], "yes", UNPROBED, "nothing to install"))
            continue

        status, detail = observe(name, inst)
        if status == PRESENT:
            rows.append(Row(name, t["ring"], "yes", status, "ok"))
            continue
        if status == UNKNOWN and detail == NOT_APPROVED:
            # The [UNTRUSTED] line already said which command and why; a WARN on top
            # would say it twice. Counted as drift: nothing about this tool is known.
            rows.append(Row(name, t["ring"], "yes", status, "not approved"))
            rep.drift += 1
            continue
        if status == UNKNOWN:
            rep.warn(name, f"probe could not decide presence: {detail}")
            rows.append(Row(name, t["ring"], "yes", status, "investigate"))
            rep.drift += 1
            continue

        # Absent, or present-unknowable (manual entries with no honest machine probe).
        instruction = str(inst.get("instruction") or "").strip()
        try:
            instruction = expand(instruction, paths) if instruction else ""
        except KeyError as exc:
            rep.fail(name, f"instruction references unknown [paths] key: {exc.args[0]}")
            instruction = ""

        if kind == "repo":
            rep.warn(name, f"working tree missing - {instruction or 'restore it by hand'}")
            manual.append(Job(name, instruction or "restore the working tree"))
            rows.append(Row(name, t["ring"], "yes", status, "clone (manual)"))
            rep.drift += 1
            continue

        if kind == "manual" or kind not in EXECUTABLE_KINDS:
            if status == UNPROBED:
                # Nothing to converge machine-wide (a library, a per-project choice).
                rep.note(name, f"manual, not machine-scoped: {instruction}")
                rows.append(Row(name, t["ring"], "yes", status, "manual"))
            else:
                rep.note(name, f"absent, install by hand: {instruction}")
                manual.append(Job(name, instruction))
                rows.append(Row(name, t["ring"], "yes", status, "manual"))
                rep.drift += 1
            continue

        raw_apply = str(inst.get("apply") or "")
        try:
            cmd = expand(raw_apply, paths)
        except KeyError as exc:
            rep.fail(name, f"apply references unknown [paths] key: {exc.args[0]}")
            rows.append(Row(name, t["ring"], "yes", status, "broken entry"))
            continue
        if not allow_network:
            rep.note(name, f"allow_network_install=false - run by hand: {cmd}")
            manual.append(Job(name, cmd))
            rows.append(Row(name, t["ring"], "yes", status, "manual (no network)"))
            rep.drift += 1
            continue
        installs.append(Job(name, cmd))
        rows.append(Row(name, t["ring"], "yes", status, f"install ({kind})"))
        rep.drift += 1

    # One line, not one per tool: an entry with no [install] block is a gap in the
    # catalogue, not drift on this machine, and it must not drown the actionable findings.
    if unmanaged:
        rep.note(
            "unmanaged",
            f"{len(unmanaged)} wanted entries carry no [install] block: " + ", ".join(unmanaged),
        )

    # Removals are proposed for what this profile actively rejects: a discarded tool, or
    # one the profile excludes. A tool in an unconverged ring (observe) is neither wanted
    # nor unwanted, so it is left alone rather than nagged about.
    for t in sorted(tools, key=lambda t: t["name"]):
        name = t["name"]
        if name in wanted_names:
            continue
        rejected = t["ring"] == "discard" or name in excluded
        inst = t.get("install") or {}
        if not rejected or not inst.get("check"):
            continue
        status, detail = observe(name, inst)
        if status != PRESENT:
            continue
        why = "ring=discard" if t["ring"] == "discard" else "excluded by this profile"
        # `instruction` says how to INSTALL, so echoing it here would invert the advice.
        # Only an entry that spells out its own removal gets a command; otherwise say
        # plainly that the operator decides, because the radar cannot see what else on
        # this machine depends on the package.
        hint = str(inst.get("remove") or "").strip()
        rep.note(
            name,
            f"present but not wanted here ({why}) - "
            + (f"suggested removal: {hint}" if hint else "removal left to you")
            + " (apply never uninstalls)",
        )
        rows.append(Row(name, t["ring"], "no", status, "propose removal"))
        rep.drift += 1

    return rows, installs, manual


# --------------------------------------------------------------------------- output


def render_table(rows: list[Row]) -> None:
    header = ("tool", "ring", "wanted", "present", "action")
    data = [(r.name, r.ring, r.wanted, r.present, r.action) for r in rows]
    widths = [
        max(len(header[i]), *(len(d[i]) for d in data)) if data else len(header[i])
        for i in range(len(header))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print("\n" + fmt.format(*header))
    print("  ".join("-" * w for w in widths))
    for d in data:
        print(fmt.format(*d))


def execute(installs: list[Job], rep: Report, trust: Trust) -> tuple[int, int, int]:
    """Run each approved install. Returns (failed, not run for want of approval, refused
    because this machine will not run it at all - `Command.refusal`).

    An install that ran and exited 0 is no longer drift: the run did what the plan asked
    of it, so it leaves `rep.drift` and the exit code with it. Whether the tool is now
    present is the next `--check`'s to say.
    """
    failed = unapproved = refused = 0
    for job in installs:
        cmd = trust.command(job.name, APPLY, job.command)
        if not trust.allows(cmd):
            if cmd.refusal:
                refused += 1
            else:
                unapproved += 1
            continue
        print(f"\n$ {job.command}")
        outcome, rc, detail = run_command(job.command, APPLY_TIMEOUT, trust.root)
        if outcome != "ran":
            rep.fail(job.name, f"install could not run: {detail}")
            failed += 1
        elif rc != 0:
            rep.fail(job.name, f"install exited {rc}: {detail}")
            failed += 1
        else:
            print(f"       ok: {detail}" if detail else "       ok")
            rep.drift -= 1
    return failed, unapproved, refused


@reports_profile_errors
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="radar apply",
        description="Plan or apply convergence of an environment's tool stack.",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument("--env", required=True, type=environment_name, help="environment profile name")
    ap.add_argument(
        "--check",
        action="store_true",
        help="plan only (the default): runs each entry's approved check probe and writes "
        "nothing else",
    )
    ap.add_argument("--plan", action="store_true", help="alias for --check")
    ap.add_argument("--apply", action="store_true", help="execute the plan's installs")
    ap.add_argument("--yes", action="store_true", help="required for --apply to run")
    ap.add_argument(
        FLAG,
        dest="trust_commands",
        action="store_true",
        help="approve, record and run the pending catalogue commands an earlier "
        "`radar apply` run listed with the same id (check probes, and installs under "
        "--apply --yes); one no such run listed, or changed since its listing, is listed "
        "and not run",
    )
    ap.add_argument("--timeout", type=int, default=CHECK_TIMEOUT, help="probe timeout, s")
    ap.add_argument("--verbose", action="store_true", help="print every probe")
    args = ap.parse_args()
    root = begin_command(args.data_root)
    trust = Trust(root, approve=args.trust_commands, verb="apply")

    applying = args.apply and not (args.check or args.plan)
    # `--plan` is an alias for `--check`; print "plan" when that is the spelling the
    # operator used, so the header matches the command the docs tell them to run rather
    # than an internal name they never typed.
    mode = "apply" if applying else ("plan" if args.plan and not args.check else "check")

    env, sources = load_profile(args.env)
    if env is None:
        names = sorted(str(e.get("name")) for e in committed_environments() if e.get("name"))
        print(f"[FAIL] env: no profile named {args.env!r} (have: {', '.join(names)})")
        sys.exit(1)

    rep = Report()
    print(f"stack-radar apply - environment {args.env!r}, mode {mode}")
    for s in sources:
        print(f"  profile: {s}")
    # The EFFECTIVE values, from the same function the plan reads, so the header cannot
    # say one thing while the plan does another.
    allow_network, allow_mcp = policy_flags(env)
    print(
        "  rings: {} | allow_network_install={} | allow_external_mcp={}".format(
            ",".join(env.get("rings") or []), allow_network, allow_mcp
        )
    )
    if env.get("exclude"):
        print(f"  exclude: {', '.join(str(x) for x in env['exclude'])}")
    print()

    for err in validate_environment(env):
        rep.fail("profile", err)

    paths = check_paths(env, rep)
    check_env_vars(env, rep)
    rows, installs, manual = plan(env, paths, rep, args.timeout, args.verbose, trust)
    render_table(rows)

    present = sum(1 for r in rows if r.present == PRESENT and r.wanted == "yes")
    wanted = sum(1 for r in rows if r.wanted == "yes")
    # The trust gate's own FAIL (no git repository to keep approvals in) is printed by
    # trust.py, not through `rep`; it is counted here so the summary agrees with it.
    print(
        f"\n{wanted} wanted - {present} present - {rep.drift} needing action"
        f" - {rep.fails + trust.failures} FAIL, {rep.warns} WARN"
    )

    failed = 0
    if applying:
        if not installs and not manual:
            print("\nnothing to execute")
        if manual:
            print("\nmanual steps (never executed by apply):")
            for job in manual:
                print(f"  {job.name}: {job.command}")
        if installs and not args.yes:
            # A pending install is listed with its id, and the listing recorded: this is
            # what `--yes --trust-commands` may then approve, and nothing else. One this
            # machine will not run at all is named with the reason, and given no id.
            cmds = [(job, trust.command(job.name, APPLY, job.command)) for job in installs]
            runnable = [(job, cmd) for job, cmd in cmds if not cmd.refusal]
            blocked = [(job, cmd) for job, cmd in cmds if cmd.refusal]
            pending = {
                job.name: trust.listed(cmd) for job, cmd in runnable if not trust.approved(cmd)
            }
            if runnable:
                again = "--yes" + (f" {FLAG}" if pending else "")
                print(f"\nwould execute (re-run with {again}):")
            for job, cmd in runnable:
                if job.name in pending:
                    print(
                        f"  {job.name}: {job.command}  (not yet approved, id {pending[job.name]})"
                    )
                    trust.explain(cmd)
                else:
                    print(f"  {job.name}: {job.command}")
            if blocked:
                print("\nrefused, and not run with --yes either:")
            for job, cmd in blocked:
                print(f"  {job.name}: {job.command}")
                print(f"    {cmd.refusal}")
        elif installs:
            print(f"\nexecuting {len(installs)} install(s)")
            failed, unapproved, refused = execute(installs, rep, trust)
            print(
                f"\n{len(installs) - failed - unapproved - refused}/{len(installs)} "
                "install(s) succeeded"
                + (f", {unapproved} not run (not approved)" if unapproved else "")
                + (f", {refused} refused" if refused else "")
                + ". Re-run --check to confirm convergence"
                " (a fresh shell may be needed for new PATH entries)."
            )
    elif manual or installs:
        print("\nproposed:")
        for job in installs:
            print(f"  install  {job.name}: {job.command}")
        for job in manual:
            print(f"  by hand  {job.name}: {job.command}")

    trust.closing()
    sys.exit(1 if (rep.fails or rep.drift or failed or trust.skipped) else 0)


if __name__ == "__main__":
    main()
