"""The entry schema's rules that decide whether a ring is backed by a record.

`validate()` is what `radar gate` FAILs on and what `radar add` refuses with, so each rule
here is tested twice where it matters: once against the function, and once through the
gate as a subprocess, so the exit code a CI step branches on is part of the proof.
"""

from __future__ import annotations

import datetime

import pytest

from stack_radar.radar_lib import validate

BASE = {
    "name": "ruff",
    "repo": "https://github.com/astral-sh/ruff",
    "axis": "lint-format",
    "ring": "observe",
    "license": "MIT",
    "visibility": "public",
    "note": "linter",
    "artifact": "cli",
}


def entry(*history: dict, **fields) -> dict:
    ring = history[-1].get("ring") if history else "observe"
    return {**BASE, "ring": ring, "history": list(history), **fields}


def block(ring: str = "observe", **fields) -> dict:
    return {"date": "2026-08-01", "ring": ring, "reason": "a fixture", **fields}


# ---- history


class TestHistoryBlocks:
    """Every block is dated and reasoned; a move to adopt or pilot also carries evidence."""

    def test_a_complete_record_is_valid(self):
        t = entry(block("observe"), block("adopt", evidence="a run of the gate"))
        assert validate(t) == []

    def test_a_toml_date_is_a_date(self):
        # `date = 2026-08-01` without quotes parses to a date object; it is the same fact.
        assert validate(entry(block("observe", date=datetime.date(2026, 8, 1)))) == []

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "2026-8-1",
            "01/08/2026",
            "2026-02-30",
            20260801,
            " 2026-08-01 ",
            "2026-08-01\n",
        ],
    )
    def test_a_block_without_a_usable_date_is_an_error(self, bad):
        b = block("observe")
        if bad is None:
            del b["date"]
        else:
            b["date"] = bad
        errs = validate(entry(b))
        assert any("`date` in YYYY-MM-DD form" in e for e in errs), errs

    @pytest.mark.parametrize("bad", [None, "", "   ", 3])
    def test_a_block_without_a_reason_is_an_error(self, bad):
        b = block("observe")
        if bad is None:
            del b["reason"]
        else:
            b["reason"] = bad
        errs = validate(entry(b))
        assert any("`reason`" in e for e in errs), errs

    @pytest.mark.parametrize("ring", ["adopt", "pilot"])
    def test_a_move_to_adopt_or_pilot_without_evidence_is_an_error(self, ring):
        errs = validate(entry(block("observe"), block(ring)))
        assert any("`evidence`" in e and "block 2" in e for e in errs), errs

    def test_an_earlier_block_is_held_to_the_rule_too(self):
        # Not only the newest block: a record whose middle is unbacked is not a record.
        t = entry(block("pilot"), block("observe"))
        errs = validate(t)
        assert any("`evidence`" in e and "block 1" in e for e in errs), errs

    @pytest.mark.parametrize("ring", ["observe", "discard", "own"])
    def test_rings_that_claim_no_result_need_no_evidence(self, ring):
        assert validate(entry(block(ring))) == []

    def test_blank_evidence_is_no_evidence(self):
        errs = validate(entry(block("adopt", evidence="  ")))
        assert any("`evidence`" in e for e in errs), errs

    def test_history_that_is_not_a_list_of_tables_is_an_error(self):
        errs = validate({**BASE, "history": "observe"})
        assert any("[[history]]" in e for e in errs), errs


UNBACKED_ADOPT = """\
name = "ruff"
repo = "https://github.com/astral-sh/ruff"
axis = "lint-format"
artifact = "cli"
ring = "adopt"
license = "MIT"
visibility = "public"
note = "linter"

[telemetry]
match_command = ["ruff"]
since = "2026-08-01"

[[history]]
date = "2026-07-01"
ring = "observe"
reason = "a fixture"

[[history]]
ring = "adopt"
"""


class TestTheGateFailsAnUnbackedRingChange:
    def test_a_ring_change_with_no_date_reason_or_evidence_fails_the_gate(self, radar):
        radar.tool("ruff", UNBACKED_ADOPT)
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] ruff: history block 2 (ring 'adopt') needs `date`" in p.stdout
        assert "[FAIL] ruff: history block 2 (ring 'adopt') needs a non-empty `reason`" in p.stdout
        assert "[FAIL] ruff: history block 2 (ring 'adopt') needs non-empty `evidence`" in p.stdout

    def test_missing_evidence_is_a_fail_not_a_warning(self, radar):
        # Dated and reasoned, and still no evidence: a WARN alone would let this through.
        radar.tool("ruff", UNBACKED_ADOPT + 'date = "2026-08-01"\nreason = "a fixture"\n')
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "needs non-empty `evidence`" in p.stdout
        assert "transition carries no evidence" not in p.stdout
        assert "0 WARN" in p.stdout


