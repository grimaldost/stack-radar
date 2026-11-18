"""`radar field --all` reads each transcript once per window, and every report it writes
is the report the entry gets on its own.

THE CONTRACT. An `--all` command used to run the whole transcript pass once per catalogue
entry. It now groups the entries by window and gives each window one shared pass: one
raw-bytes read evaluates every entry's prescan needles, and one line read feeds each entry
only the lines its own filter selects. The field report is a document other tools read,
so the shared pass is held to one rule: each `--all` report equals the single-entry report
of the same entry, except the fields and text that carry time.

THE FIXTURE holds one instance of every axis on which one entry's handling can differ from
another's in a shared pass (the population table of the change's specification), and each
axis has a witness assertion, so a fixture edit that quietly stops exercising one fails
here instead of leaving the equivalence true by vacuity.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest
from conftest import bash, note, tool_result, tool_use

from stack_radar import field_report

UNTIL = "2026-08-31"
EARLY = "2026-08-01"
LATE = "2026-08-15"
SLUG = "C--Users-x-Documents-p"
# The longest needle in the fixture, placed across the first 1 MiB chunk boundary.
LONG_NEEDLE = "longtoolname-check"


def epoch(day: str) -> float:
    """Midday UTC on `day`, as an mtime."""
    d = dt.date.fromisoformat(day)
    return dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.UTC).timestamp()


def entry(
    name: str, since: str | None, telemetry: str, *, ring: str = "adopt", extra: str = ""
) -> str:
    since_line = f'since = "{since}"\n' if since else ""
    return (
        f'name = "{name}"\nring = "{ring}"\nartifact = "cli"\n\n'
        f"[telemetry]\n{telemetry}\n{since_line}{extra}"
    )


PILOT = """
[pilot_exit]
review_after_days = 30
adopt_if = "used somewhere"
decline_if = "used nowhere"
min_sessions_with_use = 1
max_error_rate = 0.5

