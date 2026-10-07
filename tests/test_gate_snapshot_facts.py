"""What the gate says about an entry's repository facts, and about their absence.

`radar snapshot` records a failed repository lookup as `repo_status` (the HTTP status, or
"error") and keeps the facts measured earlier, marked `repo_stale` with the date they were
measured (`repo_as_of`). It never writes tools.local/ entries, because snapshots/ is
tracked. The gate reads all three cases: an entry with something to measure and no facts
is unmeasured rather than clean, a repository that answers 404 at a ring that claims use
is a WARN, and every message resting on a carried fact says since when.
"""

from __future__ import annotations

ENTRY = """\
name = "widget"
repo = "https://github.com/example/widget"
axis = "lint-format"
artifact = "cli"
ring = "{ring}"
license = "MIT"
visibility = "public"
note = "a synthetic entry"
{extra}
[telemetry]
match_command = ["widget"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "{ring}"
reason = "a fixture"
evidence = "e"
"""

NL = chr(10)

PILOT_EXIT = """
[pilot_exit]
adopt_if = "it is used"
decline_if = "it is not"
"""


def widget(ring: str = "adopt", *, repo: str | None = None) -> str:
    text = ENTRY.format(ring=ring, extra=PILOT_EXIT if ring == "pilot" else "")
    if repo is not None:
        text = text.replace("https://github.com/example/widget", repo)
    return text


def with_github_registry(text: str) -> str:
    return text.replace("[telemetry]", 'registry = "github:example/widget"' + NL + "[telemetry]")


def lines_about(stdout: str, name: str = "widget") -> list[str]:
    return [ln for ln in stdout.splitlines() if f"] {name}:" in ln]


class TestAnEntryWithNoFactsIsUnmeasured:
    def test_a_tracked_entry_missing_from_the_snapshot_says_unmeasured(self, radar):
        radar.tool("widget", widget())
        radar.snapshot("2026-09-19", {"other": {"stars": 1}})
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        found = lines_about(p.stdout)
        assert any(
            ln.startswith("[NOTE] widget: no snapshot facts - unmeasured") for ln in found
        ), p.stdout
        assert "`radar snapshot`" in "\n".join(found)

    def test_a_machine_local_entry_says_why_it_is_never_measured(self, radar):
        local = radar.root / "tools.local"
        local.mkdir()
        (local / "widget.toml").write_text(widget(), encoding="utf-8")
        radar.snapshot("2026-09-19", {"other": {"stars": 1}})
        p = radar.gate()
        assert (
            "[NOTE] widget: no snapshot facts - unmeasured (machine-local entries are never "
            "written to the tracked snapshot)"
        ) in p.stdout, p.stdout

    def test_an_entry_with_nothing_to_measure_stays_silent(self, radar):
        radar.tool("widget", widget(repo="https://example.invalid/widget"))
        radar.snapshot("2026-09-19", {"other": {"stars": 1}})
        p = radar.gate()
        assert "unmeasured" not in p.stdout, p.stdout

    def test_a_github_registry_alone_is_not_something_a_snapshot_measures(self, radar):
        # `radar snapshot` records no download figure for a `github:` registry, and a
        # repository outside github.com is never looked up, so a NOTE asking for a
        # snapshot here could never be cleared by one.
        text = widget(repo="https://gitlab.com/example/widget")
        radar.tool("widget", with_github_registry(text))
        radar.snapshot("2026-09-19", {"other": {"stars": 1}})
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "unmeasured" not in p.stdout, p.stdout


class TestTheRepositoryStatusIsReported:
    def test_a_404_at_a_ring_that_claims_use_is_a_warn(self, radar):
        radar.tool("widget", widget("adopt"))
        radar.snapshot(
            "2026-09-19", {"widget": {"repo_status": 404, "repo_error": "gh: Not Found"}}
        )
        p = radar.gate()
        assert p.returncode == 0, p.stdout + p.stderr
        assert "[WARN] widget: upstream repository not found (HTTP 404) at ring adopt" in (p.stdout)
        assert "1 WARN" in p.stdout

    def test_a_404_at_observe_is_a_note(self, radar):
        radar.tool("widget", widget("observe"))
        radar.snapshot("2026-09-19", {"widget": {"repo_status": 404, "repo_error": "gone"}})
        p = radar.gate()
        assert "[NOTE] widget: repository lookup failed (404: gone)" in p.stdout, p.stdout
        assert "0 WARN" in p.stdout

    def test_any_other_failure_is_a_note(self, radar):
        radar.tool("widget", widget("adopt"))
        radar.snapshot("2026-09-19", {"widget": {"repo_status": "error", "repo_error": "x"}})
        p = radar.gate()
        assert "[NOTE] widget: repository lookup failed (error: x)" in p.stdout, p.stdout
        assert "0 WARN" in p.stdout


class TestCarriedFactsSaySinceWhen:
    def test_an_old_push_date_that_was_carried_names_its_date(self, radar):
        radar.tool("widget", widget("adopt"))
        radar.snapshot(
            "2026-09-19",
            {
                "widget": {
                    "pushed": "2020-01-01",
                    "repo_stale": True,
                    "repo_as_of": "2026-08-01",
                    "repo_status": "error",
                    "repo_error": "timeout",
                }
            },
        )
        p = radar.gate()
        warn = next(ln for ln in lines_about(p.stdout) if ln.startswith("[WARN]"))
        assert "last push" in warn
        assert "push date carried from 2026-08-01" in warn, warn

    def test_a_fresh_push_date_says_nothing_about_carrying(self, radar):
        radar.tool("widget", widget("adopt"))
        radar.snapshot("2026-09-19", {"widget": {"pushed": "2020-01-01"}})
        p = radar.gate()
        warn = next(ln for ln in lines_about(p.stdout) if ln.startswith("[WARN]"))
        assert "carried" not in warn, warn


def test_with_no_snapshot_at_all_one_line_says_so(radar):
    radar.tool("widget", widget())
    p = radar.gate()
    assert p.returncode == 0, p.stdout + p.stderr
    notes = [ln for ln in p.stdout.splitlines() if "unmeasured" in ln]
    assert notes == [
        "[NOTE] no snapshot yet - 1 entry with a GitHub repository or a download "
        "registry unmeasured; `radar snapshot` writes one"
    ], p.stdout


def test_with_no_snapshot_machine_local_entries_are_counted_apart(radar):
    radar.tool("widget", widget())
    local = radar.root / "tools.local"
    local.mkdir()
    (local / "gadget.toml").write_text(
        widget().replace('name = "widget"', 'name = "gadget"'), encoding="utf-8"
    )
    p = radar.gate()
    notes = [ln for ln in p.stdout.splitlines() if "unmeasured" in ln]
    assert notes == [
        "[NOTE] no snapshot yet - 1 entry with a GitHub repository or a download "
        "registry unmeasured; `radar snapshot` writes one",
        "[NOTE] 1 machine-local entry with a GitHub repository or a download registry "
        "unmeasured - machine-local entries are never written to the tracked snapshot",
    ], p.stdout


def test_with_no_snapshot_a_github_registry_alone_is_not_counted(radar):
    text = widget(repo="https://gitlab.com/example/widget")
    radar.tool("widget", with_github_registry(text))
    p = radar.gate()
    assert p.returncode == 0, p.stdout + p.stderr
    assert "unmeasured" not in p.stdout, p.stdout
