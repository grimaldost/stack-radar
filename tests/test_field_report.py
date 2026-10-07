"""Behavioural tests for the field-telemetry miner.

Each test is a defect reduced to the smallest synthetic transcript that reproduces
it. They run the script as a
subprocess against a miniature radar built in tmp_path (see conftest.py), so they
exercise the whole path - argument parsing, scan, census, findings - rather than an
internal function that a refactor could route around.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from conftest import bash, note, tool_result, tool_use


def epoch(day: str) -> float:
    """Midday UTC on `day`, as an mtime."""
    d = dt.date.fromisoformat(day)
    return dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.UTC).timestamp()


WINDOW = ("--since", "2026-08-01", "--until", "2026-08-31")


def entry(name: str, *, ring: str = "adopt", body: str = "") -> str:
    return f'name = "{name}"\nring = "{ring}"\nartifact = "cli"\n{body}'


def cli_entry(name: str, globs: str, *, ring: str = "adopt", extra: str = "") -> str:
    return entry(
        name,
        ring=ring,
        body=f'\n[telemetry]\nmatch_command = [{globs}]\nsince = "2026-08-01"\n{extra}',
    )


class TestCommandMatching:
    def test_heredoc_body_is_not_an_invocation(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "git commit -F - <<'MSG'\nran ruff\nMSG"),
                bash("t2", "2026-08-05T10:01:00.000Z", "cat <<EOF > note.txt\nruff check\nEOF"),
            ],
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_quoted_python_body_is_not_an_invocation(self, radar):
        # A variable named after a tool at the head of a line inside `python -c` is
        # a command line to a newline-splitting parser, which then counts invocations
        # that never happened.
        radar.tool("ty", cli_entry("ty", '"ty"'))
        radar.session(
            "s1",
            [
                bash(
                    "t1",
                    "2026-08-05T10:00:00.000Z",
                    "python -c \"\nimport json\nty = json.load(open('x'))\nprint(ty)\n\"",
                ),
            ],
        )
        doc = radar.report("ty", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_multiline_quoted_argument_is_not_an_invocation(self, radar):
        # A commit message body is the commonest multi-line quoted argument there is.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash(
                    "t1",
                    "2026-08-05T10:00:00.000Z",
                    'git commit -m "style: reformat\n\nruff format across the tree"',
                ),
            ],
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_runner_prefix_still_resolves_the_delegated_tool(self, radar):
        # A prefix in front of the runner (`time`, `wsl`, `xargs -n1`) hides the whole
        # chain from a reading of each segment's head, which undercounts.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "time uv run ruff check ."),
                tool_result("t1", "2026-08-05T10:00:01.000Z"),
            ],
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 1
        assert doc["metrics"]["per_tool"][0]["tool"] == "cmd:ruff"

    def test_module_invocation_counts_the_module(self, radar):
        radar.tool("pytest", cli_entry("pytest", '"pytest"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pytest tests/ -q")],
        )
        doc = radar.report("pytest", *WINDOW)
        assert doc["metrics"]["invocations"] == 1

    def test_environment_assignment_is_not_the_executable(self, radar):
        # The assignment's value is a path fragment, not a command. Quote a Windows
        # path with a space in it and a whitespace-splitting parser reads the tail of
        # the path as the executable - here `Files/ruff"` becomes a ruff invocation.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash(
                    "t1",
                    "2026-08-05T10:00:00.000Z",
                    'F="C:/Program Files/ruff" git -C "$F" status',
                )
            ],
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_a_module_invocation_matches_the_distribution_name(self, radar):
        # cmdscan resolves `python -m X` to the MODULE name, and a hyphenated
        # distribution is imported with an underscore. Where an OS blocks the unsigned
        # console-script shim, the module form is the only way the tool runs, and an
        # entry matching only `pre-commit` would count a fraction of the real calls.
        # Note the transcript never contains the hyphenated spelling, so this also
        # pins the byte prescan: with only `pre-commit` as a needle the file is never
        # opened and the miss is invisible rather than merely wrong.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pre_commit run -a")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("pre-commit", *WINDOW)
        assert doc["metrics"]["invocations"] == 1
        assert doc["metrics"]["per_tool"][0]["tool"] == "cmd:pre_commit"

    def test_the_report_states_the_globs_it_actually_matched_on(self, radar):
        # An expansion the reader cannot see is an undeclared matcher. The document
        # carries the effective set, which is also what --reconcile counts the
        # second invocation table under.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"'))
        radar.session("s1", [note("2026-08-05T10:00:00.000Z")], mtime=epoch("2026-08-05"))
        doc = radar.report("pre-commit", *WINDOW)
        assert doc["match_command"] == ["pre-commit", "pre_commit"]

    def test_a_pattern_glob_is_left_alone(self, radar):
        # Only a LITERAL glob gets its module spelling. In a pattern the hyphen may be
        # structural - inside a character class it is a range, and `[a-c]` respelled is
        # the set {a, _, c} - so the rewrite would change what the author wrote. An
        # entry that needs both spellings under a pattern declares both.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit*"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pre_commit run -a")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("pre-commit", *WINDOW)
        assert doc["metrics"]["invocations"] == 0
        assert doc["match_command"] == ["pre-commit*"]

    def test_a_command_override_is_measured_literally(self, radar):
        # The expansion exists for DECLARED globs: an entry is written under the name the
        # tool installs and publishes under, and Python packaging turns that hyphen into
        # an underscore on import. An override is a probe - somebody typed a glob to find
        # out what it matches - so silently measuring a second glob they did not type
        # answers a different question from the one asked, which is what a probe is for.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pre_commit run -a")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("pre-commit", "--match-command", "pre-commit", *WINDOW)
        assert doc["match_command"] == ["pre-commit"]
        assert doc["metrics"]["invocations"] == 0

    def test_the_declared_glob_is_still_expanded(self, radar):
        # The boundary the change must not cross: same entry, same transcript, no override.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pre_commit run -a")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("pre-commit", *WINDOW)
        assert doc["match_command"] == ["pre-commit", "pre_commit"]
        assert doc["metrics"]["invocations"] == 1

    def test_the_help_describes_the_resolved_chain(self, radar):
        # The help describes the resolved chain the matcher uses, not only the executable
        # at the head of a command, so what the flag promises is what the code counts.
        out = radar.run("--help").stdout
        assert "RESOLVED CHAIN" in out
        assert "EXECUTABLE at the head" not in out
        assert "measured LITERALLY" in out

    def test_a_python_glob_counts_chains_that_never_spell_python(self, radar):
        # `uv run x.py` resolves through python without the word appearing in the
        # command line, so a byte prescan on "python" would skip this transcript.
        radar.tool("interp", cli_entry("interp", '"python"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run x.py")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("interp", *WINDOW)
        assert doc["metrics"]["invocations"] == 1
        assert any("byte prescan is disabled" in f for f in doc["findings"]), doc["findings"]

    def test_windows_executable_suffix_is_normalised(self, radar):
        # `.venv/Scripts/ruff.exe` is a ruff invocation; a parser that keeps the
        # suffix compares `ruff.exe` against the glob `ruff` and silently drops it.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "./.venv/Scripts/ruff.exe check .")],
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 1


class TestCensus:
    def test_session_is_placed_by_its_own_records_not_its_mtime(self, radar):
        # A client can rewrite transcript mtimes: opening or displaying a transcript can
        # move its mtime to around the present. Under an mtime rule an old session touched
        # that way would enter a later window's denominator.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "inside",
            [
                note("2026-08-10T09:00:00.000Z"),
                bash("t1", "2026-08-10T10:00:00.000Z", "uv run ruff check ."),
                note("2026-08-10T11:00:00.000Z"),
            ],
            mtime=epoch("2026-08-10"),
        )
        radar.session(
            "rewritten-old",
            [note("2026-06-02T09:00:00.000Z"), note("2026-06-02T10:00:00.000Z")],
            mtime=epoch("2026-08-20"),  # touched inside the window, written long before
        )
        doc = radar.report("ruff", *WINDOW)
        m = doc["metrics"]
        assert m["sessions_in_window"] == 1
        assert m["transcripts_excluded_by_timestamp"] == 1

    def test_the_exclusion_count_is_named_for_the_files_it_counts(self, radar):
        # It counts FILES - the loop runs once per transcript - so it is not named
        # `sessions_excluded_by_timestamp`. A session with a second, in-window transcript
        # is not excluded from the census by one of its files being, so a file count named
        # after sessions invites subtracting it from the denominator.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "rewritten-old",
            [note("2026-06-02T09:00:00.000Z"), note("2026-06-02T10:00:00.000Z")],
            mtime=epoch("2026-08-20"),
        )
        m = radar.report("ruff", *WINDOW)["metrics"]
        assert "sessions_excluded_by_timestamp" not in m
        assert m["transcripts_excluded_by_timestamp"] == 1

    def test_the_markdown_surfaces_the_transcripts_still_dated_by_mtime(self, radar):
        # The census falls back to the mtime rule for a transcript with no parseable
        # timestamp at either end. The Markdown says how many, so a census resting partly
        # on the mtime rule does not read as one resting entirely on record timestamps.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        (radar.home / "projects" / "C--Users-x-Documents-p").mkdir(parents=True, exist_ok=True)
        undated = radar.home / "projects" / "C--Users-x-Documents-p" / "no-clock.jsonl"
        undated.write_text('{"type":"user","message":{"role":"user","content":[]}}\n')
        import os

        os.utime(undated, (epoch("2026-08-10"), epoch("2026-08-10")))
        radar.run("ruff", *WINDOW)
        md = (radar.root / "field" / "ruff" / "2026-08-01_2026-08-31.md").read_text(
            encoding="utf-8"
        )
        assert "main transcripts still dated by mtime" in md
        assert "| 1 |" in md
        assert "transcripts excluded by the timestamp rule" in md

    def test_a_session_straddling_the_start_still_counts(self, radar):
        # Overlap, not containment: a session that began before `since` and ran into
        # the window had opportunity inside it. The mtime here is AFTER the window,
        # which is the same rewrite hazard pointing the other way - under the mtime
        # rule such a session would drop out of the denominator instead of into it.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "straddler",
            [note("2026-07-30T09:00:00.000Z"), note("2026-08-02T10:00:00.000Z")],
            mtime=epoch("2026-09-02"),
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["sessions_in_window"] == 1
        assert doc["metrics"]["transcripts_excluded_by_timestamp"] == 0


class TestDenominator:
    """One census for every row, or a --all table's column is several censuses.

    A denominator grown with the sessions an entry's own invocations touched would take in
    ids from sidechain transcripts (subagents, workflows) whose parent session's own
    transcript is absent or out of window - real use, but not a session of its own. An
    entry that runs work in subagents would then divide by a bigger number than the entry
    beside it in the same table.
    """

    def _board(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.tool("widget", cli_entry("widget", '"widget"'))
        # Two ordinary sessions, in the census.
        for i in range(2):
            radar.session(
                f"plain{i}",
                [note("2026-08-06T09:00:00.000Z"), note("2026-08-06T10:00:00.000Z")],
                mtime=epoch("2026-08-06"),
            )
        # A sidechain whose parent has no main transcript at all: `widget` is reached only
        # from inside it, the shape that would inflate one row under an entry-dependent census.
        radar.session(
            "orphan-parent",
            [bash("t9", "2026-08-05T10:00:00.000Z", "uvx widget check")],
            sub="subagents/a.jsonl",
            mtime=epoch("2026-08-05"),
        )

    def test_a_sidechain_only_session_does_not_enter_the_denominator(self, radar):
        self._board(radar)
        m = radar.report("widget", *WINDOW)["metrics"]
        assert m["sessions_in_window"] == 2
        assert m["sessions_with_use"] == 1
        assert m["sessions_with_use_outside_census"] == 1

    def test_every_row_of_all_divides_by_the_same_census(self, radar):
        self._board(radar)
        p = radar.run("--all", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        rows = [ln.split() for ln in p.stdout.splitlines() if ln.startswith(("ruff ", "widget "))]
        denominators = {r[3].split("/")[1] for r in rows}
        assert denominators == {"2"}, p.stdout

    def test_the_finding_says_where_the_extra_sessions_came_from(self, radar):
        self._board(radar)
        doc = radar.report("widget", *WINDOW)
        hit = [f for f in doc["findings"] if "not in the window census" in f]
        assert len(hit) == 1 and hit[0].startswith("[NOTE]")
        assert "sidechain" in hit[0]


class TestRetention:
    def test_replayed_history_pulls_the_horizon_back(self, radar):
        # The horizon is the EARLIEST record, not the oldest first record: a resumed
        # session replays the session it continues, and the replayed rows keep their
        # original, earlier timestamps while sitting after the resume header. So the
        # earliest record in a file can predate its first one, and a horizon read from
        # first records comes out too late.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "resumed",
            [
                note("2026-08-14T09:00:00.000Z", "resumed session header"),
                note("2026-08-02T08:00:00.000Z", "replayed from the earlier session"),
                note("2026-08-14T10:00:00.000Z"),
            ],
            mtime=epoch("2026-08-14"),
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["scan"]["oldest_record_by_head_scan"] == "2026-08-02"
        # And the census is unmoved: the session's own clock still starts 2026-08-14, so
        # a replayed timestamp cannot drag a session into an older window's denominator.
        assert doc["metrics"]["sessions_in_window"] == 1

    def test_the_warning_says_the_date_is_a_ceiling(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [note("2026-08-14T09:00:00.000Z"), note("2026-08-14T10:00:00.000Z")],
            mtime=epoch("2026-08-14"),
        )
        doc = radar.report("ruff", *WINDOW)
        warn = next(f for f in doc["findings"] if "truncated by retention" in f)
        assert "BOUNDED HEAD SCAN" in warn
        assert "CEILING" in warn
        assert "64 KiB" in warn

    def test_warns_when_the_oldest_transcript_starts_after_since(self, radar):
        # Transcripts are pruned. A window that reaches back further than the oldest
        # surviving file produces a truncated census that looks like a complete one.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [note("2026-08-14T09:00:00.000Z"), note("2026-08-14T10:00:00.000Z")],
            mtime=epoch("2026-08-14"),
        )
        doc = radar.report("ruff", *WINDOW)
        warn = [f for f in doc["findings"] if "truncated by retention" in f]
        assert warn, doc["findings"]
        assert "2026-08-14" in warn[0]
        assert warn[0].startswith("[WARN]")

    def test_no_warning_when_the_corpus_reaches_the_window_start(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [note("2026-07-20T09:00:00.000Z"), note("2026-08-14T10:00:00.000Z")],
            mtime=epoch("2026-08-14"),
        )
        doc = radar.report("ruff", *WINDOW)
        assert not [f for f in doc["findings"] if "truncated by retention" in f]


class TestReplayedHistory:
    def test_a_resumed_session_does_not_double_count_the_replayed_calls(self, radar):
        # Resuming or forking a session writes a NEW transcript that replays the
        # earlier one verbatim, tool_use ids included. Counting both copies counts one
        # event twice.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        replayed = [
            note("2026-08-05T09:59:00.000Z"),
            bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check ."),
            tool_result("t1", "2026-08-05T10:00:01.000Z"),
        ]
        radar.session("a-original", replayed, mtime=epoch("2026-08-05"))
        radar.session(
            "b-resumed",
            [
                *replayed,
                bash("t2", "2026-08-06T10:00:00.000Z", "uv run ruff format ."),
                tool_result("t2", "2026-08-06T10:00:01.000Z"),
            ],
            mtime=epoch("2026-08-06"),
        )
        m = radar.report("ruff", *WINDOW)["metrics"]
        assert m["invocations"] == 2
        assert m["replayed_dropped"] == 1

    def test_the_oldest_file_keeps_the_call(self, radar):
        # Which copy survives matters for the per-session attribution: the original
        # session is the one that made the call.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        replayed = [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")]
        radar.session("a-original", replayed, mtime=epoch("2026-08-05"))
        radar.session("b-resumed", list(replayed), mtime=epoch("2026-08-06"))
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 1
        assert [i["session"] for i in doc["invocations"]] == ["a-original"]


PILOT_EXIT = """
[pilot_exit]
review_after_days = 30
adopt_if = "used somewhere"
decline_if = "used nowhere"
min_sessions_with_use = 5
"""


class TestPilotExitGating:
    def _entry(self, ring: str) -> str:
        return (
            cli_entry("widget", '"widget"', ring=ring)
            + PILOT_EXIT
            + '\n[[history]]\ndate = "2026-07-01"\nring = "pilot"\nreason = "r"\nevidence = "e"\n'
            + (
                '\n[[history]]\ndate = "2026-08-15"\nring = "observe"\nreason = "r"\n'
                if ring != "pilot"
                else ""
            )
        )

    def test_criteria_are_not_evaluated_once_the_pilot_has_ended(self, radar):
        # A demoted entry keeps its [pilot_exit] on purpose, as the record of what was
        # promised. Reporting "pilot review is due" for an experiment that ended is a
        # finding no action can follow.
        radar.tool("widget", self._entry("observe"))
        radar.session("s1", [note("2026-08-10T09:00:00.000Z")], mtime=epoch("2026-08-10"))
        doc = radar.report("widget", *WINDOW)
        assert not [f for f in doc["findings"] if "pilot review is due" in f]
        assert not [f for f in doc["findings"] if "min_sessions_with_use" in f]
        assert doc["checks"] == []
        assert doc["pilot_exit_evaluated"] is False
        retained = [f for f in doc["findings"] if "retained from an earlier pilot" in f]
        assert len(retained) == 1
        assert retained[0].startswith("[NOTE]")

    def test_criteria_are_evaluated_while_the_entry_is_in_pilot(self, radar):
        radar.tool("widget", self._entry("pilot"))
        radar.session("s1", [note("2026-08-10T09:00:00.000Z")], mtime=epoch("2026-08-10"))
        doc = radar.report("widget", *WINDOW)
        assert doc["pilot_exit_evaluated"] is True
        assert [c["check"] for c in doc["checks"]] == [
            "review_after_days",
            "min_sessions_with_use",
        ]

    def test_only_a_pilot_is_warned_for_missing_exit_criteria(self, radar):
        # The gate asks for [pilot_exit] at ring pilot and nowhere else. Field warned on
        # every own/adopt entry as well, so the two verbs disagreed and a --all run
        # printed a line per entry that no action could follow.
        for ring in ("own", "adopt", "observe"):
            radar.tool(f"w-{ring}", cli_entry(f"w-{ring}", '"widget"', ring=ring))
        radar.tool("w-pilot", cli_entry("w-pilot", '"widget"', ring="pilot"))
        radar.session("s1", [note("2026-08-10T09:00:00.000Z")], mtime=epoch("2026-08-10"))
        p = radar.run("--all", "--include-inactive", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        warned = re.findall(r"\[WARN\] (\S+): no \[pilot_exit\] block", p.stdout)
        assert warned == ["w-pilot"], p.stdout

    def test_the_markdown_does_not_ask_a_non_pilot_for_criteria(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"', ring="adopt"))
        radar.session("s1", [note("2026-08-10T09:00:00.000Z")], mtime=epoch("2026-08-10"))
        radar.run("ruff", *WINDOW)
        md = (radar.root / "field" / "ruff" / "2026-08-01_2026-08-31.md").read_text(
            encoding="utf-8"
        )
        assert "Not a pilot" in md
        assert "criteria written after the data are not criteria" not in md

    def test_the_clock_runs_from_the_last_pilot_entry(self, radar):
        # An entry that piloted, was parked and was re-piloted is days into the
        # CURRENT experiment, not into the one it started a year ago.
        toml = (
            cli_entry("widget", '"widget"', ring="pilot")
            + PILOT_EXIT
            + '\n[[history]]\ndate = "2026-01-01"\nring = "pilot"\nreason = "r"\nevidence = "e"\n'
            + '\n[[history]]\ndate = "2026-03-01"\nring = "observe"\nreason = "r"\n'
            + '\n[[history]]\ndate = "2026-09-01"\nring = "pilot"\nreason = "r"\nevidence = "e"\n'
        )
        radar.tool("widget", toml)
        radar.session("s1", [note("2026-08-10T09:00:00.000Z")], mtime=epoch("2026-08-10"))
        doc = radar.report("widget", *WINDOW)
        rad = next(c for c in doc["checks"] if c["check"] == "review_after_days")
        assert "since 2026-09-01" in rad["detail"]


class TestAlsoRunsIn:
    def _thin(self, radar, extra: str) -> dict:
        # One session that used the tool, twenty-five that did not: 1/26 = 3.8%, under
        # the 5% threshold that fires the thin-surface finding.
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"', extra=extra))
        radar.session(
            "used",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run pre-commit run --all-files")],
            mtime=epoch("2026-08-05"),
        )
        for i in range(25):
            radar.session(
                f"quiet{i:02d}",
                [note("2026-08-06T09:00:00.000Z"), note("2026-08-06T10:00:00.000Z")],
                mtime=epoch("2026-08-06"),
            )
        return radar.report("pre-commit", *WINDOW)

    def test_thin_surface_is_a_flag_when_nothing_else_is_declared(self, radar):
        doc = self._thin(radar, "")
        assert [f for f in doc["findings"] if f.startswith("[FLAG] pre-commit: thin")]

    def test_declared_invisible_surfaces_downgrade_the_flag_to_a_note(self, radar):
        # A hook that fires on every commit still reads as a handful of sessions,
        # because only the agent's own calls are recorded. The count is right; "an
        # effect this rare cannot pay for its context cost" is not a conclusion it
        # supports.
        doc = self._thin(radar, 'also_runs_in = ["pre-commit hook"]\n')
        assert not [f for f in doc["findings"] if "thin opportunity surface" in f]
        hits = [f for f in doc["findings"] if "pre-commit hook" in f]
        assert len(hits) == 1
        assert hits[0].startswith("[NOTE]")
        assert "reach-frequency" in hits[0]


MCP_ENTRY = """\
name = "docs-server"
ring = "adopt"
artifact = "mcp-server"

