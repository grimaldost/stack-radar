"""Render README.md, the radar page, and (with --public) the filtered public projection."""

from __future__ import annotations

import argparse
import datetime
import html
import json
import re
import urllib.parse

from .paths import write_error, write_inside
from .radar_lib import (
    ARTIFACTS,
    NO_REPO_SENTINEL,
    RINGS,
    begin_command,
    catalogue_axes,
    load_marker,
    load_tools,
)

# What each artifact class actually commits you to. Rendered alongside the grouping so
# the taxonomy carries its consequence rather than being a label to sort by.
ARTIFACT_MEANS = {
    "cc-plugin": "loads into the agent: skills, agents, commands, hooks",
    "mcp-server": "tool surface in the agent's context - costs tokens every session",
    "cc-lsp": "language server, driven natively by the harness; no tool surface",
    "cli": "a command in a shell or CI; no context cost, easy to audit",
    "python-lib": "imported by project code, so it becomes a runtime dependency",
    "service": "a server and UI somebody has to operate, authenticate and patch",
    "standard": "nothing to install - a format to target",
    "corpus": "authored knowledge, not executable",
    "unknown": "never established; rejected on evidence before its nature mattered",
}

# The fallback when a catalogue declares no `[render].boundary_note` (`radar init` seeds
# new catalogues with this). No decision-record citations here: a catalogue's decisions
# are its own, not a claim the engine can make on a stranger's behalf. A catalogue that
# already has a write-up of its own boundary keeps it in its own radar.toml, verbatim.
DEFAULT_BOUNDARY_NOTE = (
    "Boundary: this radar owns adoption state (rings, transitions, evidence) and the "
    "feedback registration it renders into the data plane. The stack it governs keeps "
    "its own configuration elsewhere, and neither file points back into the other."
)

HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:14px system-ui;margin:20px;display:flex;gap:24px;flex-wrap:wrap}svg{flex:none}
h3{margin:8px 0 2px}ul{margin:2px 0;padding-left:18px}li{line-height:1.5}</style></head>
<body><svg id="r" width="760" height="760"></svg><div id="legend"></div>
<script>
const DATA=__DATA__;
const RINGS=["own","adopt","pilot","observe","discard"];
const AXES=__AXES__;
const COLORS={own:"#6b5b95",adopt:"#2e7d32",pilot:"#1565c0",observe:"#ef6c00",discard:"#9e9e9e"};
const svg=document.getElementById("r"),cx=380,cy=380,R0=40,dR=64;
function el(n,a){const e=document.createElementNS("http://www.w3.org/2000/svg",n);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e}
RINGS.forEach((r,i)=>{el("circle",{cx:cx,cy:cy,r:R0+dR*(i+1),fill:"none",stroke:"#ddd"});const t=el("text",{x:cx+4,y:cy-(R0+dR*i+dR/2),fill:"#999","font-size":11});t.textContent=r});
AXES.forEach((a,i)=>{const ang=2*Math.PI*i/AXES.length-Math.PI/2;const rMax=R0+dR*RINGS.length;el("line",{x1:cx,y1:cy,x2:cx+Math.cos(ang)*rMax,y2:cy+Math.sin(ang)*rMax,stroke:"#eee"});const lx=cx+Math.cos(ang+0.31)*(rMax-8),ly=cy+Math.sin(ang+0.31)*(rMax-8);const t=el("text",{x:lx,y:ly,fill:"#bbb","font-size":10,"text-anchor":"middle"});t.textContent=a});
function hash(s){let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))>>>0;return h}
DATA.forEach(d=>{const ai=AXES.indexOf(d.axis),ri=RINGS.indexOf(d.ring);const h=hash(d.name);
const ang=2*Math.PI*(ai+0.15+0.7*((h%97)/97))/AXES.length-Math.PI/2;
const rad=R0+dR*ri+dR*(0.2+0.6*((h>>8)%89)/89);
const c=el("circle",{cx:cx+Math.cos(ang)*rad,cy:cy+Math.sin(ang)*rad,r:5,fill:COLORS[d.ring]});
const t=document.createElementNS("http://www.w3.org/2000/svg","title");t.textContent=d.name+" ("+d.axis+")";c.appendChild(t)});
const lg=document.getElementById("legend");
RINGS.forEach(r=>{const n=DATA.filter(d=>d.ring===r);if(!n.length)return;
const h=document.createElement("h3");h.textContent=r+" ("+n.length+")";h.style.color=COLORS[r];lg.appendChild(h);
const ul=document.createElement("ul");n.sort((a,b)=>a.name.localeCompare(b.name)).forEach(d=>{const li=document.createElement("li");li.textContent=d.name+" - "+d.axis;ul.appendChild(li)});lg.appendChild(ul)});
</script></body></html>
"""


# ESCAPING. Every value below comes from a catalogue entry, and a catalogue takes pull
# requests, so a name or a note is untrusted text by the time it reaches a tracked page.
# The HTML page substitutes values into a <script> block and a <title>; the Markdown
# files put them into table cells, which GitHub renders as HTML. Each context gets the
# escaping its own grammar needs, applied at the one place a value enters it.

# `|` ends a table cell and a newline ends a table row; `<`, `>` and `&` are where
# inline HTML starts. Code spans are left as written: HTML inside backticks is shown
# as text, and an escaped entity there would be shown literally rather than decoded.
#
# CommonMark closes a backtick run only with a run of the same length and reads an
# unmatched run as literal backticks, so the HTML after it would render. Rather than
# reproduce that pairing, only the simplest span is kept - one backtick, content with
# no backtick, one backtick, neither touching another backtick - and every other
# backtick is escaped, as is every backslash, since `\`` would otherwise stop a kept
# span's opening backtick from opening it.
_CODE_SPAN = re.compile(r"((?<!`)`[^`]+`(?!`))")

# `[` and `]` open and close the text of a link or an image (`[text](url)`,
# `![alt](url)`), so both are escaped outside a kept code span: catalogue text renders as
# text, never as a link or an image a reader did not see written as one. A bare URL is
# left to the renderer, which may link it.
_MD_LINK_BRACKETS = re.compile(r"([\[\]])")


def _md_plain(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("`", "\\`")
    return html.escape(_MD_LINK_BRACKETS.sub(r"\\\1", escaped), quote=False)


def md_text(value: object) -> str:
    """Free text (a note, a reason, a licence) made safe for one Markdown table cell."""
    flat = " ".join(str(value).split())
    parts = _CODE_SPAN.split(flat)
    out = [p if i % 2 else _md_plain(p) for i, p in enumerate(parts)]
    return "".join(out).replace("|", r"\|")


# A name is an identifier, not prose, so nothing in it is meant as Markdown: every
# character that could open a link, a code span or emphasis is escaped as well.
_MD_NAME_SPECIAL = re.compile(r"([\\`*_\[\]|])")


def md_name(value: object) -> str:
    """A tool name as literal text in Markdown, whatever characters it carries."""
    flat = html.escape(" ".join(str(value).split()), quote=False)
    return _MD_NAME_SPECIAL.sub(r"\\\1", flat)


def md_code(value: object) -> str:
    """A tool name as a code span, or as escaped text when a code span cannot hold it.

    A backtick would close the span early, and a `|` inside a span still splits a table
    cell, so a name carrying either is written as escaped plain text instead.
    """
    flat = " ".join(str(value).split())
    if "`" in flat or "|" in flat:
        return md_name(flat)
    return f"`{flat}`"


# Characters a Markdown link destination cannot carry as-is: a space or `)` ends it,
# `<` and `>` start HTML, `|` splits the table cell around the link.
_URL_SAFE = ":/?#[]@!$&'*+,;=%~.-_"


def repo_cell(t: dict) -> str:
    """`repo` as a table cell: a link when it is an http(s) URL, else the name as text.

    The admitted sentinel for a tool with no repository is the name alone - linking to
    its literal text would render a link to nowhere, e.g.
    `[phantom-tool]((repo does not exist))`. Any other value that is not an http(s) URL
    - `git@host:owner/repo`, or a `javascript:` URL in a private entry, which the schema
    does not check - is written after the name as text, so no other scheme becomes a
    link.
    """
    repo = str(t["repo"]).strip()
    if t["repo"] == NO_REPO_SENTINEL:
        return md_name(t["name"])
    try:
        parts = urllib.parse.urlsplit(repo)
        linkable = parts.scheme.lower() in ("http", "https") and bool(parts.netloc)
    except ValueError:
        linkable = False
    if not linkable:
        return f"{md_name(t['name'])} ({md_code(repo)})"
    url = urllib.parse.quote(repo, safe=_URL_SAFE)
    return f"[{md_name(t['name'])}]({url})"


def ring_tables(tools: list[dict], public: bool) -> list[str]:
    lines = []
    for ring in RINGS:
        rows = [t for t in tools if t["ring"] == ring]
        if not rows:
            continue
        rows.sort(key=lambda t: (ARTIFACTS.index(t["artifact"]), t["axis"], t["name"]))
        lines.append(f"\n## {ring.capitalize()} ({len(rows)})\n")
        if public:
            lines.append("| tool | kind | axis | license |")
            lines.append("|---|---|---|---|")
            for t in rows:
                lines.append(
                    f"| {repo_cell(t)} | {md_text(t['artifact'])} | {md_text(t['axis'])} | "
                    f"{md_text(t['license'])} |"
                )
        else:
            lines.append("| tool | kind | axis | license | eval | note |")
            lines.append("|---|---|---|---|---|---|")
            for t in rows:
                ev = t.get("eval_status", "unmeasured")
                lines.append(
                    f"| {repo_cell(t)} | {md_text(t['artifact'])} | {md_text(t['axis'])} | "
                    f"{md_text(t['license'])} | {md_text(ev)} | {md_text(t['note'])} |"
                )
    return lines


def artifact_view(tools: list[dict]) -> list[str]:
    """The stack grouped by WHAT each thing is, not by which problem it addresses.

    Two views of one set. The ring tables answer "how committed am I"; this answers
    "what am I running, and where" — the question that decides context cost and blast
    radius. An axis mixes a library with a hosted service and hides that difference.
    """
    lines = [
        "\n# By kind of artifact\n",
        "`axis` says which problem a tool addresses. `artifact` says what you install "
        "and where it runs - which is what decides context cost, blast radius, and "
        "whether a locked-down environment can have it.\n",
        "| kind | own | adopt | pilot | observe | discard | what it commits you to |",
        "|---|--:|--:|--:|--:|--:|---|",
    ]
    for a in ARTIFACTS:
        group = [t for t in tools if t["artifact"] == a]
        if not group:
            continue
        cells = [str(sum(1 for t in group if t["ring"] == r) or "-") for r in RINGS]
        lines.append(f"| **{a}** | " + " | ".join(cells) + f" | {ARTIFACT_MEANS[a]} |")

    # The running stack only. A watchlist entry commits you to nothing, so listing every
    # observe entry here would bury the few things that actually load.
    live = [t for t in tools if t["ring"] in ("own", "adopt", "pilot")]
    lines.append("\n## In the loop or on the machine\n")
    for a in ARTIFACTS:
        group = sorted((t for t in live if t["artifact"] == a), key=lambda t: t["name"])
        if not group:
            continue
        names = ", ".join(f"{md_code(t['name'])} ({md_text(t['ring'])})" for t in group)
        lines.append(f"- **{a}** — {ARTIFACT_MEANS[a]}: {names}")
    return lines


def transitions(tools: list[dict], limit: int = 40) -> list[str]:
    entries = []
    for t in tools:
        for h in t.get("history", []):
            entries.append(
                (
                    str(h.get("date", "")),
                    t["name"],
                    h.get("ring", "?"),
                    h.get("reason", ""),
                )
            )
    entries.sort(reverse=True)
    lines = [
        "\n## Transitions (latest)\n",
        "| date | tool | ring | reason |",
        "|---|---|---|---|",
    ]
    for d, n, r, reason in entries[:limit]:
        lines.append(f"| {md_text(d)} | {md_name(n)} | {md_text(r)} | {md_text(reason)} |")
    return lines


def script_json(value: object) -> str:
    """JSON that can sit inside a <script> block without ending it.

    `json.dumps` leaves `<`, `>` and `&` alone, so a name carrying `</script>` closes
    the block and whatever follows it runs as page script. Written as unicode escapes they
    are the same string to the JavaScript parser and invisible to the HTML one.
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_SLOT = re.compile(r"__(TITLE|DATA|AXES)__")


