"""`radar discovery`: what the sessions in a window were shown, from a discovery table.

The verb reads one source, a table in discovery table format, version 1, and the
`derive.json` beside it. Every test builds that table by hand with the fixture's
`discovery_row` helper, so the figures each one asserts are figures it can compute on
paper, and every absence the verb has to name is one a test can plant.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from conftest import discovery_row

SINCE, UNTIL = "2026-10-25", "2026-10-31"
GENERATED = "2026-10-31T21:00:00Z"
RECORDS_END = "2026-10-31T20:00:00Z"


def active(sid: str, date: str, **fields) -> dict:
    """A row of a session that is active on its own: one prompt, one assistant message."""
    return discovery_row(
        sid,
        date,
        **{"user_prompts": 1, "assistant_messages": 1, **fields},
    )


def table(radar, rows: list[dict], **meta) -> Path:
    radar.write_environment(with_discovery=True)
    return radar.discovery_rows(
        rows,
        generated_at=meta.pop("generated_at", GENERATED),
        records_end=meta.pop("records_end", RECORDS_END),
        **meta,
    )


def discovery(radar, *args: str, since: str = SINCE, until: str | None = UNTIL) -> dict:
    window = ("--since", since, *(("--until", until) if until else ()))
    done = radar.script("discovery", *window, "--json", *args)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    return json.loads(done.stdout)


def markdown(radar, *args: str, since: str = SINCE, until: str = UNTIL) -> str:
    done = radar.script("discovery", "--since", since, "--until", until, *args)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    return done.stdout


def warned(radar, *, since: str = SINCE, until: str = UNTIL) -> str:
    """The one line a run that measured nothing prints; asserts it is one `[WARN]` line
    and exit 1, in Markdown and in `--json` alike."""
    lines = set()
    for extra in ((), ("--json",)):
        done = radar.script("discovery", "--since", since, "--until", until, *extra)
        assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
        out = done.stdout.strip().splitlines()
        assert len(out) == 1 and out[0].startswith("[WARN] "), done.stdout
        lines.add(out[0])
    assert len(lines) == 1, lines
    return lines.pop()


def own_entry(name: str, globs: list[str], *, ring: str = "own") -> str:
    return (
        f'name = "{name}"\nring = "{ring}"\nartifact = "cc-plugin"\n\n'
        f"[telemetry]\nmatch_skill = {json.dumps(globs)}\n"
    )


def local_entry(radar, name: str, placeholder: str, telemetry: str = "") -> None:
    local = radar.root / "tools.local"
    local.mkdir(exist_ok=True)
    (local / f"{name}.toml").write_text(
        f'name = "{name}"\nring = "adopt"\nartifact = "cli"\nredact_as = "{placeholder}"\n'
        + (f"\n[telemetry]\n{telemetry}" if telemetry else ""),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------- a full table

FULL = [
    active(
        "sess-a",
        "2026-10-25",
        user_prompts=3,
        assistant_messages=5,
        listing_count=1,
        listing_initial_count=1,
        listing_skill_count_max=40,
        listing_chars_max=1000,
        listing_names=["plugin-a:skill-x", "plugin-a:skill-y", "plugin-b:skill-z"],
        listing_name_only=["plugin-a:skill-x", "plugin-b:skill-z"],
        deferred_delta_count=1,
        deferred_added_names=["mcp__server-a__tool-1", "WebFetch"],
        toolsearch_calls=2,
        toolsearch_select=1,
        toolsearch_keyword=1,
        toolsearch_selected={"WebFetch": 1},
        toolsearch_select_unmatched=1,
        router_injections=2,
        router_named={"plugin-a:skill-x": 2},
        router_named_loaded_after=1,
        router_unparsed=1,
        dispatch_lines=1,
        skills_loaded={"plugin-a:skill-x": 1},
        compact_boundaries=1,
        compact_boundary_at=["2026-10-25T12:00:00Z"],
    ),
    active(
        "sess-a",
        "2026-10-26",
        listing_count=1,
        listing_chars_max=1500,
        listing_name_only=["plugin-a:skill-x"],
        toolsearch_calls_side=3,
        toolsearch_select_side=2,
        toolsearch_keyword_side=1,
        toolsearch_selected_side={"mcp__server-a": 2},
        skills_loaded={"plugin-a:skill-x": 1, "plugin-a:skill-y": 1},
        skills_loaded_unmatched=1,
        router_unmatched=1,
    ),
    active(
        "sess-b",
        "2026-10-27",
        assistant_messages=2,
        listing_count=1,
        listing_chars_max=800,
        deferred_delta_count=2,
        deferred_added_names=["WebFetch"],
        toolsearch_calls=2,
        toolsearch_select=2,
        toolsearch_selected={"WebFetch": 2},
        router_injections=1,
        router_named={"plugin-b:skill-z": 1},
        skills_loaded={"plugin-b:skill-z": 1},
        compact_boundaries=2,
    ),
    # Active, and shown nothing at all.
    active("sess-c", "2026-10-28"),
    # Prompts and no assistant message: a row, but not an active session, so none of this
    # may reach a figure.
    discovery_row(
        "sess-d",
        "2026-10-29",
        user_prompts=2,
        listing_count=1,
        listing_chars_max=99999,
        listing_name_only=["plugin-c:skill-q"],
        deferred_delta_count=4,
        deferred_added_names=["InactiveTool"],
        toolsearch_calls=9,
        toolsearch_select=9,
        toolsearch_selected={"InactiveTool": 9},
        router_injections=9,
        dispatch_lines=3,
        skills_loaded={"plugin-c:skill-q": 5},
        compact_boundaries=7,
    ),
]


class TestAFullTable:
    def test_every_figure_counts_active_sessions_only(self, radar) -> None:
        table(radar, FULL)
        doc = discovery(radar)
        assert doc["sessions"] == {"with_a_row": 4, "active": 3}
        assert doc["listing"]["sessions_with_listing"] == 2
        # sess-a's maximum over its two rows is 1500, sess-b's 800; sess-c has none.
        assert doc["listing"]["listing_chars_max"] == {
            "sessions": 2,
            "median": 1150.0,
            "max": 1500,
        }
        assert doc["listing"]["sessions_with_name_only"] == 1
        assert doc["deferred_tools"] == {
            "sessions_with_delta": 2,
            "announced_most_often": [
                {"name": "WebFetch", "sessions": 2},
                {"name": "mcp__server-a__tool-1", "sessions": 1},
            ],
        }
        ts = doc["toolsearch"]
        assert ts["main"] == {
            "calls": 4,
            "select": 3,
            "keyword": 1,
            "sessions": 2,
            "selected_most_often": [{"name": "WebFetch", "count": 3}],
        }
        assert ts["subagents"] == {
            "calls": 3,
            "select": 2,
            "keyword": 1,
            "sessions": 1,
            "selected_most_often": [{"name": "mcp__server-a", "count": 2}],
        }
        assert ts["select_unmatched"] == {"count": 1, "sessions": 1}
        assert doc["router"] == {
            "sessions_with_injection": 2,
            "injections": 3,
            "named": {"count": 3, "sessions": 2},
            "named_loaded_after": {"count": 1, "sessions": 1},
            "unparsed": {"count": 1, "sessions": 1},
            "unmatched": {"count": 1, "sessions": 1},
        }
        assert doc["dispatch_line"] == {"sessions": 1, "lines": 1}
        assert doc["skills_loaded"] == {
            "sessions": 2,
            "most_often": [
                {"name": "plugin-a:skill-x", "count": 2},
                {"name": "plugin-a:skill-y", "count": 1},
                {"name": "plugin-b:skill-z", "count": 1},
            ],
            "unmatched": {"count": 1, "sessions": 1},
        }
        assert doc["compactions"] == {"count": 3, "sessions": 2}

    def test_name_only_names_are_grouped_by_plugin_prefix(self, radar) -> None:
        table(radar, FULL)
        groups = discovery(radar)["listing"]["name_only_by_prefix"]
        assert groups == [
            {
                "prefix": "plugin-a",
                "own": False,
                "sessions": 1,
                "names": [{"name": "plugin-a:skill-x", "sessions": 1}],
            },
            {
                "prefix": "plugin-b",
                "own": False,
                "sessions": 1,
                "names": [{"name": "plugin-b:skill-z", "sessions": 1}],
            },
        ]

    def test_markdown_writes_every_figure_as_x_of_y_with_the_denominator_named(self, radar) -> None:
        table(radar, FULL)
        md = markdown(radar)
        assert "sessions active in the window: 3 of the 4 sessions with a row in it" in md
        assert "- sessions with a listing: 2 of 3 active sessions" in md
        assert "median 1,150.0, maximum 1,500, over 2 of 3 active sessions" in md
        assert "rendered at least one entry name-only: 1 of 3 active sessions" in md
        assert "- sessions with a deferred-tools delta: 2 of 3 active sessions" in md
        assert "| main thread | 3 | 1 | 4 | 2 of 3 active sessions |" in md
        assert "| subagents | 2 | 1 | 3 | 1 of 3 active sessions |" in md
        router = md.split("## Router", 1)[1].split("## Dispatch line", 1)[0]
        injection_lines = [line for line in router.splitlines() if "injection" in line]
        assert injection_lines == [
            "- sessions with an injection: 2 of 3 active sessions; injections: 3"
        ]
        assert "- compactions (main thread): 3, in 2 of 3 active sessions" in md
        assert "| `plugin-a` |  | 1 | `plugin-a:skill-x` (1) |" in md
        assert "| `WebFetch` | 2 |" in md
        assert "DESCRIPTIVE ONLY" in md
        assert "## Definitions" in md

    def test_a_median_over_an_odd_count_is_a_float_with_one_decimal(self, radar) -> None:
        # One session with a value: the median is that value, printed as a float in
        # --json and with one decimal in the Markdown, the same as over an even count.
        table(radar, [active("s1", "2026-10-25", listing_count=1, listing_chars_max=1000)])
        median = discovery(radar)["listing"]["listing_chars_max"]["median"]
        assert isinstance(median, float) and median == 1000.0
        assert "median 1,000.0, maximum 1,000, over 1 of 1 active sessions" in markdown(radar)

    def test_a_most_often_list_is_the_top_ten_ties_broken_by_name(self, radar) -> None:
        loads = {f"plugin-a:skill-{i:02d}": 1 for i in range(12)}
        loads["plugin-a:skill-11"] = 2
        table(radar, [active("s1", "2026-10-25", skills_loaded=loads)])
        top = discovery(radar)["skills_loaded"]["most_often"]
        assert len(top) == 10
        assert top[0] == {"name": "plugin-a:skill-11", "count": 2}
        assert [t["name"] for t in top[1:]] == [f"plugin-a:skill-{i:02d}" for i in range(9)]

    def test_json_carries_schema_version_1_and_the_table_it_read(self, radar) -> None:
        table(radar, FULL)
        doc = discovery(radar)
        assert doc["schema_version"] == 1
        assert doc["window"] == {"since": SINCE, "until": UNTIL}
        assert doc["environment"] == "default"
        t = doc["table"]
        assert t["discovery_format"] == "discovery-table/1"
        assert t["generated_at"] == "2026-10-31T21:00:00+00:00"
        assert isinstance(t["age_hours"], float)
        assert t["records_end"] == "2026-10-31T20:00:00+00:00"
        assert (t["rows_in_window"], t["unreadable_rows"]) == (5, 0)


class TestTheActiveSessionRule:
    def test_prompts_without_an_assistant_message_is_not_an_active_session(self, radar) -> None:
        table(
            radar,
            [
                discovery_row("prompts-only", "2026-10-25", user_prompts=4, listing_count=1),
                discovery_row("replies-only", "2026-10-26", assistant_messages=4, listing_count=1),
            ],
        )
        doc = discovery(radar)
        assert doc["sessions"] == {"with_a_row": 2, "active": 0}
        assert doc["listing"]["sessions_with_listing"] == 0

    def test_a_session_is_active_over_its_rows_in_the_window_not_per_row(self, radar) -> None:
        # The prompt on one day and the reply on the next still make an active session.
        table(
            radar,
            [
                discovery_row("s1", "2026-10-25", user_prompts=1, listing_count=1),
                discovery_row("s1", "2026-10-26", assistant_messages=1),
            ],
        )
        doc = discovery(radar)
        assert doc["sessions"] == {"with_a_row": 1, "active": 1}
        assert doc["listing"]["sessions_with_listing"] == 1

    def test_a_row_outside_the_window_does_not_make_a_session_active(self, radar) -> None:
        table(
            radar,
            [
                discovery_row("s1", "2026-10-24", assistant_messages=1),
                discovery_row("s1", "2026-10-25", user_prompts=1),
            ],
        )
        assert discovery(radar)["sessions"] == {"with_a_row": 1, "active": 0}


class TestTheOwnPrefixRule:
    def test_a_prefix_is_own_when_an_own_entry_holds_the_prefix_glob(self, radar) -> None:
        radar.tool("own-a", own_entry("own-a", ["plugin-a:*"]))
        # An own entry naming one skill does not claim its prefix.
        radar.tool("own-b", own_entry("own-b", ["plugin-b:skill-z"]))
        # The prefix glob on an entry that is not at `own` does not make it own.
        radar.tool("adopted-c", own_entry("adopted-c", ["plugin-c:*"], ring="adopt"))
        table(
            radar,
            [
                active(
                    "s1",
                    "2026-10-25",
                    listing_count=1,
                    listing_name_only=["plugin-a:skill-x", "plugin-b:skill-z", "plugin-c:x"],
                ),
                active("s2", "2026-10-25", listing_count=1, listing_name_only=["plugin-a:y"]),
            ],
        )
        groups = discovery(radar)["listing"]["name_only_by_prefix"]
        assert [(g["prefix"], g["own"], g["sessions"]) for g in groups] == [
            ("plugin-a", True, 2),
            ("plugin-b", False, 1),
            ("plugin-c", False, 1),
        ]
        md = markdown(radar)
        assert "| `plugin-a` | own | 2 | `plugin-a:skill-x` (1), `plugin-a:y` (1) |" in md
        assert "| `plugin-b` |  | 1 |" in md

    def test_a_name_with_no_prefix_is_grouped_apart_and_never_own(self, radar) -> None:
        radar.tool("own-a", own_entry("own-a", ["plugin-a:*"]))
        table(
            radar,
            [active("s1", "2026-10-25", listing_count=1, listing_name_only=["bare-skill"])],
        )
        [group] = discovery(radar)["listing"]["name_only_by_prefix"]
        assert (group["prefix"], group["own"]) == (None, False)
        assert "| (no prefix) |  | 1 | `bare-skill` (1) |" in markdown(radar)


class TestCoverage:
    def test_days_before_the_tables_first_row_are_unmeasured(self, radar) -> None:
        table(radar, [active("s1", "2026-10-27"), active("s2", "2026-10-30")])
        doc = discovery(radar)
        assert doc["coverage"] == {
            "window_days": 7,
            "measured_from": "2026-10-27",
            "measured_to": "2026-10-31",
            "measured_days": 5,
            "partial_day": "2026-10-31",
            "unmeasured": [
                {
                    "from": "2026-10-25",
                    "to": "2026-10-26",
                    "reason": "before the table's first row",
                }
            ],
        }
        md = markdown(radar)
        assert "- unmeasured: 2026-10-25 to 2026-10-26 (before the table's first row)" in md

    def test_records_end_before_until_leaves_later_days_unmeasured_and_its_day_partial(
        self, radar
    ) -> None:
        table(
            radar,
            [
                active("s1", "2026-10-25"),
                active("s2", "2026-10-29"),
                # A row dated after the records end is not a measurement of that day.
                active("s3", "2026-10-30", listing_count=1),
            ],
            records_end="2026-10-29T12:00:00Z",
        )
        doc = discovery(radar)
        c = doc["coverage"]
        assert (c["measured_from"], c["measured_to"], c["measured_days"]) == (
            "2026-10-25",
            "2026-10-29",
            5,
        )
        assert c["partial_day"] == "2026-10-29"
        assert c["unmeasured"] == [
            {"from": "2026-10-30", "to": "2026-10-31", "reason": "after the table's records end"}
        ]
        assert doc["sessions"] == {"with_a_row": 2, "active": 2}
        assert doc["listing"]["sessions_with_listing"] == 0
        assert doc["table"]["rows_after_records_end"] == 1
        md = markdown(radar)
        assert "- partial: 2026-10-29, the day the table's records end" in md
        assert "- unmeasured: 2026-10-30 to 2026-10-31 (after the table's records end)" in md
        assert "rows dated after the records end, not read: 1" in md

    def test_records_end_after_the_window_flags_no_day_partial(self, radar) -> None:
        table(radar, [active("s1", "2026-10-25")], records_end="2026-11-03T00:00:00Z")
        c = discovery(radar)["coverage"]
        assert (c["partial_day"], c["unmeasured"], c["measured_days"]) == (None, [], 7)

    def test_records_end_is_read_as_a_utc_date(self, radar) -> None:
        # 01:30 at +02:00 is 23:30 of the day before in UTC.
        table(radar, [active("s1", "2026-10-25")], records_end="2026-10-30T01:30:00+02:00")
        assert discovery(radar)["coverage"]["partial_day"] == "2026-10-29"


class TestTheWindowBounds:
    ROWS = [
        active("before", "2026-10-24"),
        active("first", "2026-10-25"),
        active("last", "2026-10-31"),
        active("after", "2026-11-01"),
    ]

    def test_until_is_inclusive_and_the_day_after_it_is_not(self, radar) -> None:
        table(radar, self.ROWS, records_end="2026-11-02T00:00:00Z")
        assert discovery(radar)["sessions"]["active"] == 2
        assert discovery(radar, until="2026-11-01")["sessions"]["active"] == 3
        assert discovery(radar, until="2026-10-30")["sessions"]["active"] == 1

    def test_since_is_inclusive_and_the_day_before_it_is_not(self, radar) -> None:
        table(radar, self.ROWS, records_end="2026-11-02T00:00:00Z")
        assert discovery(radar, since="2026-10-24")["sessions"]["active"] == 3
        assert discovery(radar, since="2026-10-26")["sessions"]["active"] == 1

    def test_until_defaults_to_today_in_utc(self, radar) -> None:
        today = dt.datetime.now(dt.UTC).date()
        table(
            radar,
            [active("today", today.isoformat())],
            records_end=dt.datetime.now(dt.UTC).isoformat(),
        )
        doc = discovery(radar, since=today.isoformat(), until=None)
        if doc["window"]["until"] != today.isoformat():
            pytest.skip("the UTC date changed while the test ran")
        assert doc["sessions"]["active"] == 1

    def test_the_help_states_the_until_convention(self, radar) -> None:
        done = radar.script("discovery", "--help")
        assert done.returncode == 0, done.stdout + done.stderr
        text = " ".join(done.stdout.split())
        assert "--until UNTIL YYYY-MM-DD, inclusive; default today (UTC)" in text, text
        assert "[paths].discovery_index" in text, text


class TestAbsencesAreNamedNeverZero:
    def test_an_absent_profile_key(self, radar) -> None:
        radar.discovery_rows(FULL, generated_at=GENERATED, records_end=RECORDS_END)
        line = warned(radar)
        assert "declares no [paths].discovery_index" in line, line

    def test_an_absent_table(self, radar) -> None:
        radar.write_environment(with_discovery=True)
        line = warned(radar)
        assert line.startswith("[WARN] no discovery table at the path"), line

    def test_an_absent_derive_json(self, radar) -> None:
        table(radar, FULL, derive=False)
        line = warned(radar)
        assert line.startswith("[WARN] no derive.json beside the discovery table"), line

    def test_an_absent_format(self, radar) -> None:
        table(radar, FULL, discovery_format=None)
        line = warned(radar)
        assert "derive.json carries no discovery_format" in line, line

    def test_an_unknown_format(self, radar) -> None:
        table(radar, FULL, discovery_format="discovery-table/2")
        line = warned(radar)
        assert 'discovery_format is "discovery-table/2"' in line, line
        assert "it reads discovery-table/1" in line, line

    def test_an_absent_records_end(self, radar) -> None:
        table(radar, FULL, records_end=None)
        line = warned(radar)
        assert "derive.json carries no records_end" in line, line

    def test_a_records_end_that_is_not_a_time(self, radar) -> None:
        table(radar, FULL, records_end="yesterday-ish")
        line = warned(radar)
        assert 'records_end "yesterday-ish" is not an ISO 8601 time' in line, line

    def test_a_derive_json_that_is_not_json(self, radar) -> None:
        path = table(radar, FULL)
        (path.parent / "derive.json").write_text("{not json", encoding="utf-8")
        line = warned(radar)
        assert "derive.json beside the discovery table is not a JSON object" in line, line

    def test_an_empty_window(self, radar) -> None:
        table(radar, FULL)
        line = warned(radar, since="2026-01-01", until="2026-01-07")
        assert line == (
            "[WARN] the discovery table has no row from 2026-01-01 to 2026-01-07 (its rows "
            "run from 2026-10-25 to 2026-10-29; its records end 2026-10-31)"
        )

    def test_a_window_wholly_after_the_records_end(self, radar) -> None:
        table(radar, [active("s1", "2026-10-25"), active("s2", "2026-11-02")])
        line = warned(radar, since="2026-11-01", until="2026-11-07")
        assert "has no row from 2026-11-01 to 2026-11-07" in line, line
        assert "its records end 2026-10-31; 1 row(s) of the window are dated after that" in line

    def test_an_empty_table(self, radar) -> None:
        table(radar, [])
        line = warned(radar)
        assert "it holds no row in the format's shape" in line, line


class TestRowsOfTheWrongShape:
    def test_they_are_skipped_counted_and_warned_about(self, radar) -> None:
        good = active("good", "2026-10-25", listing_count=1)
        wrong_type = active("wrong-type", "2026-10-25", listing_count="1")
        missing = active("missing", "2026-10-25", listing_count=1)
        del missing["skills_loaded"]
        boolean = active("boolean", "2026-10-25", listing_count=True)
        path = table(radar, [good, wrong_type, missing, boolean])
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"session_id": "torn", "date": \n\n')
        doc = discovery(radar)
        assert doc["table"]["unreadable_rows"] == 4
        assert doc["sessions"] == {"with_a_row": 1, "active": 1}
        md = markdown(radar)
        assert "[WARN] 4 line(s) of the table are not rows in the discovery-table/1 shape" in md


class TestNames:
    """No string the table holds outside a printed name field reaches either output, and
    a printed name goes through the redaction `radar field` applies to what it writes."""

    def _both(self, radar) -> list[str]:
        return [markdown(radar), json.dumps(discovery(radar))]

    def test_a_string_planted_outside_a_name_field_never_reaches_the_output(self, radar) -> None:
        planted = active(
            "s1",
            "2026-10-25",
            slug="C--Users-x-Documents-PLANTED-slug",
            cli_versions=["PLANTED-version"],
            listing_count=1,
            listing_names=["PLANTED-listed"],
            deferred_delta_count=1,
            deferred_removed_names=["PLANTED-removed"],
            deferred_surfaced_names=["PLANTED-surfaced"],
            mcp_servers_pending=["PLANTED-pending"],
            mcp_servers_failed=["PLANTED-failed"],
            mcp_servers_needs_auth=["PLANTED-auth"],
            mcp_instructions_servers=["PLANTED-instructions"],
            hook_success={"PLANTED-hook": 1},
            hook_additional_context={"PLANTED-context": 1},
            hook_errors={"command:PLANTED-error": 1},
            router_named={"PLANTED-router": 1},
            dispatch_named=["PLANTED-dispatch"],
            attachment_types={"PLANTED-attachment": 1},
        )
        planted["free_text"] = "PLANTED-unknown-field"
        table(radar, [planted])
        for text in self._both(radar):
            assert "PLANTED" not in text, text

    def test_a_scoped_term_planted_as_a_name_prints_redacted(self, radar) -> None:
        local_entry(radar, "secret-tool", "local-tool")
        table(
            radar,
            [
                active(
                    "s1",
                    "2026-10-25",
                    listing_count=1,
                    listing_name_only=["secret-tool:skill-x"],
                    deferred_delta_count=1,
                    deferred_added_names=["mcp__secret-tool__run"],
                    toolsearch_calls=1,
                    toolsearch_select=1,
                    toolsearch_selected={"mcp__Secret-Tool": 1},
                    skills_loaded={"secret-tool:skill-x": 2},
                )
            ],
        )
        for text in self._both(radar):
            assert "secret-tool" not in text.lower(), text
        doc = discovery(radar)
        [group] = doc["listing"]["name_only_by_prefix"]
        assert (group["prefix"], group["names"][0]["name"]) == ("local-tool", "local-tool:skill-x")
        assert doc["deferred_tools"]["announced_most_often"][0]["name"] == "mcp__local-tool__run"
        assert doc["toolsearch"]["main"]["selected_most_often"][0]["name"] == "mcp__local-tool"
        assert doc["skills_loaded"]["most_often"][0] == {"name": "local-tool:skill-x", "count": 2}

    def test_a_name_a_local_entrys_matcher_claims_prints_as_its_redact_as(self, radar) -> None:
        local_entry(
            radar,
            "hidden-thing",
            "local-two",
            'match_skill = ["quiet-plugin:*"]\nmatch = ["mcp__quiet-server__*"]\n',
        )
        table(
            radar,
            [
                active(
                    "s1",
                    "2026-10-25",
                    listing_count=1,
                    listing_name_only=["quiet-plugin:skill-q"],
                    deferred_delta_count=1,
                    deferred_added_names=["mcp__quiet-server__run"],
                    toolsearch_calls=1,
                    toolsearch_select=1,
                    toolsearch_selected={"mcp__quiet-server": 1},
                    skills_loaded={"quiet-plugin:skill-q": 1, "quiet-plugin:other": 1},
                )
            ],
        )
        for text in self._both(radar):
            assert "quiet" not in text, text
        doc = discovery(radar)
        [group] = doc["listing"]["name_only_by_prefix"]
        assert group["prefix"] == "local-two"
        assert group["names"] == [{"name": "local-two", "sessions": 1}]
        assert doc["deferred_tools"]["announced_most_often"] == [
            {"name": "local-two", "sessions": 1}
        ]
        assert doc["toolsearch"]["main"]["selected_most_often"] == [
            {"name": "local-two", "count": 1}
        ]
        # Two skills claimed by one entry are one printed name, and their loads add up.
        assert doc["skills_loaded"]["most_often"] == [{"name": "local-two", "count": 2}]

    @pytest.mark.parametrize(
        "glob",
        [
            "quiet-plugin:skill-q",
            "quiet-plugin:skill-?",
            "quiet-*:skill-q",
            "quiet*",
            "quiet-plugin",
        ],
    )
    def test_a_narrow_skill_claim_hides_the_whole_prefix(self, radar, glob: str) -> None:
        # The entry claims one skill (or a glob that could match one under the prefix), and
        # neither its name nor a scoped term holds the prefix. Like a narrow `match` glob,
        # which claims its whole MCP server, it claims the whole prefix: the group label and
        # every name under it print as the entry's `redact_as`, never the prefix in the clear.
        local_entry(radar, "hidden-thing", "local-two", f"match_skill = {json.dumps([glob])}\n")
        table(
            radar,
            [
                active(
                    "s1",
                    "2026-10-25",
                    listing_count=1,
                    listing_name_only=["quiet-plugin:skill-q", "plugin-a:skill-x"],
                    skills_loaded={"quiet-plugin:skill-q": 1, "quiet-plugin:skill-r": 2},
                )
            ],
        )
        for text in self._both(radar):
            assert "quiet" not in text, text
        doc = discovery(radar)
        groups = {g["prefix"]: g["names"] for g in doc["listing"]["name_only_by_prefix"]}
        assert groups == {
            "local-two": [{"name": "local-two", "sessions": 1}],
            "plugin-a": [{"name": "plugin-a:skill-x", "sessions": 1}],
        }
        assert doc["skills_loaded"]["most_often"] == [{"name": "local-two", "count": 3}]
        md = markdown(radar)
        assert "| `local-two` |  | 1 | `local-two` (1) |" in md

    @pytest.mark.parametrize(
        "claim",
        [
            'match_skill = ["quiet-plugin:skill-q"]\n',
            'match = ["mcp__plugin_quiet-plugin_srv__t1"]\n',
        ],
        ids=["skill-side", "tool-side"],
    )
    def test_a_plugin_is_one_namespace_across_skills_and_mcp_tools(self, radar, claim) -> None:
        # Claude Code names a plugin's MCP server `plugin_<plugin>_<server>`, so the plugin
        # prefix prints inside each of its tool names too. A claim on either side - one
        # skill, or one tool - claims the namespace on both: no skill, group label, tool or
        # server of the plugin prints the prefix, in any field that carries tool names,
        # while another plugin's names print in the clear.
        local_entry(radar, "hidden-thing", "local-two", claim)
        quiet_tools = {
            "mcp__plugin_quiet-plugin_srv__t1": 1,
            "mcp__plugin_quiet-plugin_other__t2": 1,
            "mcp__plugin_quiet-plugin_srv": 1,
        }
        table(
            radar,
            [
                active(
                    "s1",
                    "2026-10-25",
                    listing_count=1,
                    listing_name_only=["quiet-plugin:skill-q", "plugin-a:skill-x"],
                    deferred_delta_count=1,
                    deferred_added_names=[*quiet_tools, "mcp__plugin_plugin-a_srv__t1"],
                    toolsearch_calls=1,
                    toolsearch_select=1,
                    toolsearch_selected={**quiet_tools, "mcp__plugin_plugin-a_srv__t1": 1},
                    toolsearch_calls_side=1,
                    toolsearch_select_side=1,
                    toolsearch_selected_side={**quiet_tools, "mcp__plugin_plugin-a_srv__t1": 1},
                    skills_loaded={"quiet-plugin:skill-q": 1, "quiet-plugin:skill-r": 2},
                )
            ],
        )
        for text in self._both(radar):
            assert "quiet" not in text, text
            assert "plugin-a:skill-x" in text and "mcp__plugin_plugin-a_srv__t1" in text, text
        doc = discovery(radar)
        groups = {g["prefix"]: g["names"] for g in doc["listing"]["name_only_by_prefix"]}
        assert groups == {
            "local-two": [{"name": "local-two", "sessions": 1}],
            "plugin-a": [{"name": "plugin-a:skill-x", "sessions": 1}],
        }
        assert doc["skills_loaded"]["most_often"] == [{"name": "local-two", "count": 3}]
        assert doc["deferred_tools"]["announced_most_often"] == [
            {"name": "local-two", "sessions": 1},
            {"name": "mcp__plugin_plugin-a_srv__t1", "sessions": 1},
        ]
        for thread in ("main", "subagents"):
            assert doc["toolsearch"][thread]["selected_most_often"] == [
                {"name": "local-two", "count": 3},
                {"name": "mcp__plugin_plugin-a_srv__t1", "count": 1},
            ]

    def test_the_home_folder_in_a_name_prints_as_the_placeholder(self, radar) -> None:
        radar.user_home = radar.root
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(radar.root))
        table(radar, [active("s1", "2026-10-25", skills_loaded={f"{slug}-Documents-p:x": 1})])
        for text in self._both(radar):
            assert slug not in text, text
        loaded = discovery(radar)["skills_loaded"]["most_often"]
        assert loaded == [{"name": "~-Documents-p:x", "count": 1}]

    def test_no_path_reaches_either_output(self, radar) -> None:
        path = table(radar, FULL)
        for text in self._both(radar):
            assert path.parent.as_posix() not in text
            assert str(path.parent) not in text
            assert "Documents-p" not in text


class TestTheListingProbe:
    """A listing on one more active session moves "sessions with a listing" and nothing
    else: the probe the table's first comparison with a reference run is checked by."""

    def _doc(self, radar, rows: list[dict]) -> dict:
        table(radar, rows)
        doc = discovery(radar)
        doc.pop("table")  # its age is read from the clock
        return doc

    def test_one_listing_row_moves_one_figure_by_one(self, radar) -> None:
        before = self._doc(radar, FULL)
        probed = json.loads(json.dumps(FULL))
        # sess-c is active and has no listing row.
        [row] = [r for r in probed if r["session_id"] == "sess-c"]
        row["listing_count"] = 1
        after = self._doc(radar, probed)
        assert after["listing"]["sessions_with_listing"] == (
            before["listing"]["sessions_with_listing"] + 1
        )
        after["listing"]["sessions_with_listing"] -= 1
        assert after == before


def test_bad_arguments_exit_2(radar) -> None:
    assert radar.script("discovery").returncode == 2
    assert radar.script("discovery", "--since", "yesterday").returncode == 2
    done = radar.script("discovery", "--since", "2026-10-02", "--until", "2026-10-01")
    assert done.returncode == 2


def test_an_unknown_environment_fails(radar) -> None:
    table(radar, FULL)
    done = radar.script("discovery", "--since", SINCE, "--until", UNTIL, "--env", "nowhere")
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout.strip() == "[FAIL] no environment named 'nowhere'"


def test_the_verb_writes_nothing_to_disk(radar) -> None:
    table(radar, FULL)
    before = sorted(p.as_posix() for p in radar.root.rglob("*"))
    markdown(radar)
    discovery(radar)
    radar.script("discovery", "--since", "2026-01-01", "--until", "2026-01-02")
    assert sorted(p.as_posix() for p in radar.root.rglob("*")) == before
