"""Reconcile declared rings against observed reality. Proposes, never edits.

WHY THIS EXISTS
---------------
The radar's premise is that adoption is MEASURED, never taken on faith. A ring assigned
from other people's download counts, or from an intention, claims adoption nobody
observed - and a ring that records an intention rather than a state re-inflates soon
after every cleanup, because nothing compares the label to the world.

This command is that comparison. For each entry it gathers evidence the machine can
actually produce:

  installed     the [install].check probe passes (reuses `radar apply`'s prober, so an
                unrunnable probe is UNKNOWN and never silently "absent"; like every
                catalogue command it runs only once approved - see trust.py), or, when
                the probe gave no answer, an installer's own record lists the entry. An
                approved probe that says absent while a record says installed is a NOTE
                naming the disagreement, and the entry is not judged
  projects      the package appears in a real dependency table of some project under
                the projects root - parsed with tomllib, never substring-matched
  precommit     the name appears as a pre-commit hook id or hook repo
  mcp_mounted   the server appears in Claude Code's mcpServers config

and then proposes a ring the evidence supports. Rules, deliberately blunt:

  adopt  needs presence: installed, or a real dependency, or a mounted server.
  pilot  needs the same. A pilot is an EXPERIMENT; one that was never installed is
         not an experiment, it is a bookmark - and a pilot ring full of bookmarks is
         how the ring stops meaning anything.
  observe is the honest home for "interesting, not running". It costs nothing and
         claims nothing, which is exactly why it must be the default.

It also looks the other way: at what is installed that the catalogue does not account
for. uv tools, Claude Code plugins and user skills are listed from the installers' own
records (no catalogue command runs for it), and each is reported when it matches no
entry at all, or when the entry it matches sits at observe or discard - installed while
the catalogue says it is not in use.

Nothing here edits a file. Demotion is a judgement with a history entry attached, and
the operator writes it - same discipline as `radar apply`, which proposes removals and
never performs them.

Evidence absence is not the same as evidence of absence: entries whose probe is
UNKNOWN or not approved, whose probe and installer record disagree, whose install kind
is `none` (a standard has nothing to install), or that the profile excludes or does not
want are reported as NOTE and never proposed for demotion.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from .apply import ABSENT, NOT_APPROVED, PRESENT, UNKNOWN, approved_probe, expand
from .radar_lib import (
    begin_command,
    default_environment,
    environment_name,
    load_profile,
    load_tools,
    reports_profile_errors,
    tools_for_environment,
)
from .paths import MARKER
from .trust import FLAG, Trust
from .versions import identities, installed_plugins, pep503, uv_tools

# Where the user's actual projects live: the directory the data root sits in. The control
# plane READS the data plane - that is the permitted direction; the reverse
# would be the breach. Derived in main() rather than here, because a module-level default
# would resolve the root at import time and a command would need a marker merely to print
# its own `--help`.

# Rings whose claim requires evidence. `own` is authored (its presence is the
# working tree itself, which `radar apply` already probes); observe/discard claim nothing.
EVIDENCE_RINGS = ("adopt", "pilot")

# Rings that say "not in use here". Something installed that matches one of them is the
# reverse gap: the label claims less than the machine shows.
IDLE_RINGS = ("observe", "discard")

DEP_TABLE_KEYS = (
    ("project", "dependencies"),
    ("project", "optional-dependencies"),
    ("dependency-groups",),
    ("tool", "uv", "dev-dependencies"),
    ("tool", "poetry", "dependencies"),
    ("tool", "poetry", "group"),
)

# A requirement string ("widget[extra]>=0.20,<1 ; python_version>'3.10'") reduced to
# its distribution name. Extras and versions are noise for identity; the MARKER is not,
# so has_marker() keeps it.
REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def dist_name(req: str) -> str:
    m = REQ_NAME.match(req or "")
    return (m.group(1) if m else "").lower().replace("_", "-")


def norm(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def has_marker(req: str) -> bool:
    """Whether the requirement is conditional on an environment marker.

    Evaluating markers properly needs `packaging`, which is not stdlib - so instead of
    guessing, a conditional dependency is counted SEPARATELY and never allowed to be the
    sole evidence of adoption. `pkg; python_version < '0'` is the case that makes it
    matter: a marker that can never be true, i.e. a deliberately disabled dependency,
    which a plain count would report as a real consumer.
    """
    return ";" in (req or "")


def walk_dep_strings(node, out: list[str]) -> None:
    """Collect requirement strings from arbitrarily nested dependency tables.

    Dependency layout varies (PEP 621 lists, dependency-groups maps, poetry's
    name->constraint maps, group.<name>.dependencies). Rather than special-case each
    shape, walk it: strings are requirements, dict KEYS are also names under poetry's
    mapping style, and anything else recurses.
    """
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for v in node:
            walk_dep_strings(v, out)
    elif isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k))
            walk_dep_strings(v, out)


def dig(d: dict, path: tuple[str, ...]):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def scan_projects(
    root: Path,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """(deps -> consumers, conditional deps -> projects, config tokens -> projects).

    Parsed, not grepped. A substring search would report `ty` as used by every project
    that depends on `typing-extensions` or configures `mypy` - the same name-matching
    error the gate already forbids for registries.

    THREE WAYS A NAIVE COUNT OVERCOUNTS, each handled below:

    1. **Self-reference.** Declaring your own extras is the standard PEP 621 idiom
       (`pkg[extra]` inside pkg's own pyproject). A project is not its own adopter, so
       a project's requirements on its own name are skipped.
    2. **Sibling worktrees.** Several checkouts of one codebase share one
       `project.name`. Counting directories would count the same consumer once per
       checkout, so projects are keyed by the declared `project.name`.
    3. **Disabled dependencies.** `pkg; python_version < '0'` carries a marker that can
       never be true. Conditional requirements are tracked separately and can never be
       the sole evidence of adoption.

    A DIRECTORY CARRYING `radar.toml` IS A CATALOGUE, NOT A CONSUMER. `radar init` writes
    a pyproject.toml with the catalogue's own lint tools in it, and the default projects
    root is the directory that holds the data root, so without this every catalogue would
    count itself as a project that depends on ruff and pre-commit and configures them -
    evidence that no real project supplied. Its worktrees carry the marker too.

    A command whose purpose is to replace faith with counting has no business reporting
    a count it cannot defend.
    """
    deps: dict[str, set[str]] = {}
    conditional: dict[str, set[str]] = {}
    tokens: dict[str, set[str]] = {}
    # directory -> declared project name, so the pre-commit pass below counts the same
    # identities as the dependency pass. Without it several worktrees of one codebase
    # would be deduplicated in one half of the evidence and counted once each in the other.
    dir_to_project: dict[str, str] = {}
    for pj in sorted(root.glob("*/pyproject.toml")):
        if (pj.parent / MARKER).is_file():
            continue
        try:
            with pj.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        # Identity is the declared distribution name, not the folder. Falling back to the
        # folder keeps non-PEP-621 trees countable.
        own = norm((data.get("project") or {}).get("name") or "") or norm(pj.parent.name)
        dir_to_project[pj.parent.name] = own
        raw: list[str] = []
        for path in DEP_TABLE_KEYS:
            walk_dep_strings(dig(data, path), raw)
        for r in raw:
            n = dist_name(r)
            if not n or n == own:
                continue
            (conditional if has_marker(r) else deps).setdefault(n, set()).add(own)
        # A `[tool.<x>]` section is configuration, which is adoption evidence of a
        # different kind: the tool may be invoked by pre-commit or CI rather than
        # being a project dependency.
        for name in data.get("tool") or {}:
            tokens.setdefault(str(name).lower(), set()).add(own)

    for pc in sorted(root.glob("*/.pre-commit-config.yaml")):
        if (pc.parent / MARKER).is_file():
            continue
        proj = dir_to_project.get(pc.parent.name, norm(pc.parent.name))
        try:
            text = pc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # No yaml in the stdlib. Hook ids and repo slugs are line-shaped enough that
        # two narrow regexes beat a dependency, and a miss here only ever WEAKENS a
        # demotion proposal (it never invents evidence).
        for m in re.finditer(r"^\s*-?\s*id:\s*([A-Za-z0-9._-]+)", text, re.M):
            tokens.setdefault(m.group(1).lower(), set()).add(proj)
        for m in re.finditer(r"^\s*-?\s*repo:\s*\S*?/([A-Za-z0-9._-]+?)(?:\.git)?\s*$", text, re.M):
            tokens.setdefault(m.group(1).lower(), set()).add(proj)
    return deps, conditional, tokens


def mounted_mcp_servers(paths: dict[str, str]) -> tuple[set[str], str]:
    """Server names Claude Code has mounted, from its own config.

    Plugin-provided and claude.ai-connector servers do not appear here; they are not
    `claude mcp add` entries. That is why an empty set is reported as such rather
    than as proof of nothing running.

    `[paths].claude_config` rather than `.claude.json` in the running account's home: the file
    sits directly in the home directory, a sibling of `claude_home` and not under it, so
    deriving it from the machine the command happens to run on - rather than from the
    declared profile - would silently read the wrong machine's config on any environment
    other than the operator's own.
    """
    declared = paths.get("claude_config")
    if not declared:
        return set(), "no [paths].claude_config declared"
    cfg = Path(declared)
    if not cfg.exists():
        return set(), f"no {cfg}"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return set(), f"unreadable: {exc}"
    names = set((data.get("mcpServers") or {}).keys())
    for proj in (data.get("projects") or {}).values():
        if isinstance(proj, dict):
            names |= set((proj.get("mcpServers") or {}).keys())
    return {n.lower() for n in names}, "ok"


def installed_elsewhere(
    paths: dict[str, str], tools: list[dict]
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """(installed with no catalogue entry, installed while the entry is idle, notes,
    entry name -> the installer record that lists it).

    Read from the installers' own records - uv's tools, Claude Code's plugin record, the
    user skills directory - so no catalogue command runs. An installed thing matches an
    entry by any of the entry's identities (name, registry package, aliases), compared in
    normalized spelling. Every entry counts, whatever its ring and whichever environment
    it is scoped to: something the catalogue has an opinion about is not unlisted.
    """
    owner: dict[str, dict] = {}
    for t in tools:
        for i in identities(t):
            owner.setdefault(pep503(i), t)
    found: list[tuple[str, str, str]] = []
    notes: list[str] = []

    uv, uv_from = uv_tools()
    if uv is None:
        notes.append(f"uv tools: not read ({uv_from})")
    for package, version in sorted((uv or {}).items()):
        found.append(("uv tool", package, version or "?"))

    plugins, detail = installed_plugins(paths.get("claude_home"))
    if plugins is None:
        notes.append(f"plugins: not read ({detail})")
    for full, versions in sorted((plugins or {}).items()):
        found.append(("plugin", full, ", ".join(versions)))

    claude_home = paths.get("claude_home")
    if not claude_home:
        notes.append("skills: not read (no [paths].claude_home declared)")
    else:
        skills = Path(claude_home) / "skills"
        if skills.is_dir():
            for d in sorted(p for p in skills.iterdir() if p.is_dir()):
                found.append(("skill", d.name, ""))

    unlisted: list[str] = []
    idle: list[str] = []
    recorded: dict[str, str] = {}
    for kind, name, detail in found:
        t = owner.get(pep503(name.split("@", 1)[0] if kind == "plugin" else name))
        label = f"{kind:8} {name}" + (f" {detail}" if detail else "")
        if t is None:
            unlisted.append(label)
            continue
        recorded.setdefault(t["name"], kind)
        if t.get("ring") in IDLE_RINGS:
            idle.append(f"{label}  (entry {t['name']}: ring={t['ring']})")
    return unlisted, idle, notes, recorded


def _entries(n: int) -> str:
    return f"{n} entr{'y' if n == 1 else 'ies'}"


def _unjudged_clause(unjudged: int, not_judged: int) -> str:
    """What the summary says about the entries it did not back or demote."""
    parts = []
    if unjudged:
        parts.append(f"{_entries(unjudged)} could not be judged")
    if not_judged:
        parts.append(f"{_entries(not_judged)} not judged here")
    return " and ".join(parts) + " (see the NOTEs above)"


@reports_profile_errors
def main() -> None:
    ap = argparse.ArgumentParser(prog="radar reconcile", description=__doc__.split("\n")[0])
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
    ap.add_argument(
        "--projects-root",
        help="where the user's projects live (default: the directory holding the data root)",
    )
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument(
        "--no-probe",
        action="store_true",
        help="skip install probes (offline / fast); presence then rests on deps + mcp only",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any entry claims a ring the evidence does not support",
    )
    ap.add_argument(
        FLAG,
        dest="trust_commands",
        action="store_true",
        help="approve, record and run the pending check probes an earlier "
        "`radar reconcile` run listed with the same id; one no such run listed, or changed "
        "since, is listed and not run",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)
    args.env = args.env or default_environment(root)
    trust = Trust(root, approve=args.trust_commands, verb="reconcile")

    env, errs = load_profile(args.env)
    if env is None:
        print(f"no such environment: {args.env}", file=sys.stderr)
        raise SystemExit(2)
    for e in errs:
        print(f"[profile] {e}")

    proot = Path(args.projects_root) if args.projects_root else root.parent
    deps, conditional, tokens = scan_projects(proot)
    paths = {k: str(v) for k, v in (env.get("paths") or {}).items()}
    servers, mcp_state = mounted_mcp_servers(paths)

    print(f"stack-radar reconcile - environment {args.env!r}")
    print(f"  projects root: {proot}  ({len(deps)} distinct deps seen)")
    print(f"  mounted mcp servers: {sorted(servers) or 'none'} ({mcp_state})")
    print()

    all_tools = load_tools()
    wanted = {t["name"] for t in tools_for_environment(env, all_tools)}
    excluded = set(env.get("exclude") or [])
    rows, proposals, notes = [], [], []
    # Entries the evidence could not judge: the probe was skipped, not approved or
    # answered UNKNOWN, or it said absent while an installer's record says installed.
    # They are neither backed nor demoted, and the summary says so.
    unjudged = 0
    # Entries not judged here at all: excluded by the profile, not wanted in this
    # environment, or with nothing to install. The summary counts them too, so it never
    # says every entry is backed when some were never looked at.
    not_judged = 0
    # The installers' own records - uv's tools, installed_plugins.json, the skills
    # directory - read without running anything. An entry they list is installed, which
    # is evidence wherever its check probe gave no answer.
    unlisted, idle, inventory_notes, recorded = installed_elsewhere(paths, all_tools)

    for t in sorted(all_tools, key=lambda x: x["name"]):
        ring, name = t.get("ring"), t["name"]
        if ring not in EVIDENCE_RINGS:
            continue
        ids = identities(t)
        inst = t.get("install") or {}
        kind = inst.get("kind")

        dep_hits = sorted({p for i in ids for p in deps.get(i, set())})
        cond_hits = sorted({p for i in ids for p in conditional.get(i, set())})
        tok_hits = sorted({p for i in ids for p in tokens.get(i, set())})
        mcp_hit = bool(ids & servers)

        # An entry this environment excludes or does not want is reported and not judged
        # below, so its probe is not run either: its answer would be discarded, and
        # asking the operator to approve a command whose result is thrown away is noise.
        judged = name not in excluded and name in wanted

        state, detail = UNKNOWN, ""
        if not judged:
            state, detail = UNKNOWN, "not judged here"
        elif args.no_probe or not inst.get("check"):
            state, detail = (
                UNKNOWN,
                "no check probe" if not inst.get("check") else "skipped",
            )
        else:
            try:
                cmd = expand(str(inst["check"]), paths)
            except KeyError as exc:
                cmd = None
                state, detail = UNKNOWN, f"check references unknown [paths] key {exc.args[0]}"
            if cmd is not None:
                state, detail = approved_probe(trust, name, cmd, args.timeout)

        # The installer's record is evidence only where the probe gave no answer (none
        # declared, skipped, not approved, UNKNOWN). An approved probe that says absent
        # while a record says installed is a disagreement about the very thing judged
        # here, so it is named and the entry is left alone rather than either side winning.
        evidence = []
        disputed = False
        if state == PRESENT:
            evidence.append("installed")
        elif name in recorded and state == ABSENT:
            disputed = True
            notes.append(
                f"{name}: the approved check probe says absent, while the {recorded[name]} "
                "record lists it installed - the record is not counted; check which is current"
            )
        elif name in recorded:
            evidence.append(f"installed ({recorded[name]} record)")
        if dep_hits:
            evidence.append(f"dep in {len(dep_hits)} project(s)")
        if tok_hits:
            evidence.append(f"configured in {len(tok_hits)}")
        # Reported, never counted as evidence: a conditional requirement may be disabled
        # (`; python_version < '0'`) and markers cannot be evaluated without `packaging`.
        if cond_hits and not dep_hits:
            notes.append(
                f"{name}: {len(cond_hits)} conditional-only reference(s) "
                f"({', '.join(cond_hits)}) - behind an environment marker, not counted"
            )
        if mcp_hit:
            evidence.append("mcp mounted")

        rows.append((name, ring, state, ", ".join(evidence) or "-"))

        if name in excluded:
            notes.append(f"{name}: excluded by profile ({ring}); not judged here")
            not_judged += 1
            continue
        if name not in wanted:
            notes.append(f"{name}: not wanted in {args.env}; not judged here")
            not_judged += 1
            continue
        if kind == "none":
            notes.append(f"{name}: install.kind=none (a standard/spec) - unprobeable by design")
            not_judged += 1
            continue
        if evidence:
            continue
        if disputed:
            unjudged += 1
            continue
        # Absence of evidence is not evidence of absence. An unrun or unrunnable probe
        # leaves presence genuinely unknown, so the entry is reported and left alone -
        # including under --no-probe, where EVERY probe is unrun and demoting on that
        # would manufacture false positives out of a flag meant to go faster, and for a
        # probe not yet approved, which ran no more than a skipped one did.
        if state == UNKNOWN and inst.get("check"):
            if args.no_probe:
                why = "probe skipped (--no-probe)"
            elif detail == NOT_APPROVED:
                why = "probe not approved"
            else:
                why = f"probe UNKNOWN ({detail})"
            notes.append(f"{name}: {why} - presence unknown, not demoted")
            unjudged += 1
            continue

        why = "no install probe" if not inst.get("check") else "absent"
        proposals.append((name, ring, f"{why}; 0 project deps; 0 configs"))

    w = max((len(r[0]) for r in rows), default=4)
    print(f"{'tool'.ljust(w)}  ring    probe     evidence")
    print(f"{'-' * w}  ------  --------  --------")
    for name, ring, state, ev in rows:
        print(f"{name.ljust(w)}  {ring:6}  {state:8}  {ev}")

    if notes:
        print()
        for n in notes:
            print(f"[NOTE] {n}")

    print()
    if proposals:
        print(
            f"{len(proposals)} entr{'y' if len(proposals) == 1 else 'ies'} claim a ring the evidence does not support:"
        )
        for name, ring, why in proposals:
            print(f"  demote  {name}: {ring} -> observe   ({why})")
        print()
        print("Each demotion is a judgement: edit tools/<name>.toml (ring + a dated")
        print("[[history]] entry naming this evidence), then re-run `radar gate`. This")
        print("command never edits - a ring change is a decision, and decisions carry authorship.")
    elif unjudged or not_judged:
        print(f"no demotion proposed; {_unjudged_clause(unjudged, not_judged)}.")
    else:
        print("every adopt/pilot entry is backed by observed evidence.")
    if proposals and (unjudged or not_judged):
        print(f"Besides these, {_unjudged_clause(unjudged, not_judged)}.")

    # The reverse direction: installed, and not accounted for by the catalogue. Reported,
    # never judged - an unlisted tool may be a deliberate local choice.
    print()
    if unlisted:
        print(f"installed, with no catalogue entry ({len(unlisted)}):")
        for line in unlisted:
            print(f"  {line}")
    else:
        print("installed, with no catalogue entry: none")
    if idle:
        print(f"installed, while the catalogue says observe or discard ({len(idle)}):")
        for line in idle:
            print(f"  {line}")
    for n in inventory_notes:
        print(f"[NOTE] {n}")

    trust.closing()
    raise SystemExit(1 if ((args.strict and proposals) or trust.skipped) else 0)


if __name__ == "__main__":
    main()
