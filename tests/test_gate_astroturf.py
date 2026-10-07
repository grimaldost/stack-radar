"""The astroturf check (downloads/month vs stars) must not trust a downloads figure the
entry's own `tools/*.toml` no longer backs with a `registry`.

A snapshot can carry forward a downloads figure measured under a `registry` the entry no
longer declares: the snapshot is refreshed separately and can be older than the entry.
With no download registry declared, the figure is not evidence about the entry, so the
astroturf check does not FLAG it and says instead that it could not run, the way the gate
reports every other check it skips.
"""

from __future__ import annotations

WIDGET = """\
name = "widget"
repo = "https://github.com/example/widget"
axis = "lint-format"
artifact = "cli"
ring = "observe"
license = "MIT"
visibility = "public"
note = "a high-star tool with a low download figure in the snapshot"

[telemetry]
match_command = ["widget"]
since = "2026-09-01"

[[history]]
date = "2026-09-01"
ring = "observe"
reason = "a fixture"
evidence = "e"
"""

WIDGET_WITH_REGISTRY = WIDGET.replace(
    'note = "a high-star tool', 'registry = "npm:widget"\nnote = "a high-star tool'
)

# stars > ASTROTURF_MIN_STARS (5000) and downloads_month well under 5% of stars.
SNAPSHOT_ENTRY = {
    "stars": 30000,
    "downloads_month": 250,
    "downloads_source": "npm:a-different-package-entirely",
    "downloads_stale": True,
    "downloads_as_of": "2026-08-01",
}


class TestNoRegistryNoTrust:
    def test_a_carried_figure_with_no_registry_is_not_flagged(self, radar):
        radar.tool("widget", WIDGET)  # declares no `registry`
        radar.snapshot("2026-09-19", {"widget": SNAPSHOT_ENTRY})

        p = radar.gate()

        assert "[FLAG] widget" not in p.stdout, p.stdout

    def test_a_carried_figure_with_no_registry_says_the_check_could_not_run(self, radar):
        radar.tool("widget", WIDGET)
        radar.snapshot("2026-09-19", {"widget": SNAPSHOT_ENTRY})

        p = radar.gate()

        assert "widget" in p.stdout
        line = next(ln for ln in p.stdout.splitlines() if "widget" in ln and "250" in ln)
        assert line.startswith("[NOTE]"), line
        assert "cannot run" in line or "could not run" in line

    def test_the_same_figure_still_flags_once_a_registry_backs_it(self, radar):
        radar.tool("widget", WIDGET_WITH_REGISTRY)
        entry = dict(SNAPSHOT_ENTRY)
        entry["downloads_source"] = "npm:widget"
        entry.pop("downloads_stale")
        entry.pop("downloads_as_of")
        radar.snapshot("2026-09-19", {"widget": entry})

        p = radar.gate()

        assert "[FLAG] widget: astroturf signature" in p.stdout, p.stdout

    def test_a_registered_entry_with_no_figure_at_all_still_flags_as_before(self, radar):
        radar.tool("widget", WIDGET_WITH_REGISTRY)
        radar.snapshot("2026-09-19", {"widget": {"stars": 30000}})

        p = radar.gate()

        assert "[FLAG] widget" in p.stdout
        assert "NO download figure" in p.stdout


WIDGET_ON_GITHUB = WIDGET.replace(
    'note = "a high-star tool', 'registry = "github:example/widget"\nnote = "a high-star tool'
)


class TestGithubReleasesCarryNoDownloadCount:
    """`github:<owner>/<repo>` names releases (the repository's tags) and no download
    figure, so for the download and astroturf checks it declares no download registry.
    Expecting a number that cannot exist would flag every such entry forever."""

    def test_a_high_star_github_entry_with_no_figure_is_not_flagged(self, radar):
        radar.tool("widget", WIDGET_ON_GITHUB)
        radar.snapshot("2026-09-19", {"widget": {"stars": 30000}})

        p = radar.gate()

        assert p.returncode == 0, p.stdout + p.stderr
        assert "[FLAG] widget" not in p.stdout, p.stdout
        assert "0 astroturf FLAG" in p.stdout

    def test_a_carried_figure_under_github_is_not_evidence(self, radar):
        radar.tool("widget", WIDGET_ON_GITHUB)
        radar.snapshot("2026-09-19", {"widget": SNAPSHOT_ENTRY})

        p = radar.gate()

        assert "[FLAG] widget" not in p.stdout, p.stdout
        line = next(ln for ln in p.stdout.splitlines() if "widget" in ln and "250" in ln)
        assert line.startswith("[NOTE]"), line
        assert "cannot run" in line
