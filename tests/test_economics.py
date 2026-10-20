"""`radar economics`: the token economics of the sessions in a window, from transcripts.

The verb reads `message.usage` from the assistant records Claude Code writes, which no
other verb reads. Every test here builds its transcripts by hand in the `radar` fixture's
synthetic home, so the figures each one asserts are figures it can compute on paper.
"""

from __future__ import annotations

import json
import uuid

SINCE, UNTIL = "2026-10-01", "2026-10-02"
SECRET_SLUG = "C--Users-x-Documents-secretproj"


def assistant(msg_id: str, ts: str, model: str = "model-a", **usage) -> dict:
    """One assistant record carrying `usage`, as transcripts write it. Nested parts of
    the usage block (`cache_creation`, `output_tokens_details`) are passed as dicts."""
    return {
        "type": "assistant",
        "timestamp": ts,
        "uuid": str(uuid.uuid4()),
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "private message content"}],
            "usage": usage,
        },
    }


def economics(radar, *args: str, since: str = SINCE, until: str = UNTIL) -> dict:
    done = radar.script("economics", "--since", since, "--until", until, "--json", *args)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    return json.loads(done.stdout)


def test_streaming_duplicates_count_as_one_turn_with_the_larger_value(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m1", "2026-10-01T10:00:00.000Z", input_tokens=5, output_tokens=10),
            assistant("m1", "2026-10-01T10:00:02.000Z", input_tokens=5, output_tokens=50),
        ],
    )
    doc = economics(radar)
    assert doc["totals"]["turns"] == 1
    assert doc["totals"]["output_tokens"] == 50
    assert doc["totals"]["input_tokens"] == 5


def test_the_model_is_attributed_per_message_not_per_file(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m1", "2026-10-01T10:00:00Z", "model-a", input_tokens=1, output_tokens=7),
            assistant("m2", "2026-10-01T10:01:00Z", "model-b", input_tokens=1, output_tokens=11),
            assistant("m3", "2026-10-01T10:02:00Z", "model-b", input_tokens=1, output_tokens=13),
        ],
    )
    by_model = economics(radar)["by_model"]
    assert by_model["model-a"]["turns"] == 1
    assert by_model["model-a"]["output_tokens"] == 7
    assert by_model["model-b"]["turns"] == 2
    assert by_model["model-b"]["output_tokens"] == 24


def test_a_synthetic_model_is_not_a_turn(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=7),
            assistant("m2", "2026-10-01T10:01:00Z", "<synthetic>", input_tokens=0, output_tokens=0),
        ],
    )
    doc = economics(radar)
    assert doc["totals"]["turns"] == 1
    assert "<synthetic>" not in doc["by_model"]


def test_a_recreation_event_is_reported_with_its_gap_and_not_at_exactly_half(radar) -> None:
    usage = {"input_tokens": 0, "output_tokens": 1}
    radar.session(
        "s1",
        [
            # context(1) = 1000
            assistant(
                "m1",
                "2026-10-01T10:00:00Z",
                cache_creation_input_tokens=1000,
                cache_read_input_tokens=0,
                **usage,
            ),
            # 600 > 0.5 x 1000: an event, 400 s after turn 1. context(2) = 1000.
            assistant(
                "m2",
                "2026-10-01T10:06:40Z",
                cache_creation_input_tokens=600,
                cache_read_input_tokens=400,
                **usage,
            ),
            # 500 is exactly 0.5 x 1000: not an event.
            assistant(
                "m3",
                "2026-10-01T10:07:10Z",
                cache_creation_input_tokens=500,
                cache_read_input_tokens=500,
                **usage,
            ),
        ],
    )
    rec = economics(radar)["recreation"]
    assert rec["events"] == 1
    assert rec["eligible_turns"] == 2
    assert rec["tokens"] == 600
    assert rec["gap_seconds_median"] == 400
    assert rec["gap_seconds_max"] == 400
    assert rec["gap_over_300s"] == 1
    # uncached input = input + creation = 0 + 1000 + 600 + 500
    assert rec["share_of_uncached_input"] == round(600 / 2100, 4)


def test_the_ttl_split_sums_the_breakdown_and_counts_turns_without_it(radar) -> None:
    radar.session(
        "s1",
        [
            assistant(
                "m1",
                "2026-10-01T10:00:00Z",
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=300,
                cache_creation={
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 200,
                },
            ),
            assistant(
                "m2",
                "2026-10-01T10:01:00Z",
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=50,
            ),
            assistant(
                "m3",
                "2026-10-01T10:02:00Z",
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
            ),
        ],
    )
    ttl = economics(radar)["ttl"]
    assert ttl["ephemeral_5m_tokens"] == 100
    assert ttl["ephemeral_1h_tokens"] == 200
    assert ttl["unattributed_tokens"] == 50
    assert ttl["unattributed_turns"] == 1
    assert ttl["turns_with_creation"] == 2


def test_records_outside_the_window_are_excluded(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m0", "2026-09-30T23:59:59Z", input_tokens=1, output_tokens=1000),
            assistant("m1", "2026-10-01T00:00:00Z", input_tokens=1, output_tokens=10),
            assistant("m2", "2026-10-02T23:59:59Z", input_tokens=1, output_tokens=20),
            assistant("m3", "2026-10-03T00:00:00Z", input_tokens=1, output_tokens=1000),
        ],
    )
    totals = economics(radar)["totals"]
    assert totals["turns"] == 2
    assert totals["output_tokens"] == 30