def radar_page(tools: list[dict], title: str, axes: list[str]) -> str:
    """The HTML radar page for `tools`, laid out on the catalogue's `axes`."""
    data = [{"name": t["name"], "axis": t["axis"], "ring": t["ring"]} for t in tools]
    slots = {
        "TITLE": html.escape(title),
        "DATA": script_json(data),
        "AXES": script_json(axes),
    }
    # One pass over the template, not one `replace` per slot: chained replaces would
    # also substitute a slot name that an earlier value happened to contain, so a tool
    # named `__AXES__` would have its own name rewritten inside the data.
    return _SLOT.sub(lambda m: slots[m.group(1)], HTML_TEMPLATE)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar render",
        description="Write the catalogue's artefacts from its entries: README.md and "
        "docs/radar/index.html.",
    )
    ap.add_argument(
        "--public",
        action="store_true",
        help="also write the public projection, PUBLIC.md and docs/radar/public.html, "
        'from the entries marked visibility = "public"',
    )
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    return ap


def main() -> None:
    # Parsed before anything is resolved or written. Without a parser, `--help` fell
    # through to a full render that rewrote the catalogue's tracked files. `--data-root`
    # is still read from argv by the resolver (radar_lib._flag_value); declaring it here
    # is what lets the parser accept it and list it in the help.
    args = build_parser().parse_args()
    root = begin_command()
    marker = load_marker(root)
    # The title is a DATUM the catalogue carries, not this engine's identity - an engine
    # anyone installs must not title a stranger's radar with a name of its own.
    title = str(marker.get("title") or "radar")
    # The page is laid out on the catalogue's own axes, when radar.toml declares them.
    axes = catalogue_axes(marker)
    boundary_note = str((marker.get("render") or {}).get("boundary_note") or DEFAULT_BOUNDARY_NOTE)
    public_mode = args.public
    # include_local=False is load-bearing, not tidiness. README.md and docs/radar/*.html
    # are tracked; a tools.local/ entry rendered into them would be committed and pushed,
    # which is precisely what putting it in a gitignored directory was meant to prevent.
    # If a local entry ever needs to appear somewhere, that somewhere must be untracked.
    tools = load_tools(include_local=False)
    local_n = len(load_tools()) - len(tools)
    today = datetime.date.today().isoformat()
    counts = {r: sum(1 for t in tools if t["ring"] == r) for r in RINGS}
    head = [
        f"# {md_text(title)}",
        "",
        "Generated by `radar render` - do not hand-edit. Source of truth: `tools/*.toml`.",
        f"\n{today} - {len(tools)} tools - "
        + " - ".join(f"{r} {c}" for r, c in counts.items() if c),
        "",
        "Workflow: `radar snapshot` refreshes metrics -> `radar gate` applies the "
        "gate (staleness, license drift, astroturf detector) -> every ring transition "
        "is a dated history entry with a reason, and a move to adopt or pilot also cites "
        "evidence -> `radar render` regenerates this file and "
        "`docs/radar/index.html`.",
        "",
        boundary_note,
    ]
    body = artifact_view(tools) + ring_tables(tools, public=False) + transitions(tools)
    # LF on every OS: a catalogue normalises text to LF, so CRLF output reads as a change
    # to every generated file on Windows even when nothing in it moved.
    outputs = {
        root / "README.md": "\n".join(head + body) + "\n",
        root / "docs" / "radar" / "index.html": radar_page(tools, title, axes),
    }
    if public_mode:
        pub = [t for t in tools if t.get("visibility") == "public"]
        phead = [
            f"# {md_text(title)} (public projection)",
            "",
            f"{today} - {len(pub)} tools. Factual adoption state only; see repo history for evidence.",
        ]
        pbody = artifact_view(pub) + ring_tables(pub, public=True)
        outputs[root / "PUBLIC.md"] = "\n".join(phead + pbody) + "\n"
        outputs[root / "docs" / "radar" / "public.html"] = radar_page(pub, title, axes)
    # Every destination is judged before anything is written, so a refused one leaves the
    # others as they were rather than half a render.
    refused = [err for err in (write_error(root, path) for path in outputs) if err]
    if refused:
        for err in refused:
            print(f"[FAIL] render: {err} - nothing written")
        raise SystemExit(1)
    for path, text in outputs.items():
        write_inside(root, path, text)
    print(
        f"rendered README.md ({len(tools)} tools)"
        + (" + public projection" if public_mode else "")
        + (
            f" - {local_n} machine-local entr{'y' if local_n == 1 else 'ies'} "
            "deliberately excluded from tracked output"
            if local_n
            else ""
        )
    )


if __name__ == "__main__":
    main()