[telemetry]
match = ["mcp__*__search-docs"]
since = "2026-08-01"
{extra}
"""


class TestOpportunity:
    """Zero use has three readings, and use alone separates none of them.

    Zero use has three readings - the work never called for it, the tool was not
    mounted, or it was present and applicable and never reached for - so a zero is never
    on its own an argument for moving a ring down. The opportunity matchers supply the
    denominator that use never had.
    """

    def _radar(self, radar, extra: str = "") -> None:
        radar.tool("docs-server", MCP_ENTRY.format(extra=extra))

    OPP = (
        'opportunity_match = ["WebFetch", "WebSearch"]\n'
        '[[history]]\ndate = "2026-08-01"\nring = "adopt"\nreason = "r"\nevidence = "e"\n'
    )
    NO_OPP = '[[history]]\ndate = "2026-08-01"\nring = "adopt"\nreason = "r"\nevidence = "e"\n'

    def test_an_undeclared_opportunity_reports_none_not_zero(self, radar):
        # "0 sessions had an opportunity" is a measurement; "nobody said what an
        # opportunity looks like" is not, and one reading as the other is how a zero
        # comes to look like evidence.
        self._radar(radar, self.NO_OPP)
        radar.session("s1", [note("2026-08-05T09:00:00.000Z")], mtime=epoch("2026-08-05"))
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["opportunity_sessions"] is None
        assert m["sessions_with_use_among_opportunity"] is None
        assert m["taken_ratio"] is None

    def test_a_session_that_had_the_chance_and_took_it(self, radar):
        self._radar(radar, self.OPP)
        radar.session(
            "took",
            [
                tool_use("t1", "WebFetch", "2026-08-05T10:00:00.000Z", url="https://x"),
                tool_use("t2", "mcp__abc__search-docs", "2026-08-05T10:01:00.000Z", q="x"),
            ],
            mtime=epoch("2026-08-05"),
        )
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["opportunity_sessions"] == 1
        assert m["sessions_with_use_among_opportunity"] == 1
        assert m["taken_ratio"] == 1.0

    def test_the_ratio_counts_sessions_that_had_the_chance_and_did_not(self, radar):
        self._radar(radar, self.OPP)
        radar.session(
            "took",
            [
                tool_use("t1", "WebSearch", "2026-08-05T10:00:00.000Z", query="x"),
                tool_use("t2", "mcp__abc__search-docs", "2026-08-05T10:01:00.000Z", q="x"),
            ],
            mtime=epoch("2026-08-05"),
        )
        for i in range(3):
            radar.session(
                f"missed{i}",
                [tool_use(f"w{i}", "WebFetch", "2026-08-06T10:00:00.000Z", url="https://y")],
                mtime=epoch("2026-08-06"),
            )
        # A session with neither: it is in the census denominator and must NOT be in the
        # opportunity one. That difference is the whole point of the number.
        radar.session("unrelated", [note("2026-08-07T10:00:00.000Z")], mtime=epoch("2026-08-07"))
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["sessions_in_window"] == 5
        assert m["opportunity_sessions"] == 4
        assert m["sessions_with_use_among_opportunity"] == 1
        assert m["taken_ratio"] == 0.25

    def test_the_opportunity_tools_actually_hit_are_listed(self, radar):
        # A matcher that never fires and an occasion that never arose produce the same
        # zero. Naming what did fire is what tells them apart.
        self._radar(radar, self.OPP)
        radar.session(
            "s1",
            [
                tool_use("t1", "WebFetch", "2026-08-05T10:00:00.000Z", url="https://x"),
                tool_use("t2", "WebFetch", "2026-08-05T10:01:00.000Z", url="https://y"),
                tool_use("t3", "WebSearch", "2026-08-05T10:02:00.000Z", query="z"),
            ],
            mtime=epoch("2026-08-05"),
        )
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["opportunity_tools"] == [
            {"tool": "WebFetch", "invocations": 2, "sessions": 1},
            {"tool": "WebSearch", "invocations": 1, "sessions": 1},
        ]

    def test_a_replayed_opportunity_is_counted_once(self, radar):
        # Same dedup rule as use: a resumed session replays the session it continues,
        # tool_use ids included, and counting both copies counts one chance twice.
        self._radar(radar, self.OPP)
        replayed = [tool_use("t1", "WebFetch", "2026-08-05T10:00:00.000Z", url="https://x")]
        radar.session("a-original", replayed, mtime=epoch("2026-08-05"))
        radar.session("b-resumed", list(replayed), mtime=epoch("2026-08-06"))
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["opportunity_invocations"] == 1
        assert m["opportunity_sessions"] == 1

    def test_the_prescan_does_not_hide_an_opportunity_only_session(self, radar):
        # The same prescan trap as the pre-commit module form above: a transcript
        # containing only the OPPORTUNITY names holds none of the use needles, so a
        # prescan filtered on the use matchers alone never opens it - and the missing
        # sessions are exactly the population the ratio is about. Invisible rather than
        # merely wrong.
        self._radar(radar, self.OPP)
        radar.session(
            "opportunity-only",
            [tool_use("t1", "WebFetch", "2026-08-05T10:00:00.000Z", url="https://x")],
            mtime=epoch("2026-08-05"),
        )
        m = radar.report("docs-server", *WINDOW)["metrics"]
        assert m["opportunity_sessions"] == 1
        assert m["invocations"] == 0

    def test_the_zero_use_note_names_the_three_readings(self, radar):
        self._radar(radar, self.NO_OPP)
        radar.session("s1", [note("2026-08-05T09:00:00.000Z")], mtime=epoch("2026-08-05"))
        doc = radar.report("docs-server", *WINDOW)
        zero = [f for f in doc["findings"] if "zero measured use" in f]
        assert len(zero) == 1
        for reading in (
            "the work never called for it",
            "not mounted or reachable",
            "was never reached for",
        ):
            assert reading in zero[0], zero[0]

    def test_the_zero_use_note_reports_the_opportunity_split(self, radar):
        self._radar(radar, self.OPP)
        for i in range(2):
            radar.session(
                f"missed{i}",
                [tool_use(f"w{i}", "WebFetch", "2026-08-06T10:00:00.000Z", url="https://y")],
                mtime=epoch("2026-08-06"),
            )
        doc = radar.report("docs-server", *WINDOW)
        zero = next(f for f in doc["findings"] if "zero measured use" in f)
        assert "2 session(s) had an opportunity" in zero
        assert "0 took it" in zero

    def test_also_runs_in_reaches_the_zero_use_note(self, radar):
        # It was consulted only in the thin-surface branch, so an entry that runs
        # ENTIRELY where no transcript can see it - the strongest case for the
        # qualification - got the unqualified finding.
        radar.tool(
            "pre-commit",
            cli_entry("pre-commit", '"pre-commit"', extra='also_runs_in = ["pre-commit hook"]\n'),
        )
        radar.session("s1", [note("2026-08-05T09:00:00.000Z")], mtime=epoch("2026-08-05"))
        doc = radar.report("pre-commit", *WINDOW)
        zero = next(f for f in doc["findings"] if "zero measured use" in f)
        assert "pre-commit hook" in zero
        assert "reach, not use" in zero

    def test_the_markdown_carries_the_section(self, radar):
        self._radar(radar, self.OPP)
        radar.session(
            "s1",
            [tool_use("t1", "WebFetch", "2026-08-05T10:00:00.000Z", url="https://x")],
            mtime=epoch("2026-08-05"),
        )
        radar.run("docs-server", *WINDOW)
        md = (radar.root / "field" / "docs-server" / "2026-08-01_2026-08-31.md").read_text(
            encoding="utf-8"
        )
        assert "## Opportunity vs use (salience)" in md
        assert "| sessions with an opportunity | 1 |" in md
        assert "`WebFetch`" in md


def index_row(day: str, *, tool: str = "Bash", exe=(), skill=None, first_seen: bool = True) -> dict:
    """One row of an invocation table (invocation table format, version 1).

    Only the fields the engine reads - `timestamp`, `tool`, `first_seen`, and `exe`/`skill`
    when present. A table may carry others; they are not written here, because a fixture
    that carried fields nothing reads would assert nothing about them."""
    row = {
        "timestamp": f"{day}T10:00:00.000Z",
        "tool": tool,
        "first_seen": first_seen,
    }
    if exe:
        row["exe"] = list(exe)
    if skill:
        row["skill"] = skill
    return row


class TestReconcile:
    def _setup(self, radar, rows, *, generated_at="2026-09-01T00:00:00Z"):
        radar.write_environment(with_index=True)
        radar.index_rows(rows, generated_at=generated_at)
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))

    def test_agreement_is_reported_per_matcher_kind(self, radar):
        self._setup(radar, [index_row("2026-08-05", exe=["uv", "ruff"])])
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        rec = doc["reconcile"]
        assert rec["generated_at"] == "2026-09-01T00:00:00Z"
        assert rec["age_days"] is not None
        assert rec["kinds"] == [
            {
                "kind": "command",
                "ours": 1,
                "index": 1,
                "delta": 0,
                "relative": 0.0,
                "comparable": True,
            }
        ]
        assert not [f for f in doc["findings"] if "reconcile drift" in f]

    def test_a_table_that_names_its_format_version_is_read_the_same(self, radar):
        # docs/concepts.md lets derive.json carry `"format": "invocation-table/1"`; the
        # marker is accepted and changes nothing about how the table is read.
        self._setup(radar, [index_row("2026-08-05", exe=["uv", "ruff"])])
        meta = radar.transcript_index / "derived" / "derive.json"
        meta.write_text(
            json.dumps({"format": "invocation-table/1", "generated_at": "2026-09-01T00:00:00Z"}),
            encoding="utf-8",
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        rec = radar.report("ruff", "--reconcile", *WINDOW)["reconcile"]
        assert rec["generated_at"] == "2026-09-01T00:00:00Z"
        assert [(k["kind"], k["ours"], k["index"]) for k in rec["kinds"]] == [("command", 1, 1)]

    def test_disagreement_above_the_tolerance_warns(self, radar):
        # Ten rows in the index, one invocation on disk: the two parsers cannot both
        # be right, and the point of the flag is that neither notices alone.
        self._setup(radar, [index_row("2026-08-05", exe=["ruff"]) for _ in range(10)])
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert doc["reconcile"]["kinds"][0] == {
            "kind": "command",
            "ours": 1,
            "index": 10,
            "delta": -9,
            "relative": 0.9,
            "comparable": True,
        }
        warns = [f for f in doc["findings"] if "reconcile drift on `command`" in f]
        assert len(warns) == 1 and warns[0].startswith("[WARN]")
        assert "2026-09-01" in warns[0]

    def test_replayed_rows_in_the_index_are_not_counted(self, radar):
        # The index marks replayed copies. Counting them would compare our
        # de-duplicated total against its raw one and report drift that is not drift.
        self._setup(
            radar,
            [
                index_row("2026-08-05", exe=["ruff"]),
                index_row("2026-08-05", exe=["ruff"], first_seen=False),
            ],
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert doc["reconcile"]["kinds"][0]["index"] == 1

    def test_only_declared_matcher_kinds_are_compared(self, radar):
        self._setup(
            radar,
            [
                index_row("2026-08-05", exe=["ruff"]),
                index_row("2026-08-05", tool="Skill", skill="widget:x"),
            ],
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert [k["kind"] for k in doc["reconcile"]["kinds"]] == ["command"]

    def test_the_index_is_counted_under_the_same_module_spelling(self, radar):
        # The index resolves the same chain, so a module-form call is `pre_commit`
        # in its table too. Counting its rows under the DECLARED glob while counting
        # ours under the expanded one would make the two scanners disagree by
        # construction, and a drift warning that says nothing about either parser is
        # the one kind of finding this cross-check cannot afford.
        radar.write_environment(with_index=True)
        radar.index_rows(
            [index_row("2026-08-05", exe=["uv", "python", "pre_commit"])],
            generated_at="2026-09-01T00:00:00Z",
        )
        radar.tool("pre-commit", cli_entry("pre-commit", '"pre-commit"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python -m pre_commit run -a")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("pre-commit", "--reconcile", *WINDOW)
        kind = doc["reconcile"]["kinds"][0]
        assert (kind["ours"], kind["index"]) == (1, 1)
        assert not [f for f in doc["findings"] if "reconcile drift" in f]

    def test_a_missing_store_is_a_note_not_a_failure(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session("s1", [note("2026-08-05T10:00:00.000Z")], mtime=epoch("2026-08-05"))
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert doc["reconcile"] is None
        assert [f for f in doc["findings"] if f.startswith("[NOTE]") and "transcript_index" in f]

    def test_the_index_is_found_by_transcript_index_alone(self, radar):
        # The key is `[paths].transcript_index`, and no other key is read as its spelling:
        # an index declared under any other name is not found.
        self._setup(radar, [index_row("2026-08-05", exe=["ruff"])])
        profile = radar.root / "environments" / "default.toml"
        profile.write_text(
            profile.read_text(encoding="utf-8").replace("transcript_index =", "index_path ="),
            encoding="utf-8",
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert doc["reconcile"] is None, doc["reconcile"]
        assert [f for f in doc["findings"] if "declares no [paths].transcript_index" in f], doc

    def test_calls_made_after_the_index_was_derived_are_not_drift(self, radar):
        # The index is a dated snapshot and the transcripts keep growing. Counting our
        # side over the whole window would report every call made since the derive as a
        # disagreement between the scanners, however recent the index was.
        self._setup(
            radar,
            [index_row("2026-08-05", exe=["ruff"])],
            generated_at="2026-08-05T12:00:00Z",
        )
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "ruff check ."),
                bash("t2", "2026-08-05T15:00:00.000Z", "ruff check ."),
                bash("t3", "2026-08-06T09:00:00.000Z", "ruff format ."),
            ],
            mtime=epoch("2026-08-06"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        rec = doc["reconcile"]
        assert (rec["kinds"][0]["ours"], rec["kinds"][0]["index"]) == (1, 1)
        assert rec["compared_up_to"] == "2026-08-05T12:00:00Z"
        assert not [f for f in doc["findings"] if "reconcile drift" in f]
        # The report's own totals are untouched: only the cross-check stops at the index.
        assert doc["metrics"]["invocations"] == 3

    def test_the_index_age_is_reported_in_hours(self, radar):
        # In days, an index derived hours ago read "0d ago" however much had run since.
        derived = dt.datetime.now(dt.UTC) - dt.timedelta(hours=10)
        stamp = derived.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._setup(
            radar, [index_row("2026-08-05", exe=["ruff"]) for _ in range(10)], generated_at=stamp
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("ruff", "--reconcile", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "0d ago" not in p.stdout
        assert re.search(r"derived 1\d\.\dh ago|derived 9\.9h ago", p.stdout), p.stdout
        warn = next(ln for ln in p.stdout.splitlines() if "reconcile drift" in ln)
        assert "h ago" in warn
        doc = json.loads(
            (radar.root / "field" / "ruff" / "2026-08-01_2026-08-31.json").read_text(
                encoding="utf-8"
            )
        )
        assert 9.9 <= doc["reconcile"]["age_hours"] <= 11.0

    def test_an_index_derived_before_the_window_says_so(self, radar):
        self._setup(radar, [], generated_at="2026-07-15T00:00:00Z")
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", "--reconcile", *WINDOW)
        assert [f for f in doc["findings"] if "before the window starts" in f]

    def test_reconcile_is_off_unless_asked_for(self, radar):
        self._setup(radar, [index_row("2026-08-05", exe=["ruff"]) for _ in range(10)])
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["reconcile"] is None
        assert not [f for f in doc["findings"] if "reconcile" in f]


class TestOutDir:
    """`field/` is tracked, so a verification run must be able to write somewhere else.

    Checking a number quoted in the docs means re-running the window the report was
    generated from - which, without this, rewrites the exact artefact the check compares
    against. The verification then confirms the file it just wrote.
    """

    def test_reports_go_where_they_are_asked_to(self, radar, tmp_path):
        elsewhere = tmp_path / "scratch-reports"
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("ruff", "--out-dir", str(elsewhere), *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        assert (elsewhere / "ruff" / "2026-08-01_2026-08-31.json").is_file()
        assert (elsewhere / "ruff" / "2026-08-01_2026-08-31.md").is_file()
        assert not (radar.root / "field").exists(), "the tracked directory was touched"

    def test_all_honours_it_too(self, radar, tmp_path):
        elsewhere = tmp_path / "scratch-board"
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.tool("widget", cli_entry("widget", '"widget"'))
        radar.session("s1", [note("2026-08-05T10:00:00.000Z")], mtime=epoch("2026-08-05"))
        radar.run("--all", "--out-dir", str(elsewhere), *WINDOW)
        assert (elsewhere / "ruff" / "2026-08-01_2026-08-31.md").is_file()
        assert (elsewhere / "widget" / "2026-08-01_2026-08-31.md").is_file()
        assert not (radar.root / "field").exists()

    # The report directory is named after the entry, so a name that is not a plain
    # identifier would choose where the report is written.
    # Two levels up from field/ is still inside tmp_path, so even the unfixed code keeps
    # its stray report where the assertion can see it.
    ESCAPING = "../../escaped-field"

    def _escaping_entry(self, radar) -> None:
        radar.tool("escaping", cli_entry(self.ESCAPING, '"ruff"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check .")],
            mtime=epoch("2026-08-05"),
        )

    def test_a_name_that_is_not_an_identifier_is_refused(self, radar, tmp_path):
        self._escaping_entry(radar)
        p = radar.run(self.ESCAPING, *WINDOW)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL]" in p.stdout and "not a valid entry name" in p.stdout
        assert not list(tmp_path.rglob("escaped-field")), "a report escaped"

    def test_all_refuses_it_and_writes_nothing_outside_the_out_dir(self, radar, tmp_path):
        elsewhere = tmp_path / "a" / "b" / "out"
        self._escaping_entry(radar)
        radar.tool("widget", cli_entry("widget", '"widget"'))
        p = radar.run("--all", "--out-dir", str(elsewhere), *WINDOW)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "not a valid entry name" in p.stdout
        assert (elsewhere / "widget" / "2026-08-01_2026-08-31.md").is_file()
        assert not list(tmp_path.rglob("escaped-field")), "a report escaped"


class TestAll:
    def _board(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.tool("widget", cli_entry("widget", '"widget"', ring="own"))
        radar.tool("just", cli_entry("just", '"just"', ring="observe"))
        radar.tool("docs-server", entry("docs-server", ring="observe"))  # no matcher at all
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check ."),
                tool_result("t1", "2026-08-05T10:00:01.000Z", is_error=True),
                bash("t2", "2026-08-05T10:02:00.000Z", "uvx widget check"),
                bash("t3", "2026-08-05T10:03:00.000Z", "just build"),
            ],
            mtime=epoch("2026-08-05"),
        )

    def test_runs_every_active_entry_with_a_matcher(self, radar):
        self._board(radar)
        p = radar.run("--all", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        table = p.stdout[p.stdout.index("tool") :]
        assert "widget" in table and "ruff" in table
        # observe is inactive by default; an entry with no matcher is never selected.
        assert "just" not in table
        assert "docs-server" not in table
        assert "2 entries with a matcher" in p.stdout

    def test_include_inactive_adds_the_demoted_entries(self, radar):
        self._board(radar)
        p = radar.run("--all", "--include-inactive", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "just" in p.stdout[p.stdout.index("tool") :]
        assert "3 entries with a matcher" in p.stdout

    def test_the_summary_carries_the_deciding_columns(self, radar):
        self._board(radar)
        p = radar.run("--all", *WINDOW)
        header = next(ln for ln in p.stdout.splitlines() if ln.startswith("tool"))
        assert header.split() == ["tool", "ring", "inv", "sessions", "err", "findings"]
        row = next(ln for ln in p.stdout.splitlines() if ln.startswith("ruff"))
        assert row.split()[:5] == ["ruff", "adopt", "1", "1/1", "1"]

    def test_each_entry_still_writes_its_own_report(self, radar):
        self._board(radar)
        radar.run("--all", *WINDOW)
        for name in ("ruff", "widget"):
            out = radar.root / "field" / name / "2026-08-01_2026-08-31.md"
            assert out.is_file(), f"{name} wrote no report"

    def test_json_with_all_is_refused_rather_than_ignored(self, radar):
        # Silently ignoring the flag hands a caller an empty parse instead of an error.
        self._board(radar)
        p = radar.run("--all", "--json", *WINDOW)
        assert p.returncode == 2
        assert "read field/<tool>/*.json" in p.stdout

    def test_naming_a_tool_and_all_together_is_refused(self, radar):
        self._board(radar)
        p = radar.run("ruff", "--all", *WINDOW)
        assert p.returncode == 2
        assert "not both, not neither" in p.stdout


class TestScriptIdentity:
    """A `cli` that ships as a python script has no name but its filename."""

    def test_a_script_run_through_the_interpreter_counts(self, radar):
        radar.tool("widget", cli_entry("widget", '"tool.py"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python tool.py run")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", *WINDOW)
        assert doc["metrics"]["invocations"] == 1
        assert doc["metrics"]["per_tool"][0]["tool"] == "cmd:tool.py"

    def test_a_script_named_as_an_argument_to_another_tool_does_not(self, radar):
        # The substring trap, in its last hiding place: naming the file is not running
        # it. Every one of these resolves to ruff / git / cat, not to python.
        radar.tool("widget", cli_entry("widget", '"tool.py"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "ruff check tool.py"),
                bash("t2", "2026-08-05T10:01:00.000Z", "git add tool.py"),
                bash("t3", "2026-08-05T10:02:00.000Z", "cat tool.py | head -20"),
            ],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_a_different_script_with_a_similar_name_does_not_match(self, radar):
        radar.tool("widget", cli_entry("widget", '"tool.py"'))
        radar.session(
            "s1",
            [
                bash(
                    "t1",
                    "2026-08-05T10:00:00.000Z",
                    "uv run python scripts/make_dev_tool.py --dry-run",
                )
            ],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", *WINDOW)
        assert doc["metrics"]["invocations"] == 0

    def test_executable_matchers_are_unaffected(self, radar):
        # The addition must be inert for every existing entry: a script identity always
        # carries its `.py`, so a bare executable glob can never collide with one.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "uv run ruff check ."),
                bash("t2", "2026-08-05T10:01:00.000Z", "uv run python ruff.py"),
            ],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("ruff", *WINDOW)
        assert doc["metrics"]["invocations"] == 1

    def test_a_script_glob_beside_an_executable_glob_is_compared_too(self, radar):
        # A script-shaped glob is not carved out of the cross-check as incomparable. An
        # index that names the script an interpreter runs has a row for it, so an entry
        # declaring ["tool.py", "just"] is compared under BOTH globs and nothing is
        # excluded.
        radar.write_environment(with_index=True)
        radar.index_rows(
            [
                index_row("2026-08-05", exe=["just"]),
                index_row("2026-08-05", exe=["uv", "python", "tool.py"]),
            ],
            generated_at="2026-09-01T00:00:00Z",
        )
        radar.tool("widget", cli_entry("widget", '"tool.py", "just"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "just build"),
                bash("t2", "2026-08-05T10:01:00.000Z", "uv run python tool.py run"),
            ],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", "--reconcile", *WINDOW)
        kind = doc["reconcile"]["kinds"][0]
        assert kind["kind"] == "command"
        assert kind["comparable"] is True
        assert (kind["ours"], kind["index"]) == (2, 2)
        assert not [f for f in doc["findings"] if "reconcile drift" in f]
        assert doc["metrics"]["invocations"] == 2

    def test_a_script_matcher_is_compared_against_the_index_script_names(self, radar):
        # An index that records the script an interpreter runs has a row on both sides
        # for a script-named glob, which is then compared like any other.
        radar.write_environment(with_index=True)
        radar.index_rows(
            [index_row("2026-08-05", exe=["uv", "python", "tool.py"])],
            generated_at="2026-09-01T00:00:00Z",
        )
        radar.tool("widget", cli_entry("widget", '"tool.py"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python tool.py run")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", "--reconcile", *WINDOW)
        kind = doc["reconcile"]["kinds"][0]
        assert kind["kind"] == "command"
        assert (kind["ours"], kind["index"]) == (1, 1)
        assert kind["comparable"] is True
        assert not [f for f in doc["findings"] if "reconcile drift" in f]

    def test_an_index_without_script_names_reads_as_drift(self, radar):
        # An index that records only the interpreter disagrees with this scanner on a
        # script-named glob. That is a real disagreement, and the right signal is drift:
        # excusing it would hide exactly what --reconcile exists to surface.
        radar.write_environment(with_index=True)
        radar.index_rows(
            [index_row("2026-08-05", exe=["uv", "python"])], generated_at="2026-09-01T00:00:00Z"
        )
        radar.tool("widget", cli_entry("widget", '"tool.py"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "uv run python tool.py run")],
            mtime=epoch("2026-08-05"),
        )
        doc = radar.report("widget", "--reconcile", *WINDOW)
        kind = doc["reconcile"]["kinds"][0]
        assert kind["comparable"] is True
        assert (kind["ours"], kind["index"]) == (1, 0)
        assert [f for f in doc["findings"] if "reconcile drift" in f]


def slug_of(path: Path) -> str:
    """The project-folder name Claude Code gives a working directory.

    Every character outside ASCII letters and digits becomes `-`, so a session run under
    the home directory is filed under a name that starts with the home in that spelling -
    `C:/Users/<account>/Documents/p` becomes `C--Users-<account>-Documents-p`.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