# ---- names


class TestToolNames:
    """A name becomes a path segment and page text, so it has a fixed shape."""

    @pytest.mark.parametrize("name", ["ruff", "docs-server", "just", "widget_2", "a.b-c", "X1"])
    def test_ordinary_names_are_valid(self, name):
        assert validate(entry(block("observe"), name=name)) == []

    @pytest.mark.parametrize(
        "name",
        [
            "../x",
            "a/b",
            "a\b",
            ".hidden",
            "-dash",
            "has space",
            "x</script>",
            "ruff!",
            "é",
            "ruff\n",
        ],
    )
    def test_a_name_that_could_move_a_write_or_break_a_page_is_an_error(self, name):
        errs = validate(entry(block("observe"), name=name))
        assert any(e.startswith("bad name") for e in errs), errs

    def test_the_gate_fails_it(self, radar):
        radar.tool(
            "widget",
            'name = "../widget"\nrepo = "https://github.com/example/widget"\n'
            'axis = "lint-format"\nartifact = "cli"\nring = "observe"\n'
            'license = "MIT"\nvisibility = "public"\nnote = "n"\n\n'
            '[[history]]\ndate = "2026-08-01"\nring = "observe"\nreason = "a fixture"\n',
        )
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] ../widget: bad name" in p.stdout


class TestRedactAs:
    """A machine-local entry's placeholder replaces its name in tracked file names and
    names the field report's directory, so it has the shape of a name."""

    @pytest.mark.parametrize("placeholder", ["local-lib", "scoped_tool", "lib.v2"])
    def test_a_plain_placeholder_is_valid(self, placeholder):
        assert validate(entry(block("observe"), redact_as=placeholder)) == []

    @pytest.mark.parametrize("placeholder", ["../x", "../../escaped", "a/b", "a\\b", ".x", 3])
    def test_a_placeholder_that_could_move_a_write_is_an_error(self, placeholder):
        errs = validate(entry(block("observe"), redact_as=placeholder))
        assert any(e.startswith("bad redact_as") for e in errs), errs

    @pytest.mark.parametrize(
        ("placeholder", "fields"),
        [
            ("ruff-x", {}),
            ("Local-RUFF", {}),
            ("lint-lib", {"redact_also": ["lint"]}),
        ],
        ids=["name", "name-in-another-case", "redact-also-term"],
    )
    def test_a_placeholder_that_contains_a_name_it_replaces_is_an_error(self, placeholder, fields):
        # Redaction matches the name anywhere and in any case, so a placeholder that
        # contains it puts the name back into everything it is applied to.
        errs = validate(entry(block("observe"), redact_as=placeholder, **fields))
        assert any(e.startswith("bad redact_as") and "contains" in e for e in errs), errs

    def test_the_gate_fails_it(self, radar):
        local = radar.root / "tools.local"
        local.mkdir()
        (local / "local-widget.toml").write_text(
            'name = "local-widget"\nrepo = "https://example.invalid/w"\n'
            'axis = "lint-format"\nartifact = "cli"\nring = "observe"\n'
            'license = "MIT"\nvisibility = "private"\nnote = "n"\n'
            'redact_as = "../../escaped"\n',
            encoding="utf-8",
        )
        # Not --no-data-plane, which leaves tools.local/ unread.
        p = radar.gate()
        assert p.returncode == 1, p.stdout + p.stderr
        assert "bad redact_as" in p.stdout, p.stdout


# ---- registries


class TestRegistryKinds:
    """Exactly three kinds: pypi:<name>, npm:<name> and github:<owner>/<repo>."""

    @pytest.mark.parametrize(
        "registry",
        [
            "pypi:ruff",
            "pypi:docs_server",
            "npm:widget",
            "npm:@example/docs-server",
            "github:casey/just",
            "github:example/widget.py",
        ],
    )
    def test_the_three_kinds_are_valid(self, registry):
        assert validate(entry(block("observe"), registry=registry)) == []

    @pytest.mark.parametrize(
        "registry",
        [
            "crates:ripgrep",
            "ruff",
            "pypi:",
            "npm:",
            "github:just",
            "github:casey/",
            "github:/just",
            "github:casey/just/extra",
            "github:casey/..",
            "pypi:ruff\n",
            "npm:widget\n",
            "github:casey/just\n",
            "https://github.com/casey/just",
            "",
            42,
        ],
    )
    def test_anything_else_is_an_error(self, registry):
        errs = validate(entry(block("observe"), registry=registry))
        assert any("registry" in e for e in errs), errs

    def test_a_github_registry_satisfies_a_latest_version_policy(self):
        # Releases are the repository's tags, so `latest` has something to compare with.
        t = entry(block("observe"), registry="github:casey/just", version={"policy": "latest"})
        assert validate(t) == []


