"""`radar discovery` - what the sessions in a window were shown, and what they reached for.

WHY THIS VIEW EXISTS. Every other instrument in the engine measures INVOCATION: `radar
field` counts the calls that reached one entry, `radar economics` the tokens a session
spent. None measures EXPOSURE - what a session was shown before it chose: the skill
listing and how much of it rendered as a bare name, the deferred tools announced to it,
the ToolSearch calls it made, the router's suggestions, the dispatch line, the skills it
loaded. A tool nobody reaches for and a tool nobody was shown read the same in an
invocation count; this verb is what tells them apart.

ITS ONE SOURCE IS A DISCOVERY TABLE, specified in docs/concepts.md ("discovery table
format, version 1"): one row per session and UTC date, and the `derive.json` beside it, at
the path the profile's `[paths].discovery_index` names. Unlike the invocation table
`radar field --reconcile` reads, this one is not a cross-check of a parse the engine also
makes: it is the only parse. So its format is checked before a row is read, and a row
without the format's shape is skipped and counted, never read as zeros.

DEFINITIONS.

- A session is ACTIVE in the window when its rows there sum to at least one user prompt
  and at least one assistant message. Every figure counts active sessions only, so X never
  exceeds Y; only the first figure, the active sessions themselves, is over every session
  with a row in the window.
- A session's value is the sum of its rows in the window, or their maximum for
  `listing_chars_max`; its name list is the union of its rows' lists.
- "Sessions with X" counts distinct session ids, and a median is over sessions, those with
  a null value left out.
- A "most often" list is the top 10 by count, ties broken by name. A name-list field counts
  the distinct sessions that hold a name, a map field the sum of its counts.

COVERAGE. The table's records end at `derive.json`'s `records_end`: the days of the window
after its UTC date are unmeasured, that date itself is partial, and the days before the
table's first row are unmeasured too. An unmeasured day is printed as unmeasured, never
as a day of zeros.

WRITES NOTHING, and names no project, slug or path. The names it prints are skill, tool
and MCP server names, and each one goes through the redaction `radar field` applies to
what it writes - the home folders, then the scoped terms of the `tools.local/` entries -
while a name a machine-local entry's matcher claims prints as that entry's `redact_as`.
The redaction is applied before anything is counted, so the Markdown and `--json`, which
is the document the Markdown is rendered from, name exactly the same things.

THE HONEST ZERO. An absent profile key, an absent table, an absent `derive.json`, an
absent or unknown `discovery_format`, an absent `records_end` and a window with no rows
each print one `[WARN]` line naming which, and exit 1: a report of zeros there would read
exactly like a measurement of sessions that were shown nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import Counter
from pathlib import Path, PurePath
from typing import Any, NoReturn

from .bootstrap import home_dir
from .field_report import derive_age_hours, matches, utc_moment
from .radar_lib import (
    begin_command,
    default_environment,
    environment_name,
    load_environment,
    load_tools,
    reports_profile_errors,
)
from .redact import extra_homes, redact_homes, redact_text, scoped_terms
from .render import md_code

SCHEMA_VERSION = 1
FORMAT = "discovery-table/1"
PROFILE_KEY = "discovery_index"
TOP = 10

DESCRIPTIVE_BANNER = (
    "DESCRIPTIVE ONLY - what the sessions in the window were shown and what they reached "
    "for, counted from a discovery table. Not whether any of it helped."
)

# The fields this report reads, by the JSON type the format gives them. A row that lacks
# one, or carries it as another type, is not read: the format says what every row holds,
# so a row without that shape was not written to it, and reading its missing counts as
# zeros is the quiet failure this verb exists not to have. The other fields of a row are
# not this report's, and are not judged.
COUNTS = (
    "user_prompts",
    "assistant_messages",
    "listing_count",
    "deferred_delta_count",
    "toolsearch_calls",
    "toolsearch_calls_side",
    "toolsearch_select",
    "toolsearch_select_side",
    "toolsearch_keyword",
    "toolsearch_keyword_side",
    "toolsearch_select_unmatched",
    "router_injections",
    "router_unparsed",
    "router_unmatched",
    "router_named_loaded_after",
    "dispatch_lines",
    "skills_loaded_unmatched",
    "compact_boundaries",
)
NAME_LISTS = ("listing_name_only", "deferred_added_names")
MAPS = ("router_named", "toolsearch_selected", "toolsearch_selected_side", "skills_loaded")
# An integer, or null on a row with no listing.
MAXIMA = ("listing_chars_max",)


# ----------------------------------------------------------------------------- reading


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _count(value: object) -> bool:
    # bool is an int to Python and never a count.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_row(raw: object) -> dict[str, Any] | None:
    """The fields of one row this report reads, or None when the row lacks the format's
    shape for any of them."""
    if not isinstance(raw, dict):
        return None
    sid, day = raw.get("session_id"), raw.get("date")
    if not (isinstance(sid, str) and sid and isinstance(day, str)):
        return None
    try:
        date = dt.date.fromisoformat(day)
    except ValueError:
        return None
    row: dict[str, Any] = {"session_id": sid, "date": date}
    for key in COUNTS:
        if not _count(raw.get(key)):
            return None
        row[key] = raw[key]
    for key in MAXIMA:
        if key not in raw or not (raw[key] is None or _count(raw[key])):
            return None
        row[key] = raw[key]
    for key in NAME_LISTS:
        value = raw.get(key)
        if not (isinstance(value, list) and all(isinstance(n, str) for n in value)):
            return None
        row[key] = value
    for key in MAPS:
        value = raw.get(key)
        if not (
            isinstance(value, dict)
            and all(isinstance(k, str) and _count(v) for k, v in value.items())
        ):
            return None
        row[key] = value
    return row


def read_table(path: Path) -> tuple[list[dict[str, Any]], int]:
    """(the rows in the format's shape, how many lines were not). A blank line is no row."""
    rows: list[dict[str, Any]] = []
    unreadable = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            row = read_row(raw)
            if row is None:
                unreadable += 1
                continue
            rows.append(row)
    return rows, unreadable


# ------------------------------------------------------------------------------- names


def _globs(value: object) -> list[str]:
    return [g for g in value if isinstance(g, str) and g] if isinstance(value, list) else []


def _telemetry(tool: dict) -> dict:
    tel = tool.get("telemetry")
    return tel if isinstance(tel, dict) else {}


def _server(name: str) -> str:
    """The MCP server of a tool name: `mcp__<server>__<tool>` and `mcp__<server>` both give
    `<server>`. A name that is not an MCP tool's is its own server, so a server name the
    table records bare is compared the same way."""
    return name[5:].split("__", 1)[0] if name.startswith("mcp__") else name


class Names:
    """How each name is printed.

    First the claim: a name that a machine-local entry's `[telemetry]` matcher matches -
    `match_skill` for a skill id, `match` for a tool name or its MCP server - prints as
    that entry's `redact_as`, whole, because the rest of such a name can say as much as
    the part redaction would replace. Every other name goes through the redaction
    `radar field` applies to what it writes: the home folders, then the scoped terms.
    """

    def __init__(self, tools: list[dict], homes: list[PurePath]) -> None:
        self.terms = scoped_terms(tools)
        self.homes = homes
        self.claims: list[tuple[str, list[str], list[str]]] = []
        for t in tools:
            if not t.get("_local"):
                continue
            # `scoped_terms` already knows the entry's placeholder, including the generic
            # one that stands in for a missing or invalid `redact_as`.
            pairs = scoped_terms([t])
            if not pairs:
                continue
            tel = _telemetry(t)
            self.claims.append(
                (pairs[0][1], _globs(tel.get("match_skill")), _globs(tel.get("match")))
            )

    def _redacted(self, text: str) -> str:
        return redact_text(redact_homes(text, self.homes), self.terms)

    def skill(self, name: str) -> str:
        for placeholder, skill_globs, _ in self.claims:
            if matches(name, skill_globs):
                return placeholder
        return self._redacted(name)

    def prefix(self, prefix: str) -> str:
        """A plugin prefix: claimed when a `match_skill` glob matches every skill under it,
        which is the glob matching the literal `<prefix>:*`."""
        for placeholder, skill_globs, _ in self.claims:
            if matches(f"{prefix}:*", skill_globs):
                return placeholder
        return self._redacted(prefix)

    def tool(self, name: str) -> str:
        server = _server(name)
        for placeholder, _, tool_globs in self.claims:
            for glob in tool_globs:
                if matches(name, [glob]) or (
                    glob.startswith("mcp__") and matches(server, [_server(glob)])
                ):
                    return placeholder
        return self._redacted(name)


def own_prefixes(tools: list[dict]) -> set[str]:
    """The plugin prefixes an `own` entry claims: those whose glob `<prefix>:*` its
    `[telemetry].match_skill` holds."""
    out: set[str] = set()
    for t in tools:
        if t.get("ring") != "own":
            continue
        for glob in _globs(_telemetry(t).get("match_skill")):
            if glob.endswith(":*") and len(glob) > 2:
                out.add(glob[:-2])
    return out


# --------------------------------------------------------------------------- aggregates


def _session() -> dict[str, Any]:
    return {
        **dict.fromkeys(COUNTS, 0),
        "listing_chars_max": None,
        **{k: set() for k in NAME_LISTS},
        **{k: Counter() for k in MAPS},
    }


def per_session(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each session's value over its rows: sums, the maximum, unions."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = out.setdefault(r["session_id"], _session())
        for key in COUNTS:
            s[key] += r[key]
        chars = r["listing_chars_max"]
        if chars is not None:
            s["listing_chars_max"] = (
                chars if s["listing_chars_max"] is None else max(s["listing_chars_max"], chars)
            )
        for key in NAME_LISTS:
            s[key].update(r[key])
        for key in MAPS:
            s[key].update(r[key])
    return out


def is_active(s: dict[str, Any]) -> bool:
    return s["user_prompts"] >= 1 and s["assistant_messages"] >= 1


def _top(counts: Counter, key: str) -> list[dict[str, Any]]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    return [{"name": name, key: n} for name, n in ranked]


def _shown_sessions(active: list[dict[str, Any]], field: str, shown) -> Counter:
    """Distinct sessions per printed name of a name-list field."""
    out: Counter = Counter()
    for s in active:
        out.update({shown(n) for n in s[field]})
    return out


def _shown_sum(active: list[dict[str, Any]], field: str, shown) -> Counter:
    """The summed counts per printed name of a map field."""
    out: Counter = Counter()
    for s in active:
        for name, n in s[field].items():
            out[shown(name)] += n
    return out


def analyse(rows: list[dict[str, Any]], names: Names, own: set[str]) -> dict[str, Any]:
    """The report's figures over the rows of the measured days."""
    sessions = per_session(rows)
    active = [s for s in sessions.values() if is_active(s)]

    def having(test) -> int:
        return sum(1 for s in active if test(s))

    def total(key: str) -> dict[str, int]:
        return {"count": sum(s[key] for s in active), "sessions": having(lambda s: s[key] > 0)}

    chars = [s["listing_chars_max"] for s in active if s["listing_chars_max"] is not None]

    # Grouped by the PRINTED prefix, so two prefixes redacted to one placeholder are one
    # group; it is own when any prefix behind it is.
    groups: dict[str | None, dict[str, Any]] = {}
    for sid, s in sessions.items():
        if not is_active(s):
            continue
        for raw in s["listing_name_only"]:
            head = raw.split(":", 1)[0] if ":" in raw else ""
            label = names.prefix(head) if head else None
            g = groups.setdefault(label, {"own": False, "sessions": set(), "names": {}})
            g["own"] = g["own"] or head in own
            g["sessions"].add(sid)
            g["names"].setdefault(names.skill(raw), set()).add(sid)
    by_prefix = [
        {
            "prefix": label,
            "own": g["own"],
            "sessions": len(g["sessions"]),
            "names": [
                {"name": n, "sessions": len(sids)}
                for n, sids in sorted(g["names"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
            ],
        }
        for label, g in sorted(
            groups.items(), key=lambda kv: (kv[0] is None, -len(kv[1]["sessions"]), kv[0] or "")
        )
    ]

    def thread(suffix: str) -> dict[str, Any]:
        return {
            "calls": sum(s[f"toolsearch_calls{suffix}"] for s in active),
            "select": sum(s[f"toolsearch_select{suffix}"] for s in active),
            "keyword": sum(s[f"toolsearch_keyword{suffix}"] for s in active),
            "sessions": having(lambda s: s[f"toolsearch_calls{suffix}"] > 0),
            "selected_most_often": _top(
                _shown_sum(active, f"toolsearch_selected{suffix}", names.tool), "count"
            ),
        }

    named = {
        "count": sum(sum(s["router_named"].values()) for s in active),
        "sessions": having(lambda s: sum(s["router_named"].values()) > 0),
    }
    return {
        "sessions": {"with_a_row": len(sessions), "active": len(active)},
        "listing": {
            "sessions_with_listing": having(lambda s: s["listing_count"] > 0),
            "listing_chars_max": {
                "sessions": len(chars),
                "median": round(statistics.median(chars), 1) if chars else None,
                "max": max(chars) if chars else None,
            },
            "sessions_with_name_only": having(lambda s: bool(s["listing_name_only"])),
            "name_only_by_prefix": by_prefix,
        },
        "deferred_tools": {
            "sessions_with_delta": having(lambda s: s["deferred_delta_count"] > 0),
            "announced_most_often": _top(
                _shown_sessions(active, "deferred_added_names", names.tool), "sessions"
            ),
        },
        "toolsearch": {
            "main": thread(""),
            "subagents": thread("_side"),
            "select_unmatched": total("toolsearch_select_unmatched"),
        },
        "router": {
            "sessions_with_injection": having(lambda s: s["router_injections"] > 0),
            "injections": sum(s["router_injections"] for s in active),
            "named": named,
            "named_loaded_after": total("router_named_loaded_after"),
            "unparsed": total("router_unparsed"),
            "unmatched": total("router_unmatched"),
        },
        "dispatch_line": {
            "sessions": having(lambda s: s["dispatch_lines"] > 0),
            "lines": sum(s["dispatch_lines"] for s in active),
        },
        "skills_loaded": {
            "sessions": having(
                lambda s: sum(s["skills_loaded"].values()) + s["skills_loaded_unmatched"] > 0
            ),
            "most_often": _top(_shown_sum(active, "skills_loaded", names.skill), "count"),
            "unmatched": total("skills_loaded_unmatched"),
        },
        "compactions": total("compact_boundaries"),
    }


def coverage(
    since: dt.date, until: dt.date, first_day: dt.date, end_day: dt.date
) -> dict[str, Any]:
    """Which days of the window the table measures, which it measures in part, and which
    it does not measure at all."""
    one = dt.timedelta(days=1)
    lo, hi = max(since, first_day), min(until, end_day)
    unmeasured = []
    if first_day > since:
        unmeasured.append(
            {
                "from": since.isoformat(),
                "to": min(until, first_day - one).isoformat(),
                "reason": "before the table's first row",
            }
        )
    if end_day < until:
        unmeasured.append(
            {
                "from": max(since, end_day + one).isoformat(),
                "to": until.isoformat(),
                "reason": "after the table's records end",
            }
        )
    return {
        "window_days": (until - since).days + 1,
        "measured_from": lo.isoformat(),
        "measured_to": hi.isoformat(),
        "measured_days": (hi - lo).days + 1,
        "partial_day": end_day.isoformat() if since <= end_day <= until else None,
        "unmeasured": unmeasured,
    }


# ------------------------------------------------------------------------------ output


def _n(v: object) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _of(x: int, y: int) -> str:
    return f"{x:,} of {y:,} active sessions"


def _counted(figure: dict[str, int], y: int) -> str:
    return f"{figure['count']:,}, in {_of(figure['sessions'], y)}"


def _names_table(items: list[dict[str, Any]], key: str, header: str) -> list[str]:
    if not items:
        return ["(none)"]
    lines = [f"| name | {header} |", "|---|---:|"]
    lines += [f"| {md_code(i['name'])} | {i[key]:,} |" for i in items]
    return lines


def render_md(doc: dict[str, Any]) -> str:
    w, t, c = doc["window"], doc["table"], doc["coverage"]
    y = doc["sessions"]["active"]
    age = f", {t['age_hours']} h ago" if t["age_hours"] is not None else ""
    L = [f"# Tool discovery, {w['since']} to {w['until']}", "", f"> {DESCRIPTIVE_BANNER}", ""]
    if t["unreadable_rows"]:
        L += [
            f"[WARN] {t['unreadable_rows']:,} line(s) of the table are not rows in the "
            f"{FORMAT} shape and were skipped: no figure below counts them.",
            "",
        ]
    L += [
        f"- environment: {doc['environment']}",
        f"- table: `{t['discovery_format']}`, derived {t['generated_at'] or 'at an unknown time'}"
        f"{age}; its records end {t['records_end']}",
        f"- days measured: {c['measured_from']} to {c['measured_to']}, "
        f"{c['measured_days']:,} of the window's {c['window_days']:,} days",
    ]
    if c["partial_day"]:
        L.append(
            f"- partial: {c['partial_day']}, the day the table's records end - its figures "
            "count only what was recorded before then"
        )
    for u in c["unmeasured"]:
        days = u["from"] if u["from"] == u["to"] else f"{u['from']} to {u['to']}"
        L.append(
            f"- unmeasured: {days} ({u['reason']}) - no figure below covers these days, "
            "and none is a zero for them"
        )
    L.append(f"- rows read: {t['rows_in_window']:,}")
    if t["rows_after_records_end"]:
        L.append(f"- rows dated after the records end, not read: {t['rows_after_records_end']:,}")

    s, li = doc["sessions"], doc["listing"]
    ch = li["listing_chars_max"]
    L += [
        "",
        "## Sessions",
        "",
        f"- sessions active in the window: {s['active']:,} of the {s['with_a_row']:,} "
        "sessions with a row in it",
        "",
        "## Skill listing",
        "",
        f"- sessions with a listing: {_of(li['sessions_with_listing'], y)}",
        f"- per-session `listing_chars_max`: median {_n(ch['median'])}, maximum "
        f"{_n(ch['max'])}, over {_of(ch['sessions'], y)} (those with a value)",
        f"- sessions whose listing rendered at least one entry name-only: "
        f"{_of(li['sessions_with_name_only'], y)}",
        "",
        f"Name-only entries by plugin prefix (sessions, of {y:,} active):",
        "",
    ]
    if li["name_only_by_prefix"]:
        L += ["| prefix | own | sessions | names (sessions) |", "|---|---|---:|---|"]
        for g in li["name_only_by_prefix"]:
            listed = ", ".join(f"{md_code(n['name'])} ({n['sessions']:,})" for n in g["names"])
            prefix = md_code(g["prefix"]) if g["prefix"] is not None else "(no prefix)"
            L.append(f"| {prefix} | {'own' if g['own'] else ''} | {g['sessions']:,} | {listed} |")
    else:
        L.append("(none)")

    d = doc["deferred_tools"]
    L += [
        "",
        "## Deferred tools",
        "",
        f"- sessions with a deferred-tools delta: {_of(d['sessions_with_delta'], y)}",
        "",
        f"Announced most often (top {TOP}, sessions of {y:,} active):",
        "",
        *_names_table(d["announced_most_often"], "sessions", "sessions"),
    ]

    ts = doc["toolsearch"]
    L += [
        "",
        "## ToolSearch",
        "",
        "| thread | select calls | keyword calls | all calls | sessions with a call |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, key in (("main thread", "main"), ("subagents", "subagents")):
        th = ts[key]
        L.append(
            f"| {label} | {th['select']:,} | {th['keyword']:,} | {th['calls']:,} | "
            f"{_of(th['sessions'], y)} |"
        )
    L += [
        "",
        f"- `select:` tokens that matched no announced name (main thread): "
        f"{_counted(ts['select_unmatched'], y)}",
    ]
    for label, key in (("main thread", "main"), ("subagents", "subagents")):
        L += [
            "",
            f"Selected most often, {label} (top {TOP}, selections):",
            "",
            *_names_table(ts[key]["selected_most_often"], "count", "selections"),
        ]

    r = doc["router"]
    L += [
        "",
        "## Router",
        "",
        f"- sessions with an injection: {_of(r['sessions_with_injection'], y)}",
        f"- injections: {r['injections']:,}, in {_of(r['sessions_with_injection'], y)}",
        f"- named suggestions: {_counted(r['named'], y)}",
        f"- suggestions followed by a load of the same skill: "
        f"{_counted(r['named_loaded_after'], y)}",
        f"- unparsed blocks: {_counted(r['unparsed'], y)}",
        f"- unmatched ids: {_counted(r['unmatched'], y)}",
    ]

    dl, sk, cp = doc["dispatch_line"], doc["skills_loaded"], doc["compactions"]
    L += [
        "",
        "## Dispatch line",
        "",
        f"- sessions with at least one dispatch line: {_of(dl['sessions'], y)}; "
        f"dispatch lines: {dl['lines']:,}",
        "",
        "## Skills loaded",
        "",
        f"- sessions with at least one Skill load, matched or unmatched (main thread): "
        f"{_of(sk['sessions'], y)}",
        f"- loads that matched no listed name: {_counted(sk['unmatched'], y)}",
        "",
        f"Loaded most often, main thread (top {TOP}, loads):",
        "",
        *_names_table(sk["most_often"], "count", "loads"),
        "",
        "## Compactions",
        "",
        f"- compactions (main thread): {_counted(cp, y)}",
        "",
        "## Definitions",
        "",
        "- A session is active in the window when its rows there sum to at least one user "
        "prompt and at least one assistant message. Every figure counts active sessions "
        "only; the first counts them against every session with a row in the window.",
        "- A session's value is the sum of its rows in the window, or their maximum for "
        "`listing_chars_max`; a median is over sessions, those with a null value left out.",
        f"- A most-often list is the top {TOP} by count, ties broken by name: a name list "
        "counts the distinct sessions that hold a name, a map the sum of its counts.",
        "- A name-only entry is grouped by the plugin prefix before its `:`; a prefix is own "
        "when an `own` entry's `[telemetry].match_skill` holds the glob `<prefix>:*`.",
        "- The window is inclusive at both ends, by the UTC date of a row. Days after the UTC "
        "date of the table's records end, and days before its first row, are unmeasured.",
    ]
    return "\n".join(L)


# -------------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar discovery",
        description="What the sessions in a window were shown and what they reached for - "
        "the skill listing, deferred tools, ToolSearch, router suggestions, the dispatch "
        "line, Skill loads and compactions - read from the table the profile's "
        "[paths].discovery_index names, in discovery table format, version 1. Writes "
        "nothing, and names no project, slug or path.",
    )
    ap.add_argument("--since", required=True, type=_date, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--until", type=_date, help="YYYY-MM-DD, inclusive; default today (UTC)")
    ap.add_argument(
        "--env",
        type=environment_name,
        help="environment name (default: the top-level default_environment key of radar.toml)",
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml",
    )
    ap.add_argument("--json", action="store_true", help="print one JSON document instead")
    return ap


def _warn(message: str) -> NoReturn:
    print(f"[WARN] {message}")
    sys.exit(1)


def _quoted(value: object) -> str:
    """A value read from `derive.json`, shown in a warning without letting a long one
    through whole."""
    text = json.dumps(value)
    return text if len(text) <= 60 else text[:57] + "..."


@reports_profile_errors
def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    until = args.until or dt.datetime.now(dt.UTC).date()
    if until < args.since:
        ap.error(f"--until {until} is before --since {args.since}")
    # The banner goes to stderr, which is what keeps `--json` a parseable document.
    root = begin_command(args.data_root)
    args.env = args.env or default_environment(root)
    env = load_environment(args.env)
    if env is None:
        print(f"[FAIL] no environment named {args.env!r}")
        sys.exit(1)

    # Each absence is named, and none of them is read as an empty table.
    raw = (env.get("paths") or {}).get(PROFILE_KEY)
    if not raw:
        _warn(
            f"the {args.env!r} profile declares no [paths].{PROFILE_KEY} - there is no "
            "discovery table to read"
        )
    table = Path(str(raw).replace("\\", "/"))
    if not table.is_file():
        _warn(
            f"no discovery table at the path [paths].{PROFILE_KEY} names - it has not been "
            "written there, or the key names the wrong file"
        )
    meta_path = table.parent / "derive.json"
    if not meta_path.is_file():
        _warn(
            "no derive.json beside the discovery table - the table's format and where its "
            "records end are unknown, so no row is read"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if not isinstance(meta, dict):
        _warn("the derive.json beside the discovery table is not a JSON object - no row is read")
    fmt = meta.get("discovery_format")
    if fmt is None:
        _warn(
            "derive.json carries no discovery_format - the table's format is unknown, so no "
            "row is read"
        )
    if fmt != FORMAT:
        _warn(
            f"derive.json's discovery_format is {_quoted(fmt)}, a format this engine does "
            f"not read (it reads {FORMAT}) - no row is read"
        )
    end_raw = meta.get("records_end")
    if end_raw is None:
        _warn(
            "derive.json carries no records_end - where the table's records end is unknown, "
            "so no day of the window can be called measured"
        )
    records_end = utc_moment(end_raw) if isinstance(end_raw, str) else None
    if records_end is None:
        _warn(f"derive.json's records_end {_quoted(end_raw)} is not an ISO 8601 time")
    end_day = records_end.date()
    gen_raw = meta.get("generated_at")
    generated = utc_moment(gen_raw) if isinstance(gen_raw, str) else None

    try:
        rows, unreadable = read_table(table)
    except OSError as exc:
        _warn(f"the discovery table could not be read ({exc.strerror or type(exc).__name__})")
    first_day = min((r["date"] for r in rows), default=None)
    in_window = [r for r in rows if args.since <= r["date"] <= until]
    measured = [r for r in in_window if r["date"] <= end_day]
    if first_day is None or not measured:
        held = (
            f"its rows run from {first_day} to {max(r['date'] for r in rows)}"
            if first_day is not None
            else "it holds no row in the format's shape"
        )
        beyond = len(in_window)
        _warn(
            f"the discovery table has no row from {args.since} to {until} ({held}; its "
            f"records end {end_day}"
            + (f"; {beyond} row(s) of the window are dated after that" if beyond else "")
            + (f"; {unreadable} line(s) not in the format's shape" if unreadable else "")
            + ")"
        )

    tools = load_tools()
    names = Names(tools, [home_dir(), *extra_homes()])
    doc = {
        "schema_version": SCHEMA_VERSION,
        "window": {"since": args.since.isoformat(), "until": until.isoformat()},
        "environment": args.env,
        "table": {
            "discovery_format": FORMAT,
            "generated_at": generated.isoformat() if generated else None,
            "age_hours": derive_age_hours(generated.isoformat()) if generated else None,
            "records_end": records_end.isoformat(),
            "first_day": first_day.isoformat(),
            "rows_in_window": len(measured),
            "rows_after_records_end": len(in_window) - len(measured),
            "unreadable_rows": unreadable,
        },
        "coverage": coverage(args.since, until, first_day, end_day),
        **analyse(measured, names, own_prefixes(tools)),
    }
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(render_md(doc))


if __name__ == "__main__":
    main()