def test_main_session_and_sidechain_are_split(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=3),
            assistant("m2", "2026-10-01T10:01:00Z", input_tokens=1, output_tokens=4),
        ],
    )
    radar.session(
        "s1",
        [assistant("a1", "2026-10-01T10:00:30Z", input_tokens=1, output_tokens=5)],
        sub="subagents/agent-a.jsonl",
    )
    doc = economics(radar)
    assert doc["by_side"]["main"]["turns"] == 2
    assert doc["by_side"]["main"]["transcripts"] == 1
    assert doc["by_side"]["main"]["output_tokens"] == 7
    assert doc["by_side"]["sidechain"]["turns"] == 1
    assert doc["by_side"]["sidechain"]["transcripts"] == 1
    assert doc["by_side"]["sidechain"]["output_tokens"] == 5
    rows = {(r["session_id"], r["agent"]) for r in doc["per_transcript"]}
    assert rows == {("s1", None), ("s1", "agent-a")}


def test_per_transcript_shape_t_c0_cf_and_growth(radar) -> None:
    radar.session(
        "s1",
        [
            assistant("m1", "2026-10-01T10:00:00Z", input_tokens=100, output_tokens=1),
            assistant(
                "m2",
                "2026-10-01T10:01:00Z",
                input_tokens=10,
                cache_read_input_tokens=190,
                output_tokens=1,
            ),
            assistant(
                "m3",
                "2026-10-01T10:02:00Z",
                input_tokens=10,
                cache_creation_input_tokens=90,
                cache_read_input_tokens=300,
                output_tokens=1,
            ),
        ],
    )
    doc = economics(radar)
    [row] = doc["per_transcript"]
    assert (row["T"], row["C0"], row["Cf"], row["g"]) == (3, 100, 400, 150.0)
    assert doc["distribution"]["T"]["total"] == 3
    assert doc["distribution"]["g"]["n"] == 1


def test_missing_fields_are_counted_not_zeroed_and_malformed_lines_are_not_fatal(radar) -> None:
    path = radar.session(
        "s1",
        [
            assistant(
                "m1",
                "2026-10-01T10:00:00Z",
                input_tokens=1,
                output_tokens=9,
                output_tokens_details={"thinking_tokens": 4},
            ),
            assistant("m2", "2026-10-01T10:01:00Z", input_tokens=1, output_tokens=9),
        ],
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","message":{"usage":\n')
    doc = economics(radar)
    assert doc["totals"]["thinking_tokens"] == 4
    assert doc["missing_fields"]["thinking_tokens"] == 1
    assert doc["missing_fields"]["cache_read_input_tokens"] == 2
    assert doc["malformed_lines"] == 1


def test_an_empty_window_warns_and_exits_1(radar) -> None:
    radar.session("s1", [assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=1)])
    done = radar.script("economics", "--since", "2026-01-01", "--until", "2026-01-02")
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout.strip() == (
        "[WARN] read 1 transcript(s), found 0 assistant messages with usage "
        "between 2026-01-01 and 2026-01-02"
    )


def test_bad_arguments_exit_2(radar) -> None:
    assert radar.script("economics").returncode == 2
    assert radar.script("economics", "--since", "yesterday").returncode == 2
    done = radar.script("economics", "--since", "2026-10-02", "--until", "2026-10-01")
    assert done.returncode == 2


def test_no_slug_path_or_content_reaches_either_output(radar) -> None:
    radar.session(
        "s1",
        [assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=1)],
        slug=SECRET_SLUG,
    )
    radar.session(
        "s1",
        [assistant("a1", "2026-10-01T10:00:30Z", input_tokens=1, output_tokens=1)],
        slug=SECRET_SLUG,
        sub="subagents/agent-a.jsonl",
    )
    for extra in ((), ("--json",)):
        done = radar.script("economics", "--since", SINCE, "--until", UNTIL, *extra)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert "secretproj" not in done.stdout
        assert "private message content" not in done.stdout
        assert radar.home.as_posix() not in done.stdout
        assert str(radar.home) not in done.stdout


def test_json_carries_schema_version_1(radar) -> None:
    radar.session("s1", [assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=1)])
    doc = economics(radar)
    assert doc["schema_version"] == 1
    assert doc["window"] == {"since": SINCE, "until": UNTIL}
    assert doc["transcripts_read"] == 1


def test_markdown_states_it_is_descriptive_and_defines_its_terms(radar) -> None:
    radar.session("s1", [assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=1)])
    done = radar.script("economics", "--since", SINCE, "--until", UNTIL)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "DESCRIPTIVE ONLY" in done.stdout
    assert "context(k)" in done.stdout
    assert "1/1" in done.stdout


def test_the_verb_writes_nothing_to_disk(radar) -> None:
    radar.session("s1", [assistant("m1", "2026-10-01T10:00:00Z", input_tokens=1, output_tokens=1)])
    before = sorted(p.as_posix() for p in radar.root.rglob("*"))
    radar.script("economics", "--since", SINCE, "--until", UNTIL)
    radar.script("economics", "--since", SINCE, "--until", UNTIL, "--json")
    assert sorted(p.as_posix() for p in radar.root.rglob("*")) == before
