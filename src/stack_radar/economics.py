"""`radar economics` - the token economics of the sessions in a window, from transcripts.

WHY THIS VIEW EXISTS. `radar field` asks whether one catalogue entry had an opportunity
and was used; nothing in the engine asked what the sessions themselves cost in tokens,
or through which mechanism, although Claude Code writes the answer into the same
transcripts: every assistant record carries a `message.usage` block. Measured by hand,
that block shows costs the session's own length does not explain - cache re-created
after the cache's time to live expired during a long wait, and cache reads that grow with
the square of the number of turns rather than with the cold start. This verb makes those
figures something a command prints rather than something worked out once by hand.

WHAT THE NUMBERS ARE NOT. They are DESCRIPTIVE, not causal: there is no control arm, and a
share of re-creation says where tokens went, not what would have happened otherwise. And
they are TOKENS, not prices: no rate card is applied, because prices differ by model, by
class of token and over time, and a figure converted at the wrong rate looks exactly as
precise as one converted at the right rate.

DEFINITIONS.

- A turn is one deduplicated assistant message. Claude Code writes a streamed message
  several times under one `message.id`, so records are merged by that id within a
  transcript: each numeric field takes its largest value, the turn takes its earliest
  timestamp, and the message's own model (never the file's) is the turn's model.
- A field the record does not carry is absent, not zero: totals add only the values
  present, and the report says how many turns lacked each field. context(k) sums the parts
  present.
- context(k) = input + cache_creation + cache_read of turn k.
- Per transcript, in timestamp order: T = turns, C0 = context(1), Cf = context(T), and
  g = (Cf - C0) / (T - 1), the mean growth per turn, defined when T >= 2.
- Uncached input = input + cache_creation: the input the cache did not serve.
- A cache re-creation event is a turn k >= 2 whose cache_creation exceeds
  0.5 x context(k - 1): most of the context the previous turn had was written to the
  cache again. Its gap is the time in seconds since turn k - 1.

THE WINDOW is inclusive at both ends, by the UTC date of a turn's earliest timestamp.

WRITES NOTHING, and its outputs name no project, no path, no message content and no tool
input: a transcript is identified only by its session id and, for a sidechain, its file
stem. The Markdown goes to stdout by default; `--json` prints one document instead.

THE HONEST ZERO. A window in which no assistant message carries usage prints one `[WARN]`
line naming how many transcripts were read and exits 1: a table of zeros there would
read exactly like a measurement of sessions that cost nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from .field_report import discover
from .radar_lib import (
    begin_command,
    default_environment,
    environment_name,
    load_environment,
    reports_profile_errors,
)

SCHEMA_VERSION = 1

DESCRIPTIVE_BANNER = (
    "DESCRIPTIVE ONLY - no control arm, no counterfactual. These figures say where tokens "
    "went in the window, not what would have happened otherwise. Tokens, not prices: no "
    "rate card is applied."
)

# The four top-level counters of a usage block, in the order a reader adds them up.
FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
# Every field a turn may lack, the nested ones included.
ALL_FIELDS = (*FIELDS, "thinking_tokens", "ttl_breakdown")

RECREATION_RATIO = 0.5
LONG_GAP_SECONDS = 300  # the 5-minute cache time to live
TOP_TRANSCRIPTS = 10
USAGE_NEEDLE = b'"usage"'


# ----------------------------------------------------------------------------- reading


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _int(value: object) -> int | None:
    # bool is an int to Python and never a token count.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _usage_fields(usage: dict) -> dict[str, int]:
    """The numeric fields a usage block carries, flattened. Absent ones are left out."""
    out: dict[str, int] = {}
    for key in FIELDS:
        v = _int(usage.get(key))
        if v is not None:
            out[key] = v
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        v = _int(details.get("thinking_tokens"))
        if v is not None:
            out["thinking_tokens"] = v
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        for key, name in (
            ("ephemeral_5m_input_tokens", "ephemeral_5m"),
            ("ephemeral_1h_input_tokens", "ephemeral_1h"),
        ):
            v = _int(creation.get(key))
            if v is not None:
                out[name] = v
    return out


def _timestamp(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


class Counters:
    """What reading the files found that was not a turn."""

    def __init__(self) -> None:
        self.malformed = 0
        self.undated = 0


def read_turns(path: Path, counters: Counters) -> list[dict[str, Any]]:
    """Every deduplicated assistant message of one transcript, in timestamp order.

    Streamed line by line; a line without the bytes `"usage"` is skipped before parsing,
    which is most of a transcript.
    """
    by_id: dict[str, dict[str, Any]] = {}
    order = 0
    with path.open("rb") as fh:
        for line in fh:
            if USAGE_NEEDLE not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                counters.malformed += 1
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            msg_id, model, usage = msg.get("id"), msg.get("model"), msg.get("usage")
            if not (isinstance(msg_id, str) and isinstance(model, str)):
                continue
            if not isinstance(usage, dict) or model == "<synthetic>":
                continue
            ts = _timestamp(rec.get("timestamp"))
            if ts is None:
                counters.undated += 1
                continue
            fields = _usage_fields(usage)
            turn = by_id.get(msg_id)
            if turn is None:
                by_id[msg_id] = {"ts": ts, "model": model, "order": order, "fields": fields}
                order += 1
                continue
            turn["ts"] = min(turn["ts"], ts)
            for key, v in fields.items():
                turn["fields"][key] = max(turn["fields"].get(key, v), v)
    return sorted(by_id.values(), key=lambda t: (t["ts"], t["order"]))


def context(fields: dict[str, int]) -> int:
    return sum(fields.get(k, 0) for k in FIELDS[:3])


def uncached(fields: dict[str, int]) -> int:
    return fields.get("input_tokens", 0) + fields.get("cache_creation_input_tokens", 0)


# --------------------------------------------------------------------------- aggregates


def _block() -> dict[str, int]:
    return {
        "transcripts": 0,
        "turns": 0,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
    }


def _add(block: dict[str, int], fields: dict[str, int]) -> None:
    block["turns"] += 1
    for key in (*FIELDS, "thinking_tokens"):
        block[key] += fields.get(key, 0)
    block["uncached_input_tokens"] += uncached(fields)


def _summary(values: list[float], *, total: bool = False) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {
        "n": len(values),
        "median": round(statistics.median(values), 1) if values else None,
        "mean": round(statistics.fmean(values), 1) if values else None,
    }
    if total:
        out["total"] = int(sum(values))
    return out


def analyse(
    files: list[tuple[Path, str, str, bool]], since: dt.date, until: dt.date
) -> dict[str, Any]:
    """The report document, before the run's own metadata is added."""
    counters = Counters()
    totals = _block()
    sides = {"main": _block(), "sidechain": _block()}
    models: dict[str, dict[str, int]] = {}
    missing = dict.fromkeys(ALL_FIELDS, 0)
    ttl = {
        "ephemeral_5m_tokens": 0,
        "ephemeral_1h_tokens": 0,
        "unattributed_tokens": 0,
        "unattributed_turns": 0,
        "turns_with_creation": 0,
    }
    events: list[tuple[int, float]] = []
    eligible = 0
    rows: list[dict[str, Any]] = []
    read = 0
    # A transcript last written before the window opened holds no record inside it.
    floor = dt.datetime.combine(since, dt.time(), tzinfo=dt.UTC).timestamp()

    for path, session_id, _slug, is_subagent in files:
        try:
            if path.stat().st_mtime < floor:
                continue
            turns = read_turns(path, counters)
        except OSError:
            counters.malformed += 1
            continue
        read += 1
        turns = [t for t in turns if since <= t["ts"].date() <= until]
        if not turns:
            continue
        side = sides["sidechain" if is_subagent else "main"]
        totals["transcripts"] += 1
        side["transcripts"] += 1
        row_events = 0
        prev: dict[str, Any] | None = None
        for k, turn in enumerate(turns, start=1):
            f = turn["fields"]
            _add(totals, f)
            _add(side, f)
            m = models.setdefault(
                turn["model"],
                {
                    "turns": 0,
                    "output_tokens": 0,
                    "uncached_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            )
            m["turns"] += 1
            m["output_tokens"] += f.get("output_tokens", 0)
            m["uncached_input_tokens"] += uncached(f)
            m["cache_read_input_tokens"] += f.get("cache_read_input_tokens", 0)
            for key in (*FIELDS, "thinking_tokens"):
                if key not in f:
                    missing[key] += 1
            has_ttl = "ephemeral_5m" in f or "ephemeral_1h" in f
            if not has_ttl:
                missing["ttl_breakdown"] += 1
            created = f.get("cache_creation_input_tokens", 0)
            if created > 0:
                ttl["turns_with_creation"] += 1
                if has_ttl:
                    ttl["ephemeral_5m_tokens"] += f.get("ephemeral_5m", 0)
                    ttl["ephemeral_1h_tokens"] += f.get("ephemeral_1h", 0)
                else:
                    ttl["unattributed_tokens"] += created
                    ttl["unattributed_turns"] += 1
            if prev is not None:
                eligible += 1
                if created > RECREATION_RATIO * context(prev["fields"]):
                    gap = (turn["ts"] - prev["ts"]).total_seconds()
                    events.append((created, gap))
                    row_events += 1
            prev = turn
        t = len(turns)
        c0, cf = context(turns[0]["fields"]), context(turns[-1]["fields"])
        rows.append(
            {
                "session_id": session_id,
                "agent": path.stem if is_subagent else None,
                "sidechain": is_subagent,
                "T": t,
                "C0": c0,
                "Cf": cf,
                "g": round((cf - c0) / (t - 1), 1) if t >= 2 else None,
                "recreation_events": row_events,
            }
        )

    gaps = [g for _, g in events]
    created_total = sum(c for c, _ in events)
    uncached_total = totals["uncached_input_tokens"]
    rows.sort(key=lambda r: (r["session_id"], r["agent"] or ""))
    return {
        "transcripts_read": read,
        "malformed_lines": counters.malformed,
        "undated_records": counters.undated,
        "totals": totals,
        "by_side": sides,
        "by_model": dict(sorted(models.items())),
        "missing_fields": missing,
        "ttl": ttl,
        "recreation": {
            "events": len(events),
            "eligible_turns": eligible,
            "tokens": created_total,
            "share_of_uncached_input": (
                round(created_total / uncached_total, 4) if uncached_total else None
            ),
            "gap_seconds_median": round(statistics.median(gaps), 1) if gaps else None,
            "gap_seconds_max": round(max(gaps), 1) if gaps else None,
            "gap_over_300s": sum(1 for g in gaps if g > LONG_GAP_SECONDS),
        },
        "distribution": {
            "transcripts": len(rows),
            "T": _summary([float(r["T"]) for r in rows], total=True),
            "C0": _summary([float(r["C0"]) for r in rows]),
            "Cf": _summary([float(r["Cf"]) for r in rows]),
            "g": _summary([float(r["g"]) for r in rows if r["g"] is not None]),
        },
        "per_transcript": rows,
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


def _share(num: int, den: int) -> str:
    return f"{num:,}/{den:,} ({100.0 * num / den:.1f}%)" if den else f"{num:,}/0 (n/a)"


def render_md(doc: dict[str, Any]) -> str:
    t = doc["totals"]
    turns, uncached_total = t["turns"], t["uncached_input_tokens"]
    all_tokens = uncached_total + t["cache_read_input_tokens"] + t["output_tokens"]
    w = doc["window"]
    L = [
        f"# Token economics, {w['since']} to {w['until']}",
        "",
        f"> {DESCRIPTIVE_BANNER}",
        "",
        f"- environment: {doc['environment'] or '(none, --claude-home given)'}",
        f"- transcripts read: {doc['transcripts_read']:,}; with a turn in the window: "
        f"{t['transcripts']:,}",
        f"- malformed lines skipped: {doc['malformed_lines']:,}; assistant records without "
        f"a timestamp skipped: {doc['undated_records']:,}",
        "",
        "## Totals by class",
        "",
        "| class | tokens | share of all tokens |",
        "|---|---:|---:|",
        f"| output | {_n(t['output_tokens'])} | {_share(t['output_tokens'], all_tokens)} |",
        f"| of which thinking | {_n(t['thinking_tokens'])} | "
        f"{_share(t['thinking_tokens'], all_tokens)} |",
        f"| uncached input (input + cache creation) | {_n(uncached_total)} | "
        f"{_share(uncached_total, all_tokens)} |",
        f"| - input | {_n(t['input_tokens'])} | {_share(t['input_tokens'], all_tokens)} |",
        f"| - cache creation | {_n(t['cache_creation_input_tokens'])} | "
        f"{_share(t['cache_creation_input_tokens'], all_tokens)} |",
        f"| cache read | {_n(t['cache_read_input_tokens'])} | "
        f"{_share(t['cache_read_input_tokens'], all_tokens)} |",
        "",
        f"Turns: {turns:,}. Transcripts: {t['transcripts']:,}.",
        "",
        "Turns lacking a field (absent, not counted as zero): "
        + "; ".join(f"{k} {_share(v, turns)}" for k, v in doc["missing_fields"].items())
        + ".",
        "",
        "## Main session and sidechain",
        "",
        "| side | transcripts | turns | output | uncached input | cache read |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, b in doc["by_side"].items():
        L.append(
            f"| {name} | {_share(b['transcripts'], t['transcripts'])} | "
            f"{_share(b['turns'], turns)} | {_n(b['output_tokens'])} | "
            f"{_n(b['uncached_input_tokens'])} | {_n(b['cache_read_input_tokens'])} |"
        )
    L += [
        "",
        "## By model",
        "",
        "Attributed per message, not per file.",
        "",
        "| model | turns | output | output share | uncached input | cache read |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for model, m in doc["by_model"].items():
        L.append(
            f"| {model} | {_share(m['turns'], turns)} | {_n(m['output_tokens'])} | "
            f"{_share(m['output_tokens'], t['output_tokens'])} | "
            f"{_n(m['uncached_input_tokens'])} | {_n(m['cache_read_input_tokens'])} |"
        )
    ttl = doc["ttl"]
    created = t["cache_creation_input_tokens"]
    L += [
        "",
        "## Cache creation by time to live",
        "",
        "| time to live | tokens | share of cache creation |",
        "|---|---:|---:|",
        f"| 5 minutes | {_n(ttl['ephemeral_5m_tokens'])} | "
        f"{_share(ttl['ephemeral_5m_tokens'], created)} |",
        f"| 1 hour | {_n(ttl['ephemeral_1h_tokens'])} | "
        f"{_share(ttl['ephemeral_1h_tokens'], created)} |",
        f"| unattributed (no breakdown) | {_n(ttl['unattributed_tokens'])} | "
        f"{_share(ttl['unattributed_tokens'], created)} |",
        "",
        f"Turns that created cache without a breakdown: "
        f"{_share(ttl['unattributed_turns'], ttl['turns_with_creation'])}.",
    ]
    rec = doc["recreation"]
    L += [
        "",
        "## Cache re-creation",
        "",
        f"- events: {_share(rec['events'], rec['eligible_turns'])} of the turns k >= 2",
        f"- tokens re-created: {_n(rec['tokens'])}, "
        f"{_share(rec['tokens'], uncached_total)} of uncached input",
        f"- gap to the previous turn: median {_n(rec['gap_seconds_median'])} s, "
        f"max {_n(rec['gap_seconds_max'])} s",
        f"- events after a gap over {LONG_GAP_SECONDS} s: "
        f"{_share(rec['gap_over_300s'], rec['events'])}",
    ]
    d = doc["distribution"]
    L += [
        "",
        "## Per transcript",
        "",
        "| figure | n | median | mean | total |",
        "|---|---:|---:|---:|---:|",
        f"| T (turns) | {d['T']['n']:,} | {_n(d['T']['median'])} | {_n(d['T']['mean'])} | "
        f"{_n(d['T']['total'])} |",
        f"| C0 (cold start) | {d['C0']['n']:,} | {_n(d['C0']['median'])} | "
        f"{_n(d['C0']['mean'])} | - |",
        f"| Cf (final context) | {d['Cf']['n']:,} | {_n(d['Cf']['median'])} | "
        f"{_n(d['Cf']['mean'])} | - |",
        f"| g (growth per turn, T >= 2) | {_share(d['g']['n'], d['transcripts'])} | "
        f"{_n(d['g']['median'])} | {_n(d['g']['mean'])} | - |",
        "",
        f"Largest {min(TOP_TRANSCRIPTS, len(doc['per_transcript']))} by final context:",
        "",
        "| session | agent | T | C0 | Cf | g | re-creation events |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    top = sorted(doc["per_transcript"], key=lambda r: r["Cf"], reverse=True)[:TOP_TRANSCRIPTS]
    for r in top:
        L.append(
            f"| {r['session_id']} | {r['agent'] or '(main)'} | {r['T']:,} | {_n(r['C0'])} | "
            f"{_n(r['Cf'])} | {_n(r['g'])} | {r['recreation_events']:,} |"
        )
    L += [
        "",
        "## Definitions",
        "",
        "- A turn is one assistant message, deduplicated by `message.id` within a "
        "transcript: the largest value of each field, the earliest timestamp, the "
        "message's own model.",
        "- context(k) = input + cache_creation + cache_read of turn k (the fields present).",
        "- Per transcript: T = turns; C0 = context(1); Cf = context(T); "
        "g = (Cf - C0) / (T - 1) when T >= 2.",
        "- Uncached input = input + cache_creation.",
        f"- A cache re-creation event is a turn k >= 2 whose cache_creation exceeds "
        f"{RECREATION_RATIO} x context(k - 1); its gap is the seconds since turn k - 1.",
        "- The window is inclusive, by the UTC date of a turn's earliest timestamp.",
    ]
    return "\n".join(L)


# -------------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar economics",
        description="Descriptive token economics of the sessions in a window, read from "
        "the usage blocks of session transcripts. Writes nothing; tokens, not prices.",
    )
    ap.add_argument("--since", required=True, type=_date, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--until", type=_date, help="YYYY-MM-DD, inclusive; default today (UTC)")
    ap.add_argument(
        "--env",
        type=environment_name,
        help="environment name (default: the top-level default_environment key of radar.toml)",
    )
    ap.add_argument("--claude-home", help="override the environment's [paths].claude_home")
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml",
    )
    ap.add_argument("--json", action="store_true", help="print one JSON document instead")
    return ap


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
    if env is None and not args.claude_home:
        print(f"[FAIL] no environment named {args.env!r} and no --claude-home")
        sys.exit(1)
    home = Path(args.claude_home or (env or {}).get("paths", {}).get("claude_home", ""))
    projects = home / "projects"
    if not projects.is_dir():
        print(f"[FAIL] no transcript directory at {projects}")
        sys.exit(1)

    body = analyse(discover(projects), args.since, until)
    if body["totals"]["turns"] == 0:
        print(
            f"[WARN] read {body['transcripts_read']} transcript(s), found 0 assistant "
            f"messages with usage between {args.since} and {until}"
        )
        sys.exit(1)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "window": {"since": args.since.isoformat(), "until": until.isoformat()},
        "environment": args.env if env is not None else None,
        **body,
    }
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(render_md(doc))


if __name__ == "__main__":
    main()
