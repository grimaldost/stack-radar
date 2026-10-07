"""`radar render` writes tracked, publishable pages from catalogue data.

A catalogue takes pull requests, so every name, note and title is untrusted text by the
time it reaches `docs/radar/index.html` or a Markdown table. These tests plant hostile
values in a miniature catalogue and read what render wrote, byte for byte.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SCRIPT_BREAKER = "x</script><script>alert(document.domain)</script>"


def entry(
    name: str,
    *,
    note: str = "a note",
    visibility: str = "public",
    repo: str = "https://example.invalid/widget",
    ring: str = "adopt",
) -> str:
    return (
        f"name = {json.dumps(name)}\n"
        f"repo = {json.dumps(repo)}\n"
        'axis = "lint-format"\n'
        'artifact = "cli"\n'
        f'ring = "{ring}"\n'
        'license = "MIT"\n'
        f'visibility = "{visibility}"\n'
        f"note = {json.dumps(note)}\n"
        "\n[[history]]\n"
        'date = "2026-08-01"\n'
        f'ring = "{ring}"\n'
        f"reason = {json.dumps(note)}\n"
        'evidence = "e"\n'
    )


def set_title(radar, title: str) -> None:
    marker = radar.root / "radar.toml"
    text = marker.read_text(encoding="utf-8")
    marker.write_text(
        re.sub(r'^title = ".*"$', f"title = {json.dumps(title)}", text, flags=re.M),
        encoding="utf-8",
    )


def render(radar, *args: str) -> None:
    p = radar.script("render", *args)
    assert p.returncode == 0, p.stdout + p.stderr


def page_data(html_text: str) -> list[dict]:
    """The DATA array exactly as the page's JavaScript would parse it."""
    m = re.search(r"const DATA=(.*?);\nconst RINGS", html_text, flags=re.S)
    assert m, "no DATA slot in the page"
    return json.loads(m.group(1))


def md_cells(row: str) -> list[str]:
    """A Markdown table row split the way GFM splits it: on every unescaped `|`."""
    return re.split(r"(?<!\\)\|", row.strip())[1:-1]


class TestTheRadarPage:
    def test_a_name_cannot_close_the_script_block(self, radar):
        radar.tool("evil", entry(SCRIPT_BREAKER))
        render(radar)
        page = (radar.root / "docs" / "radar" / "index.html").read_text(encoding="utf-8")
        assert SCRIPT_BREAKER not in page
        # The template's own closing tag is the only one; nothing a name carried.
        assert page.count("</script>") == 1
        assert page.count("<script>") == 1
        # And the value is not mangled: the page's JavaScript reads the name back exactly.
        assert [d["name"] for d in page_data(page)] == [SCRIPT_BREAKER]

    def test_the_title_is_escaped(self, radar):
        set_title(radar, "tools <b>&</b> more")
        radar.tool("ruff", entry("ruff"))
        render(radar)
        page = (radar.root / "docs" / "radar" / "index.html").read_text(encoding="utf-8")
        assert "<title>tools &lt;b&gt;&amp;&lt;/b&gt; more</title>" in page
        assert "<b>" not in page

    def test_a_value_carrying_a_slot_name_is_not_substituted_again(self, radar):
        # Chained `replace` calls substituted each slot in turn, so a name spelled like a
        # later slot had the axes list pasted into the middle of the data.
        radar.tool("slot", entry("__AXES__"))
        render(radar)
        page = (radar.root / "docs" / "radar" / "index.html").read_text(encoding="utf-8")
        assert [d["name"] for d in page_data(page)] == ["__AXES__"]

    def test_the_public_page_is_escaped_too(self, radar):
        radar.tool("evil", entry(SCRIPT_BREAKER))
        render(radar, "--public")
        page = (radar.root / "docs" / "radar" / "public.html").read_text(encoding="utf-8")
        assert SCRIPT_BREAKER not in page
        assert page.count("</script>") == 1