class TestTheGenerator:
    """A written report names the command that wrote it, as a reader can run it."""

    def test_the_written_report_names_radar_field(self, radar):
        # `generated_by` names the verb that wrote the report, the command a reader can
        # run to write it again.
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "ruff check ."),
                tool_result("t1", "2026-08-05T10:00:01.000Z"),
            ],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("ruff", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        base = radar.root / "field" / "ruff"
        doc = json.loads((base / "2026-08-01_2026-08-31.json").read_text(encoding="utf-8"))
        md = (base / "2026-08-01_2026-08-31.md").read_text(encoding="utf-8")
        assert doc["generated_by"] == "radar field"
        assert "generated by `radar field` - do not hand-edit" in md


class TestHomeRedaction:
    """The machine's home directory never reaches a written report.

    `field/` is tracked, and a report carried the home in three places nobody wrote by
    hand: `claude_home`, the index location under `--reconcile`, and every project
    slug, which Claude Code derives from the working directory, so every report named the
    machine's account. The written artefacts carry `~`
    instead - `~-` at the head of a slug - and `--json` to stdout keeps the real paths,
    the same split the scoped-name redaction makes.
    """

    STEM = "2026-08-01_2026-08-31"

    def _written(self, radar, tool: str) -> tuple[str, str]:
        base = radar.root / "field" / tool
        md = (base / f"{self.STEM}.md").read_text(encoding="utf-8")
        js = (base / f"{self.STEM}.json").read_text(encoding="utf-8")
        return md, js

    def _board(self, radar, *, with_index: bool = False) -> None:
        radar.user_home = radar.root
        if with_index:
            radar.write_environment(with_index=True)
            radar.index_rows(
                [index_row("2026-08-05", exe=["ruff"])], generated_at="2026-09-01T00:00:00Z"
            )
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [
                bash("t1", "2026-08-05T10:00:00.000Z", "ruff check ."),
                tool_result("t1", "2026-08-05T10:00:01.000Z"),
            ],
            slug=slug_of(radar.root) + "-Documents-p",
            mtime=epoch("2026-08-05"),
        )

    def test_a_slug_under_the_home_is_written_with_the_placeholder(self, radar):
        self._board(radar)
        p = radar.run("ruff", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        md, js = self._written(radar, "ruff")
        doc = json.loads(js)
        assert [i["slug"] for i in doc["invocations"]] == ["~-Documents-p"]
        assert doc["metrics"]["slugs"] == [{"slug": "~-Documents-p", "invocations": 1}]
        assert "| `~-Documents-p` | 1 |" in md
        for text in (md, js):
            assert slug_of(radar.root) not in text

    def test_a_path_under_the_home_is_written_with_the_placeholder(self, radar):
        self._board(radar, with_index=True)
        p = radar.run("ruff", "--reconcile", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        md, js = self._written(radar, "ruff")
        doc = json.loads(js)
        claude_home, store = str(Path("~") / "home"), str(Path("~") / "transcript-index")
        assert doc["claude_home"] == claude_home
        assert doc["reconcile"]["store"] == store
        assert f"claude_home: `{claude_home}`" in md
        assert f"- index location (`[paths].transcript_index`): `{store}`" in md
        for text in (md, js):
            for spelling in (str(radar.root), radar.root.as_posix()):
                assert spelling not in text
                assert json.dumps(spelling)[1:-1] not in text

    def test_nothing_but_the_home_changes(self, radar):
        # The written JSON against the document `--json` prints from the SAME run, which
        # keeps the real paths: once the home is mapped by hand, the two must be equal to
        # the byte. A slug outside the home rides along to show it is left alone.
        self._board(radar, with_index=True)
        radar.session(
            "s2",
            [bash("t2", "2026-08-06T10:00:00.000Z", "ruff format .")],
            mtime=epoch("2026-08-06"),
        )
        p = radar.run("ruff", "--json", "--reconcile", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        real = json.dumps(json.loads(p.stdout), indent=2) + "\n"
        escaped, slug = json.dumps(str(radar.root))[1:-1], slug_of(radar.root)
        # Without the home in the unredacted document the comparison would prove nothing.
        assert escaped in real and slug in real
        assert '"slug": "C--Users-x-Documents-p"' in real
        _md, js = self._written(radar, "ruff")
        assert js == real.replace(escaped, "~").replace(slug, "~")

    def test_a_declared_extra_home_is_written_with_the_placeholder(self, radar):
        # A report mined on this machine can still carry ANOTHER machine's home: sessions
        # synced from it keep their slugs. `redact.local.toml` declares that home, and the
        # written report must map it the way it maps this machine's own.
        self._board(radar)
        other = radar.root.parent / "other.account"
        (radar.root / "redact.local.toml").write_text(
            f"homes = [{json.dumps(other.as_posix())}]\n", encoding="utf-8"
        )
        radar.session(
            "s3",
            [bash("t3", "2026-08-07T10:00:00.000Z", "ruff check .")],
            slug=slug_of(other) + "-Documents-q",
            mtime=epoch("2026-08-07"),
        )
        p = radar.run("ruff", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        md, js = self._written(radar, "ruff")
        slugs = sorted(i["slug"] for i in json.loads(js)["invocations"])
        assert slugs == ["~-Documents-p", "~-Documents-q"], slugs
        for text in (md, js):
            assert slug_of(other) not in text

    def test_stdout_keeps_the_real_paths(self, radar):
        self._board(radar)
        doc = radar.report("ruff", *WINDOW)
        assert doc["claude_home"] == str(radar.home)
        assert doc["invocations"][0]["slug"] == slug_of(radar.root) + "-Documents-p"


class TestLineEndings:
    """field/ is tracked, so a report written with CRLF on Windows shows as modified
    until it is staged, even when no number in it moved."""

    def test_written_reports_carry_no_carriage_returns(self, radar):
        radar.tool("ruff", cli_entry("ruff", '"ruff"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "ruff check .")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("ruff", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        base = radar.root / "field" / "ruff"
        for suffix in ("md", "json"):
            data = (base / f"2026-08-01_2026-08-31.{suffix}").read_bytes()
            assert b"\n" in data
            assert b"\r" not in data, suffix


class TestMarkdownCells:
    """Script names and project slugs come from transcripts, not from the catalogue, and
    field/ is tracked Markdown: a `|` in one would split a table cell, a backtick would
    close its code span, and a tag would render as HTML."""

    HOSTILE = "x|y<img src=x onerror=alert(1)>.py"

    def _md(self, radar, *, slug: str = "C--Users-x-Documents-p") -> str:
        radar.tool("widget", cli_entry("widget", '"*.py"'))
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", f"uv run python '{self.HOSTILE}'")],
            slug=slug,
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("widget", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        return (radar.root / "field" / "widget" / "2026-08-01_2026-08-31.md").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _cells(row: str) -> list[str]:
        return re.split(r"(?<!\\)\|", row.strip())[1:-1]

    def test_a_script_name_stays_in_its_own_cell_and_renders_no_html(self, radar):
        md = self._md(radar)
        assert "<img" not in md
        row = next(ln for ln in md.splitlines() if "onerror" in ln and ln.startswith("|"))
        assert len(self._cells(row)) == 6, row

    def test_a_backtick_in_a_slug_does_not_open_a_code_span(self, radar):
        md = self._md(radar, slug="C--Users-x-Documents-p`q")
        row = next(ln for ln in md.splitlines() if "Documents-p" in ln and ln.startswith("|"))
        assert r"\`q" in row, row
        assert len(self._cells(row)) == 2, row


class TestLocalEntryReportPath:
    """A machine-local entry's name must not land in the tracked `field/` path.

    The report directory is named for the entry's `redact_as` placeholder, not its name,
    so the gate's scope-leak scan over tracked file names does not fail on a path the
    field report itself wrote. An entry with no placeholder cannot be placed safely and
    is refused.
    """

    def _local(self, radar, body: str) -> None:
        local = radar.root / "tools.local"
        local.mkdir(exist_ok=True)
        (local / "local-widget.toml").write_text(body, encoding="utf-8")

    def test_the_report_directory_carries_the_placeholder(self, radar):
        self._local(
            radar,
            'name = "local-widget"\nring = "adopt"\nartifact = "cli"\nredact_as = "local-lib"\n'
            '\n[telemetry]\nmatch_command = ["widgetcli"]\nsince = "2026-08-01"\n',
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "widgetcli run")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("local-widget", *WINDOW)
        assert p.returncode == 0, p.stdout + p.stderr
        assert (radar.root / "field" / "local-lib" / "2026-08-01_2026-08-31.md").is_file()
        assert not (radar.root / "field" / "local-widget").exists(), "the name is in the path"

    def test_a_local_entry_without_a_placeholder_is_refused(self, radar):
        self._local(
            radar,
            'name = "local-widget"\nring = "adopt"\nartifact = "cli"\n'
            '\n[telemetry]\nmatch_command = ["widgetcli"]\nsince = "2026-08-01"\n',
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "widgetcli run")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("local-widget", *WINDOW)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "redact_as" in p.stdout, p.stdout
        assert not (radar.root / "field").exists(), "a report was written for a refused entry"

    @pytest.mark.parametrize("placeholder", ["../../escaped", "a/b"])
    def test_a_placeholder_that_is_not_a_plain_name_is_refused(self, radar, placeholder):
        # The placeholder names a directory under field/, so a separator or `..` in it
        # would choose where the report is written - outside the data root, even.
        self._local(
            radar,
            'name = "local-widget"\nring = "adopt"\nartifact = "cli"\n'
            f'redact_as = "{placeholder}"\n'
            '\n[telemetry]\nmatch_command = ["widgetcli"]\nsince = "2026-08-01"\n',
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "widgetcli run")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("local-widget", *WINDOW)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "redact_as" in p.stdout, p.stdout
        assert not (radar.root.parent / "escaped").exists(), "a report escaped the data root"
        assert not (radar.root / "field").exists(), "a report was written for a refused entry"

    def test_a_placeholder_that_contains_the_name_is_refused(self, radar):
        # field/local-widget-x/ would carry the name it is there to keep out of the path.
        self._local(
            radar,
            'name = "local-widget"\nring = "adopt"\nartifact = "cli"\n'
            'redact_as = "local-widget-x"\n'
            '\n[telemetry]\nmatch_command = ["widgetcli"]\nsince = "2026-08-01"\n',
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "widgetcli run")],
            mtime=epoch("2026-08-05"),
        )
        p = radar.run("local-widget", *WINDOW)
        assert p.returncode == 1, p.stdout + p.stderr
        assert "redact_as" in p.stdout and "contains" in p.stdout, p.stdout
        assert not (radar.root / "field").exists(), "a report was written for a refused entry"

    def test_the_gate_passes_after_a_local_entry_is_measured(self, radar):
        import subprocess

        self._local(
            radar,
            'name = "local-widget"\nring = "adopt"\nartifact = "cli"\nredact_as = "local-lib"\n'
            '\n[telemetry]\nmatch_command = ["widgetcli"]\nsince = "2026-08-01"\n',
        )
        radar.session(
            "s1",
            [bash("t1", "2026-08-05T10:00:00.000Z", "widgetcli run")],
            mtime=epoch("2026-08-05"),
        )
        assert radar.run("local-widget", *WINDOW).returncode == 0
        (radar.root / ".gitignore").write_text("tools.local/\n", encoding="utf-8")
        for argv in (["init", "-q"], ["add", "-A"]):
            subprocess.run(["git", "-C", str(radar.root), *argv], check=True, capture_output=True)
        p = radar.gate()
        assert "[FAIL] scope leak" not in p.stdout, p.stdout + p.stderr