# ---- the name is the file


def named(name: str, ring: str = "observe") -> str:
    """An entry that passes the schema, called `name`."""
    return (
        f'name = "{name}"\nrepo = "https://example.invalid/{name}"\naxis = "mcp-servers"\n'
        f'ring = "{ring}"\nlicense = "MIT"\nvisibility = "public"\nnote = "a fixture"\n'
        f'artifact = "cli"\n\n[[history]]\ndate = "2026-08-01"\nring = "{ring}"\n'
        'reason = "a fixture"\n'
    )


class TestTheNameIsTheFile:
    """An entry's name is the stem of its file, in the form a file name takes, and no two
    entries claim one name. `radar add` writes entries that way; the gate holds a file
    written by hand, or added in a pull request, to the same rule."""

    def test_a_file_named_differently_from_its_entry_fails(self, radar):
        radar.tool("ruff", named("ruff"))
        radar.tool("mismatch", named("other"))
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] other: it lives in tools/mismatch.toml" in p.stdout, p.stdout
        assert "tools/other.toml" in p.stdout, p.stdout

    def test_a_name_not_in_file_form_fails(self, radar):
        radar.tool("MyTool", named("MyTool"))
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] MyTool:" in p.stdout and "'mytool'" in p.stdout, p.stdout

    def test_two_entries_that_claim_one_name_fail(self, radar):
        # A second file claiming an existing tool's name, with a different ring: without
        # the check both are rendered, and the tool sits in two ring tables at once.
        radar.tool("ruff", named("ruff"))
        radar.tool("dup", named("ruff", ring="discard"))
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        claimed = [ln for ln in p.stdout.splitlines() if "claimed by" in ln]
        assert claimed and "tools/dup.toml" in claimed[0] and "tools/ruff.toml" in claimed[0], (
            p.stdout
        )

    def test_entries_named_as_their_files_pass(self, radar):
        radar.tool("ruff", named("ruff"))
        radar.tool("my_tool.x", named("my_tool.x"))
        p = radar.gate("--no-data-plane")
        assert p.returncode == 0, p.stdout + p.stderr


# ---- [telemetry].since


class TestTelemetrySince:
    """`since` starts every report's window, so it is a real day, as a history date is."""

    @pytest.mark.parametrize("bad", ["2026-99-01", "2026-02-30", "26-08-01", "yesterday", 20260801])
    def test_a_since_that_is_not_a_real_day_is_a_schema_error(self, bad):
        t = entry(block("observe"), telemetry={"match_command": ["ruff"], "since": bad})
        assert any("[telemetry].since" in e for e in validate(t)), validate(t)

    def test_a_real_day_is_valid(self):
        t = entry(block("observe"), telemetry={"match_command": ["ruff"], "since": "2026-08-01"})
        assert validate(t) == []

    def test_the_gate_fails_it(self, radar):
        radar.tool(
            "ruff",
            named("ruff") + '\n[telemetry]\nmatch_command = ["ruff"]\nsince = "2026-99-01"\n',
        )
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "[FAIL] ruff: [telemetry].since" in p.stdout, p.stdout


# ---- history order


class TestHistoryOrder:
    """Blocks run oldest first, so the last block is the newest and its ring is the entry's."""

    def test_a_block_dated_before_the_one_above_it_is_a_schema_error(self):
        t = entry(
            block("pilot", date="2026-09-27", evidence="e"), block("observe", date="2020-01-01")
        )
        assert any("before the block above it" in e for e in validate(t)), validate(t)

    def test_blocks_on_one_day_or_in_order_are_valid(self):
        t = entry(block("observe", date="2026-08-01"), block("observe", date="2026-08-01"))
        assert validate(t) == []

    def test_the_gate_fails_blocks_out_of_order(self, radar):
        radar.tool(
            "ruff",
            named("ruff")
            + '\n[[history]]\ndate = "2020-01-01"\nring = "observe"\nreason = "a fixture"\n',
        )
        p = radar.gate("--no-data-plane")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "before the block above it" in p.stdout, p.stdout