class TestTheMarkdownTables:
    NAME = "x|y<img src=z>"
    NOTE = "pipes | and <script>alert(1)</script> and `<kept>` code"

    def _readme(self, radar) -> str:
        radar.tool("odd", entry(self.NAME, note=self.NOTE))
        render(radar, "--public")
        return (radar.root / "README.md").read_text(encoding="utf-8")

    def test_a_name_or_note_cannot_add_a_table_column(self, radar):
        readme = self._readme(radar)
        header = next(ln for ln in readme.splitlines() if ln.startswith("| tool | kind"))
        row = next(ln for ln in readme.splitlines() if ln.startswith("| [x"))
        assert len(md_cells(row)) == len(md_cells(header)), row

    def test_a_name_or_note_cannot_inject_html(self, radar):
        readme = self._readme(radar)
        outside_code = re.sub(r"`[^`]*`", "", readme)
        assert "<img" not in outside_code
        assert "<script" not in outside_code
        # A code span the author wrote stays as written: HTML inside backticks is text.
        assert "`<kept>`" in readme

    # CommonMark pairs backtick runs of equal length and reads an unmatched run as
    # literal backticks, so a run of two or three backticks does not open a code span
    # that a single backtick later closes. These notes are built so that pairing single
    # backticks naively would leave the HTML after them unescaped.
    @pytest.mark.parametrize(
        ("note", "tag"),
        [
            ("```<img src=x onerror=alert(1)>`", "<img"),
            ("``<b>x</b>`", "<b>"),
            ("`````<b>y</b>`", "<b>"),
        ],
    )
    def test_an_unmatched_backtick_run_does_not_shield_html(self, radar, note, tag):
        radar.tool("ticks", entry("ticks", note=note))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        assert tag not in readme

    def test_an_escaped_backtick_does_not_open_a_code_span(self, radar):
        # A backslash before a backtick makes it literal, so the HTML after it would
        # render. Escaping the backslash itself lets the backtick open the span again.
        radar.tool("slash", entry("slash", note="\\`<i>x</i>`"))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        assert "\\\\`<i>x</i>`" in readme

    def test_the_transitions_table_is_escaped(self, radar):
        readme = self._readme(radar)
        row = next(ln for ln in readme.splitlines() if ln.startswith("| 2026-08-01 |"))
        assert len(md_cells(row)) == 4, row

    def test_the_public_projection_is_escaped(self, radar):
        self._readme(radar)
        public = (radar.root / "PUBLIC.md").read_text(encoding="utf-8")
        row = next(ln for ln in public.splitlines() if ln.startswith("| [x"))
        assert len(md_cells(row)) == 4, row
        assert "<img" not in public

    def test_a_repo_url_cannot_end_the_link_early(self, radar):
        radar.tool("odd", entry("widget", repo="https://example.invalid/a b)c|d"))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        assert "[widget](https://example.invalid/a%20b%29c%7Cd)" in readme

    def test_a_note_cannot_carry_a_link_or_an_image(self, radar):
        note = "[the docs](javascript:alert(1)) ![x](https://a.example/pixel.png) done"
        radar.tool("linky", entry("linky", note=note, visibility="private"))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        # Escaped brackets: the text reads as written and renders as text.
        assert "\\[the docs\\](javascript:alert(1))" in readme, readme
        assert "!\\[x\\](https://a.example/pixel.png)" in readme, readme

    def test_only_an_http_repo_becomes_a_link(self, radar):
        radar.tool(
            "evil", entry("evil", repo="javascript:alert(document.domain)", visibility="private")
        )
        radar.tool("ssh", entry("ssh", repo="git@github.com:example/ssh.git"))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        assert "](javascript:" not in readme, readme
        assert "| evil (`javascript:alert(document.domain)`) |" in readme, readme
        assert "| ssh (`git@github.com:example/ssh.git`) |" in readme, readme

    def test_an_ordinary_entry_renders_as_before(self, radar):
        radar.tool("ruff", entry("docs-server", note="serves `docs` locally"))
        render(radar)
        readme = (radar.root / "README.md").read_text(encoding="utf-8")
        assert "| [docs-server](https://example.invalid/widget) | cli |" in readme
        assert "serves `docs` locally |" in readme
        assert "`docs-server` (adopt)" in readme


class TestLineEndings:
    """Every file render writes uses LF on every OS.

    A catalogue normalises text to LF through .gitattributes, so a render that writes
    CRLF on Windows leaves every generated file showing as modified until it is staged,
    even when nothing in it changed.
    """

    @pytest.mark.parametrize(
        "rel",
        ["README.md", "PUBLIC.md", "docs/radar/index.html", "docs/radar/public.html"],
    )
    def test_no_carriage_returns(self, radar, rel):
        radar.tool("ruff", entry("ruff"))
        render(radar, "--public")
        data = Path(radar.root / rel).read_bytes()
        assert data, rel
        assert b"\r" not in data, rel


@pytest.mark.parametrize(
    ("name", "written"), [("_x_", r"\_x\_"), ("__init__", r"\_\_init\_\_"), ("a_b", r"a\_b")]
)
def test_an_underscore_in_a_name_is_not_emphasis(name: str, written: str):
    # `_x_` renders italic and `__init__` bold unless the underscores are escaped; a name
    # is an identifier, so nothing in it is meant as Markdown.
    from stack_radar.render import md_name

    assert md_name(name) == written


def test_the_workflow_line_states_the_evidence_rule_as_the_gate_applies_it(radar):
    # Every transition needs a date and a reason; only a move to adopt or pilot needs
    # evidence, which is the rule the gate enforces.
    radar.tool("widget", entry("widget"))
    render(radar)
    readme = (radar.root / "README.md").read_text(encoding="utf-8")
    flat = " ".join(readme.split())
    assert "a move to adopt or pilot also cites evidence" in flat, readme
    assert "ring transitions require a dated history entry with evidence" not in flat