[[history]]
date = "2026-08-01"
ring = "pilot"
reason = "r"
evidence = "e"
"""

# Six entries over two windows, interleaved in name order so the window groups are not
# contiguous: fetchonly, longtool, noprefix, pyglob (early), ruff-late (late), ruff-pilot (early).
ENTRIES = {
    # an opportunity-only hit: its own matcher never fires, its opportunity matcher does
    "fetchonly": entry(
        "fetchonly",
        EARLY,
        'match = ["mcp__never__used"]\nopportunity_match = ["WebFetch"]',
    ),
    # the long needle, across the chunk boundary; a pending id second on a two-id line
    "longtool": entry("longtool", EARLY, f'match_command = ["{LONG_NEEDLE}"]'),
    # no byte needles, first variant: a matcher with no literal prefix
    "noprefix": entry("noprefix", EARLY, 'match = ["*__search-docs"]'),
    # no byte needles, second variant: a command matcher that matches `python`
    "pyglob": entry("pyglob", EARLY, 'match_command = ["py*", "uv"]'),
    # a second window: the same needle as ruff-pilot, read from a later `since`
    "ruff-late": entry("ruff-late", LATE, 'match_command = ["ruff"]'),
    # the short needle; a pilot, so the report carries checks; the malformed line's reader
    "ruff-pilot": entry("ruff-pilot", EARLY, 'match_command = ["ruff"]', ring="pilot", extra=PILOT),
}


def write_lines(path: Path, lines: list[str], mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8", newline="\n")
    os.utime(path, (mtime, mtime))
    return path


def compact(rec: dict) -> str:
    # Compact, as the real transcripts are written (see conftest.Radar.session).
    return json.dumps(rec, separators=(",", ":"))


def two_results(first: str, second: str, ts: str) -> dict:
    """One user record carrying two tool_results, `first` before `second`."""
    rec = tool_result(first, ts)
    rec["message"]["content"].append(
        {"type": "tool_result", "tool_use_id": second, "is_error": False, "content": "ok"}
    )
    return rec


class Corpus:
    """The transcript tree, and the facts about it the witnesses check."""

    def __init__(self, radar) -> None:
        self.radar = radar
        projects = radar.home / "projects" / SLUG
        self.paths: dict[str, Path] = {}

        def main(sid: str, recs: list, day: str) -> None:
            lines = [r if isinstance(r, str) else compact(r) for r in recs]
            self.paths[sid] = write_lines(projects / f"{sid}.jsonl", lines, epoch(day))

        # Outside both windows: only the census and the retention horizon read it.
        main(
            "s00-old",
            [note("2026-07-20T09:00:00.000Z"), note("2026-07-20T10:00:00.000Z")],
            "2026-07-20",
        )

        # A transcript hit by several entries (ruff-pilot, pyglob, noprefix), with a
        # tool_result on a line no needle names.
        s1 = [
            note("2026-08-05T09:00:00.000Z"),
            bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check ."),
            tool_result("t1", "2026-08-05T10:00:01.000Z"),
            tool_use("t2", "mcp__abc__search-docs", "2026-08-05T10:05:00.000Z", q="x"),
            tool_result("t2", "2026-08-05T10:05:02.000Z"),
        ]
        self.result_line = compact(s1[2])
        main("s01-original", s1, "2026-08-05")
        # The same tool_use ids replayed in a later file.
        main(
            "s02-resumed",
            [
                *s1,
                bash("t3", "2026-08-06T10:00:00.000Z", "uv run ruff format ."),
                tool_result("t3", "2026-08-06T10:00:03.000Z", is_error=True),
            ],
            "2026-08-06",
        )
        # A line carrying two tool_use_ids, longtool's pending one second; a malformed
        # line only ruff-pilot's needle selects.
        self.malformed_line = '{"type":"user","note":"ruff'
        main(
            "s03-two-results",
            [
                note("2026-08-07T09:00:00.000Z"),
                bash("tX", "2026-08-07T10:00:00.000Z", "ruff format src"),
                bash("tY", "2026-08-07T10:01:00.000Z", f"{LONG_NEEDLE} run"),
                two_results("tX", "tY", "2026-08-07T10:02:00.000Z"),
                self.malformed_line,
                note("2026-08-07T11:00:00.000Z"),
            ],
            "2026-08-07",
        )
        # The long needle across the first chunk boundary.
        head = compact(note("2026-08-08T09:00:00.000Z", ""))
        call = compact(bash("tB", "2026-08-08T10:00:00.000Z", f"{LONG_NEEDLE} build"))
        at = call.index(LONG_NEEDLE)
        pad = field_report.CHUNK - 8 - at - (len(head) + 1)
        head = compact(note("2026-08-08T09:00:00.000Z", "x" * pad))
        main("s04-boundary", [head, call, note("2026-08-08T11:00:00.000Z")], "2026-08-08")
        # The only WebFetch in the corpus.
        main(
            "s05-fetch",
            [tool_use("tF", "WebFetch", "2026-08-09T10:00:00.000Z", url="https://example.invalid")],
            "2026-08-09",
        )
        # An unreadable transcript: a DIRECTORY with a transcript's name. `discover` lists
        # it, `stat` reads it, and opening it raises OSError on every system and as root.
        self.unreadable = projects / "s06-unreadable.jsonl"
        self.unreadable.mkdir(parents=True)
        os.utime(self.unreadable, (epoch("2026-08-10"), epoch("2026-08-10")))
        # Inside both windows, with a sidechain.
        main(
            "s07-late",
            [
                note("2026-08-20T09:00:00.000Z"),
                bash("tL", "2026-08-20T10:00:00.000Z", "ruff check ."),
                tool_result("tL", "2026-08-20T10:00:02.000Z"),
            ],
            "2026-08-20",
        )
        self.paths["s07-sidechain"] = write_lines(
            projects / "s07-late" / "subagents" / "agent-1.jsonl",
            [compact(bash("tS", "2026-08-20T10:30:00.000Z", "uv run ruff check tests"))],
            epoch("2026-08-20"),
        )


@pytest.fixture
def board(radar):
    for name, toml in ENTRIES.items():
        radar.tool(name, toml)
    radar.write_environment(with_index=True)
    radar.index_rows(
        [
            {
                "timestamp": "2026-08-05T10:00:00.000Z",
                "tool": "Bash",
                "first_seen": True,
                "exe": ["uv", "ruff"],
            },
            {
                "timestamp": "2026-08-20T10:00:00.000Z",
                "tool": "Bash",
                "first_seen": True,
                "exe": ["ruff"],
            },
        ],
        generated_at="2026-09-01T00:00:00Z",
    )
    return Corpus(radar)


# ---------------------------------------------------------------- what carries time

AGE_HOURS = re.compile(r"\d+(?:\.\d+)?h ago")
DAYS_IN_PILOT = re.compile(r"\d+d in pilot")
DAYS_DUE = re.compile(r"\(\d+d >= ")
WALL = re.compile(r"; \d+(?:\.\d+)?s wall clock")


def untimed_text(text: str) -> str:
    text = AGE_HOURS.sub("<n>h ago", text)
    text = DAYS_IN_PILOT.sub("<n>d in pilot", text)
    text = DAYS_DUE.sub("(<n>d >= ", text)
    return WALL.sub("; <n>s wall clock", text)


def untimed_doc(doc: dict) -> dict:
    doc = json.loads(untimed_text(json.dumps(doc)))
    del doc["generated_at"]
    del doc["scan"]["wall_clock_s"]
    if doc.get("reconcile"):
        del doc["reconcile"]["age_hours"]
        del doc["reconcile"]["age_days"]
    return doc


def reports(out: Path) -> dict[str, Path]:
    return {p.relative_to(out).as_posix(): p for p in sorted(out.rglob("*")) if p.is_file()}


def run_all(radar, out: Path):
    p = radar.run("--all", "--reconcile", "--until", UNTIL, "--out-dir", str(out))
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def run_single(radar, out: Path) -> None:
    for name in ENTRIES:
        p = radar.run(name, "--reconcile", "--until", UNTIL, "--out-dir", str(out))
        assert p.returncode == 0, name + "\n" + p.stdout + p.stderr


def all_docs(out: Path) -> dict[str, dict]:
    return {
        rel.split("/")[0]: json.loads(path.read_text(encoding="utf-8"))
        for rel, path in reports(out).items()
        if rel.endswith(".json")
    }


# --------------------------------------------------------------------- equivalence


def test_every_all_report_equals_the_single_entry_report(board, tmp_path):
    shared, single = tmp_path / "all", tmp_path / "single"
    run_all(board.radar, shared)
    run_single(board.radar, single)

    got, want = reports(shared), reports(single)
    assert sorted(got) == sorted(want)
    assert len(got) == 2 * len(ENTRIES)
    for rel in sorted(want):
        a = got[rel].read_text(encoding="utf-8")
        b = want[rel].read_text(encoding="utf-8")
        if rel.endswith(".json"):
            assert untimed_doc(json.loads(a)) == untimed_doc(json.loads(b)), rel
        else:
            assert untimed_text(a) == untimed_text(b), rel


def test_the_wall_clock_keeps_its_name_and_type(board, tmp_path):
    out = tmp_path / "all"
    run_all(board.radar, out)
    docs = all_docs(out)
    assert sorted(docs) == sorted(ENTRIES)
    for name, doc in docs.items():
        wall = doc["scan"]["wall_clock_s"]
        assert isinstance(wall, float) and wall >= 0, (name, wall)


# ------------------------------------------------------------------------ witnesses
#
# One per row of the population table: proof the fixture still holds the instance.


@pytest.fixture
def docs(board, tmp_path) -> dict[str, dict]:
    out = tmp_path / "all"
    run_all(board.radar, out)
    return all_docs(out)


def sessions_of(doc: dict) -> list[str]:
    return [i["session"] for i in doc["invocations"]]


def test_witness_entries_read_different_windows(docs):
    assert docs["ruff-late"]["window"]["since"] == LATE
    assert docs["ruff-pilot"]["window"]["since"] == EARLY
    assert docs["ruff-pilot"]["scan"]["in_time_window"] == 8
    assert docs["ruff-late"]["scan"]["in_time_window"] == 2
    assert sessions_of(docs["ruff-late"]) == ["s07-late", "s07-late"]


def test_witness_a_transcript_hit_by_several_entries(docs):
    for name in ("ruff-pilot", "pyglob", "noprefix"):
        assert "s01-original" in sessions_of(docs[name]), name


def test_witness_a_replayed_tool_use_id(docs):
    for name in ("ruff-pilot", "pyglob", "noprefix"):
        assert docs[name]["metrics"]["replayed_dropped"] == 1, name
    assert sessions_of(docs["noprefix"]) == ["s01-original"]


def test_witness_a_result_on_a_line_no_needle_names(board, docs):
    assert "ruff" not in board.result_line
    t1 = docs["ruff-pilot"]["invocations"][0]
    assert t1["session"] == "s01-original"
    assert t1["resolved"] is True
    assert t1["latency_ms"] == 1000.0


def test_witness_the_first_id_only_rule_holds_per_entry(docs):
    by_session = {i["session"]: i for i in docs["longtool"]["invocations"]}
    assert by_session["s03-two-results"]["resolved"] is False
    tx = next(i for i in docs["ruff-pilot"]["invocations"] if i["session"] == "s03-two-results")
    assert tx["resolved"] is True


def test_witness_a_malformed_line_selected_by_one_entry(board, docs):
    assert "ruff" in board.malformed_line and LONG_NEEDLE not in board.malformed_line
    assert docs["ruff-pilot"]["scan"]["malformed_lines"] == 1
    assert docs["longtool"]["scan"]["malformed_lines"] == 0
    # longtool did read that file, so the zero is not for want of opening it.
    assert "s03-two-results" in sessions_of(docs["longtool"])


def test_witness_an_unreadable_transcript(board, docs):
    listed = [f[0] for f in field_report.discover(board.radar.home / "projects")]
    assert board.unreadable in listed
    with pytest.raises(OSError):
        board.unreadable.open("rb")
    # It enters the census by its mtime (no timestamp can be read from it) ...
    assert docs["ruff-pilot"]["metrics"]["sessions_dated_by_mtime"] == 1
    # ... is streamed for an entry with no byte needles ...
    noprefix = docs["noprefix"]["scan"]
    assert noprefix["parsed"] == noprefix["in_time_window"] == 8
    # ... and misses the prescan of an entry with needles: ruff is in s01, s02, s03, s07
    # and the sidechain, five of the eight.
    assert docs["ruff-pilot"]["scan"]["prescan_hits"] == 5


def test_witness_a_needle_across_the_chunk_boundary(board, docs):
    data = board.paths["s04-boundary"].read_bytes()
    at = data.index(LONG_NEEDLE.encode())
    assert at < field_report.CHUNK < at + len(LONG_NEEDLE)
    assert len(LONG_NEEDLE) > len("ruff")
    assert docs["longtool"]["scan"]["prescan_hits"] == 2
    assert "s04-boundary" in sessions_of(docs["longtool"])


def test_witness_entries_with_no_byte_needles(docs):
    for name, why in (("noprefix", "no literal prefix"), ("pyglob", "matches `python`")):
        sc = docs[name]["scan"]
        assert sc["prescan_hits"] == sc["parsed"] == sc["in_time_window"], name
        assert any(why in f for f in docs[name]["findings"]), name


def test_witness_an_opportunity_only_hit(docs):
    m, sc = docs["fetchonly"]["metrics"], docs["fetchonly"]["scan"]
    assert m["invocations"] == 0
    assert m["opportunity_sessions"] == 1
    assert sc["prescan_hits"] == 1


# ---------------------------------------------------------------------- one pass


def test_each_transcript_is_read_once_per_window(board, tmp_path, monkeypatch):
    """The point of the change: the reads no longer scale with the number of entries.

    Per window, the census reads a main transcript's head and tail, the prescan reads its
    bytes once, and the line read opens it as text once. Two windows here, six entries.
    """
    projects = board.radar.home / "projects"
    text_opens: dict[Path, int] = {}
    byte_opens: dict[Path, int] = {}
    real_open = Path.open

    def counting_open(self, mode="r", *args, **kwargs):
        if self.is_relative_to(projects):
            seen = byte_opens if "b" in mode else text_opens
            seen[self] = seen.get(self, 0) + 1
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    windows = 2
    p = board.radar.call(
        field_report.main,
        "--all",
        "--reconcile",
        "--until",
        UNTIL,
        "--out-dir",
        str(tmp_path / "all"),
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert text_opens, "no transcript was read as text"
    assert max(text_opens.values()) <= windows, text_opens
    assert max(byte_opens.values()) <= 3 * windows, byte_opens


def test_the_console_lines_keep_the_entry_order(radar, tmp_path):
    """A FAIL printed while an entry is prepared stays between its neighbours' lines."""
    radar.tool("aaa", entry("aaa", EARLY, 'match_command = ["ruff"]'))
    radar.tool("bbb", entry("bbb", None, 'match_command = ["ruff"]'))
    radar.tool("ccc", entry("ccc", EARLY, 'match_command = ["just"]'))
    radar.session(
        "s1",
        [note("2026-08-05T09:00:00.000Z"), bash("t1", "2026-08-05T10:00:00.000Z", "ruff check .")],
        mtime=epoch("2026-08-05"),
    )
    p = radar.run("--all", "--until", UNTIL, "--out-dir", str(tmp_path / "out"))
    assert p.returncode == 1, p.stdout + p.stderr
    lines = p.stdout.splitlines()
    first = max(
        i for i, ln in enumerate(lines) if ln.startswith(("[NOTE] aaa", "[WARN] aaa", "[FLAG] aaa"))
    )
    fail = lines.index("[FAIL] bbb: no --since and no [telemetry].since")
    last = min(
        i for i, ln in enumerate(lines) if ln.startswith(("[NOTE] ccc", "[WARN] ccc", "[FLAG] ccc"))
    )
    assert first < fail < last, p.stdout
