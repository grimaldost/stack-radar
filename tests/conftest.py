"""A synthetic radar root, so the field-report tests never read the real machine.

The commands resolve everything from the data root, which is the nearest directory above
the working directory carrying a `radar.toml`. So the cheapest hermetic fixture is a
whole miniature radar in tmp_path: the marker, a tools/ directory with the entries the
test declares, an environments/ profile pointing at a synthetic claude_home, and
transcripts written by hand. Every command runs with `cwd` at that root, so the search
finds the fixture's marker and nothing else. Nothing here touches the operator's real
transcripts or a tracked `field/` directory.

THE FIXTURE HOLDS NO CODE. The engine is reached by name - `python -m
stack_radar.<module>` - and a data root holds no code at all, so the fixture contains
exactly what a real data root contains. A test that needed the modules present inside the
root would be testing an arrangement no operator has.

Commands run through `engine_run.run_engine`: in this process by default, and as a real
subprocess when `RADAR_ENGINE_SRC` is set. That variable selects WHICH engine runs, for the
red proofs: point it at a `src/` taken from an earlier commit and the same tests run
against that code. It is
one variable because the modules import each other as one package, so a pre-change `gate`
beside a post-change `radar_lib` is not a state the engine can be in, and an override that
could produce it would prove a failure no operator could meet.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from engine_run import call_main, run_engine
from stack_radar import __version__
from stack_radar.init import compatible_range

REPO = Path(__file__).resolve().parent.parent
ENGINE_SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))

# A miniature radar is still a radar: it has the two version sites, so gate.py's
# repo-only half has something real to read. Kept minimal and in agreement, so a test
# that wants a version failure creates it explicitly rather than inheriting one.
MINI_PYPROJECT = """\
[project]
name = "mini-radar"
version = "1.0.0"
requires-python = ">=3.11"
"""
MINI_CHANGELOG = """\
# Changelog

## [1.0.0] - 2000-01-01

The fixture's own release.
"""

# A miniature radar needs the MARKER above all: without it a command run inside the
# fixture walks past tmp_path and either finds nothing (DataRootNotFound) or, worse on a
# machine where the temporary directory happens to live under a real root, finds that one.
#
# `[invariant].needles` is not padding either. The gate reads its needles from
# here, and a fixture with no needles would leave the invariant tests running against a
# scan that cannot fail - a whole class of test passing because the thing under test was
# switched off. It carries the fixture's own name, which the tests plant in their
# synthetic data plane.
#
# `requires_framework` is derived from the ENGINE's own version (`compatible_range`, the
# same function `radar init` calls), not hand-pinned: the check compares against
# `stack_radar.__version__`, so a marker that pinned a range by hand would fail every test
# here the day the engine cut a minor release, on a message about versions rather than
# about whatever the test actually meant to exercise.
MINI_MARKER = f"""\
schema = "radar-data/v1"
title = "mini-radar"
default_environment = "default"
requires_framework = "{compatible_range(__version__)}"

