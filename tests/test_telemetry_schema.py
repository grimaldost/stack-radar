"""The [telemetry] block's schema, which is what stands between a typo and a false zero.

TOML has no notion of an unknown key, so `match_commands = ["ruff"]` parses cleanly,
matches nothing, and produces a report saying zero invocations - a number that reads
exactly like evidence of disuse. A matcher of the wrong kind returns the same confident
zero by another route (an MCP glob for a library, a server glob for a docs-server that
is mounted some other way), which is why the key set is checked rather than trusted.
"""

from __future__ import annotations

import pytest
from stack_radar.radar_lib import OPPORTUNITY_KEYS, TELEMETRY_KEYS, unknown_telemetry_keys, validate

BASE = {
    "name": "x",
    "repo": "https://example.invalid/x",
    "axis": "lint-format",
    "ring": "adopt",
    "license": "MIT",
    "visibility": "public",
    "note": "n",
    "artifact": "cli",
    "history": [{"date": "2026-08-01", "ring": "adopt", "reason": "a fixture", "evidence": "e"}],
}


def tool(**telemetry) -> dict:
    return {**BASE, "telemetry": {"match_command": ["x"], "since": "2026-08-01", **telemetry}}


class TestUnknownKeys:
    def test_a_mistyped_matcher_name_is_reported(self):
        assert unknown_telemetry_keys(tool(match_commands=["ruff"])) == ["match_commands"]

    def test_every_documented_key_is_accepted(self):
        t = tool(
            match=["mcp__x__*"],
            match_skill=["x:*"],
            exposed=["mcp__x__y"],
            also_runs_in=["pre-commit hook"],
            opportunity_match=["WebFetch"],
            opportunity_match_skill=["y:*"],
            opportunity_match_command=["rg"],
        )
        assert unknown_telemetry_keys(t) == []
        assert set(t["telemetry"]) <= set(TELEMETRY_KEYS)

    def test_an_entry_without_telemetry_is_clean(self):
        assert unknown_telemetry_keys(dict(BASE)) == []

    @pytest.mark.parametrize("key", OPPORTUNITY_KEYS)
    def test_the_singular_spelling_of_an_opportunity_key_is_reported(self, key):
        # The whole reason the set is closed: `opportunity_matches` parses as valid TOML,
        # is read by nothing, and leaves the entry reporting `taken_ratio: null` - which
        # looks exactly like "no opportunity matcher was declared", the honest state.
        typo = key + "es"
        assert unknown_telemetry_keys(tool(**{typo: ["WebFetch"]})) == [typo]


class TestMatcherGlobsAreNonEmpty:
    """An empty or whitespace-only glob passes the "needs one matcher" rule while matching
    nothing - the silent zero the closed key set exists to prevent."""

    @pytest.mark.parametrize("key", ["match", "match_skill", "match_command"])
    @pytest.mark.parametrize("bad", [[""], ["   "], ["ok", ""]])
    def test_an_empty_or_blank_glob_is_a_schema_error(self, key, bad):
        errs = validate(tool(**{key: bad}))
        assert any(key in e for e in errs), errs

    @pytest.mark.parametrize("key", ["match", "match_skill", "match_command"])
    def test_a_real_glob_still_validates(self, key):
        assert validate(tool(**{key: ["widgetcli"]})) == []


class TestOpportunityShape:
    @pytest.mark.parametrize("key", OPPORTUNITY_KEYS)
    def test_a_list_of_globs_validates(self, key):
        assert validate(tool(**{key: ["WebFetch", "WebSearch"]})) == []

    @pytest.mark.parametrize("key", OPPORTUNITY_KEYS)
    @pytest.mark.parametrize("bad", ["WebFetch", [""], [1], {"a": 1}])
    def test_anything_else_is_a_schema_error(self, key, bad):
        errs = validate(tool(**{key: bad}))
        assert any(key in e for e in errs), errs

    def test_an_opportunity_matcher_is_not_a_matcher(self):
        # It answers "was there an occasion", never "was it used", so it must not satisfy
        # the [telemetry] requirement gate.py enforces on own/adopt entries. An entry
        # whose only glob were an opportunity glob would report zero use forever with
        # nothing saying the use matcher was missing.
        t = {**BASE, "telemetry": {"opportunity_match": ["WebFetch"], "since": "2026-08-01"}}
        errs = validate(t)
        assert any("needs one of `match`" in e for e in errs), errs


class TestAlsoRunsInShape:
    def test_a_list_of_strings_validates(self):
        assert validate(tool(also_runs_in=["pre-commit hook", "CI"])) == []

    @pytest.mark.parametrize("bad", ["pre-commit hook", [""], [1], {"a": 1}])
    def test_anything_else_is_a_schema_error(self, bad):
        errs = validate(tool(also_runs_in=bad))
        assert any("also_runs_in" in e for e in errs), errs