[invariant]
needles = ["mini-radar"]
"""


# Every field of a discovery table row (discovery table format, version 1), at the value it
# takes on a row with no such event: a count is 0, a name list `[]`, a map `{}`, and the two
# listing maxima null. Written from the format's own statement of its types, NOT read from
# the engine's reader: a fixture that asked the reader what a row looks like could never
# catch the reader being wrong about it.
DISCOVERY_FORMAT = "discovery-table/1"
DISCOVERY_EMPTY: dict[str, object] = {
    # identity
    "session_id": "",
    "slug": "C--Users-x-Documents-p",
    "date": "",
    "first_timestamp": "",
    "last_timestamp": "",
    "cli_versions": [],
    "user_prompts": 0,
    "assistant_messages": 0,
    "untimed_records": 0,
    # skill listing
    "listing_count": 0,
    "listing_initial_count": 0,
    "listing_skill_count_max": None,
    "listing_chars_max": None,
    "listing_names": [],
    "listing_name_only": [],
    "listing_side_count": 0,
    # deferred tools and MCP
    "deferred_delta_count": 0,
    "deferred_added_names": [],
    "deferred_removed_names": [],
    "deferred_surfaced_names": [],
    "mcp_servers_pending": [],
    "mcp_servers_failed": [],
    "mcp_servers_needs_auth": [],
    "mcp_instructions_count": 0,
    "mcp_instructions_servers": [],
    # hooks
    "hook_success": {},
    "hook_additional_context": {},
    "hook_errors": {},
    # router
    "router_injections": 0,
    "router_named": {},
    "router_unparsed": 0,
    "router_unmatched": 0,
    "router_named_loaded_after": 0,
    # dispatch line
    "dispatch_lines": 0,
    "dispatch_named": [],
    # ToolSearch
    "toolsearch_calls": 0,
    "toolsearch_calls_side": 0,
    "toolsearch_select": 0,
    "toolsearch_select_side": 0,
    "toolsearch_keyword": 0,
    "toolsearch_keyword_side": 0,
    "toolsearch_selected": {},
    "toolsearch_selected_side": {},
    "toolsearch_select_unmatched": 0,
    # Skill loads
    "skills_loaded": {},
    "skills_loaded_side": {},
    "skills_loaded_unmatched": 0,
    # compactions
    "compact_boundaries": 0,
    "compact_boundary_at": [],
    # drift sentinels
    "attachment_types": {},
    "discovery_skipped": 0,
}


def discovery_row(session_id: str, date: str, **fields: object) -> dict:
    """One discovery table row: `session_id` on the UTC date `date`, every other field at
    its empty value unless given.

    A field the format does not have is refused, so a misspelt keyword cannot plant a
    field no reader looks at and leave the test asserting about an empty row. A name list
    is sorted, as the format writes it. Every row has both timestamps, inside its date.
    """
    unknown = sorted(set(fields) - set(DISCOVERY_EMPTY))
    if unknown:
        raise KeyError(f"not a field of {DISCOVERY_FORMAT}: {', '.join(unknown)}")
    row = copy.deepcopy(DISCOVERY_EMPTY)
    row.update(
        session_id=session_id,
        date=date,
        first_timestamp=f"{date}T09:00:00Z",
        last_timestamp=f"{date}T17:00:00Z",
    )
    for key, value in fields.items():
        row[key] = sorted(value) if isinstance(value, list) else value
    return row


def tool_use(tid: str, name: str, ts: str, **inp) -> dict:
    """One assistant record carrying a single tool_use block, as transcripts write it."""
    return {
        "type": "assistant",
        "timestamp": ts,
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
        },
    }


def tool_result(tid: str, ts: str, *, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "ok"}
            ],
        },
    }


def bash(tid: str, ts: str, command: str) -> dict:
    return tool_use(tid, "Bash", ts, command=command)


def note(ts: str, text: str = "nothing interesting") -> dict:
    """A record with no tool call - transcript filler, so a session has a time span."""
    return {
        "type": "user",
        "timestamp": ts,
        "uuid": str(uuid.uuid4()),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


class Radar:
    """A miniature radar root. Every path it returns is inside tmp_path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "tools").mkdir(parents=True, exist_ok=True)
        (root / "environments").mkdir(exist_ok=True)
        self.home = root / "home"
        (self.home / "projects").mkdir(parents=True, exist_ok=True)
        self.transcript_index = root / "transcript-index"
        self.discovery_table = self.transcript_index / "derived" / "discovery.jsonl"
        # The home directory the subprocesses see, or None to inherit the machine's. The
        # field report redacts the home out of what it writes, so a test of that has to
        # choose the home - otherwise it would be asserting about the real account, which
        # differs per machine and is absent on a runner.
        self.user_home: Path | None = None
        (root / "radar.toml").write_text(MINI_MARKER, encoding="utf-8")
        (root / "pyproject.toml").write_text(MINI_PYPROJECT, encoding="utf-8")
        (root / "CHANGELOG.md").write_text(MINI_CHANGELOG, encoding="utf-8")
        self.write_environment()

    # ------------------------------------------------------------------ inputs
    def write_environment(self, *, with_index: bool = False, with_discovery: bool = False) -> None:
        lines = [
            'name = "default"',
            'rings = ["own", "adopt", "pilot"]',
            "",
            "[paths]",
            f'claude_home = "{self.home.as_posix()}"',
        ]
        if with_index:
            lines.append(f'transcript_index = "{self.transcript_index.as_posix()}"')
        if with_discovery:
            lines.append(f'discovery_index = "{self.discovery_table.as_posix()}"')
        (self.root / "environments" / "default.toml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def tool(self, name: str, toml: str) -> None:
        (self.root / "tools" / f"{name}.toml").write_text(toml, encoding="utf-8")

    def session(
        self,
        session_id: str,
        records: list[dict],
        *,
        slug: str = "C--Users-x-Documents-p",
        sub: str | None = None,
        mtime: float | None = None,
    ) -> Path:
        """Write one transcript. `sub` puts it in the session's sidechain subtree."""
        base = self.home / "projects" / slug
        path = base / f"{session_id}.jsonl" if sub is None else base / session_id / sub
        path.parent.mkdir(parents=True, exist_ok=True)
        # COMPACT, like the real thing. Claude Code writes `"tool_use_id":"toolu_..."`
        # with no spaces, and field_report's fast path for uninteresting lines is a
        # regex over exactly that shape - a fixture with pretty-printed separators
        # would exercise a code path the real corpus never takes.
        body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        path.write_text(body, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def index_rows(self, rows: list[dict], *, generated_at: str) -> None:
        """A synthetic invocation table (invocation table format, version 1)."""
        d = self.transcript_index / "derived"
        d.mkdir(parents=True, exist_ok=True)
        (d / "invocations.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        (d / "derive.json").write_text(
            json.dumps({"generated_at": generated_at}) + "\n", encoding="utf-8"
        )

    def discovery_rows(
        self,
        rows: list[dict],
        *,
        generated_at: str,
        records_end: str | None,
        discovery_format: str | None = DISCOVERY_FORMAT,
        derive: bool = True,
    ) -> Path:
        """A synthetic discovery table (discovery table format, version 1) and its
        `derive.json`, in the directory that holds the invocation table's two files.

        `records_end` or `discovery_format` given as None leaves that key out of
        `derive.json`, and `derive=False` writes no `derive.json` at all: the absences a
        reader has to name. `derive.json` is written whole, so it replaces one written by
        `index_rows`. Rows are written compact, one per line, as a writer of the format
        does. Returns the table's path.
        """
        self.discovery_table.parent.mkdir(parents=True, exist_ok=True)
        self.discovery_table.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8"
        )
        meta = self.discovery_table.parent / "derive.json"
        if derive:
            doc: dict[str, object] = {"generated_at": generated_at}
            if discovery_format is not None:
                doc["discovery_format"] = discovery_format
            if records_end is not None:
                doc["records_end"] = records_end
            doc["result"] = {"discovery_rows": len(rows)}
            meta.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        elif meta.exists():
            meta.unlink()
        return self.discovery_table

    def snapshot(self, name: str, entries: dict) -> Path:
        """Write `snapshots/<name>.json` by hand, the way `gate.py` reads it - never
        through `snapshot.py` itself, which hits the network. `latest_snapshot()` picks
        the lexicographically-last filename, so a test that wants ITS file read chooses
        one that sorts last among whatever else is in the directory."""
        d = self.root / "snapshots"
        d.mkdir(exist_ok=True)
        path = d / f"{name}.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ running
    def script(self, name: str, *args: str) -> subprocess.CompletedProcess:
        # `cwd` at the fixture's root is what makes the marker search land here, and
        # dropping RADAR_DATA_ROOT is what keeps it landing here: the resolver reads that
        # variable BEFORE it searches, so a machine that exports it would point every one
        # of these subprocesses at a real tree and the whole fixture would quietly stop
        # being hermetic.
        #
        # `-m`, so the module is reached by NAME and imports its siblings as a package.
        # `RADAR_ENGINE_SRC` is put on PYTHONPATH ahead of whatever is installed, which is
        # what lets a red proof run these same tests against an engine extracted from an
        # earlier commit without installing it; with it set, `run_engine` starts a real
        # subprocess (see tests/engine_run.py).
        return run_engine(name, args, cwd=self.root, env=self.env())

    def env(self) -> dict[str, str]:
        env = {**os.environ}
        env.pop("RADAR_DATA_ROOT", None)
        if self.user_home is not None:
            # Both variables, because the interpreter reads a different one per platform:
            # USERPROFILE on Windows, HOME everywhere else.
            env["HOME"] = env["USERPROFILE"] = str(self.user_home)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ENGINE_SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        return env

    def call(self, main: Callable[[], object], *args: str) -> subprocess.CompletedProcess:
        """Call an imported command's `main` in this process, at the fixture's root.

        For a test that has to change a module constant first (monkeypatch), which a
        separate process would not see. It always runs the imported engine, whatever
        `RADAR_ENGINE_SRC` says."""
        return call_main(main, ["radar", *args], cwd=self.root, env=self.env())

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return self.script("field_report", *args)

    def gate(self, *args: str) -> subprocess.CompletedProcess:
        return self.script("gate", *args)

    def report(self, tool: str, *args: str) -> dict:
        """Run for one tool and return the JSON document it prints."""
        p = self.run(tool, "--json", *args)
        assert p.stdout.strip(), f"no JSON on stdout; stderr:\n{p.stderr}"
        return json.loads(p.stdout)


@pytest.fixture
def radar(tmp_path: Path) -> Radar:
    return Radar(tmp_path / "radar")
