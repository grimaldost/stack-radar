"""The documents exist, and they cannot go stale in the ways a test can see.

WHY A TEST AT ALL, when documentation is prose and prose is read by people. Three of its
failure modes are not prose failures, and each is silent:

1. A DOCUMENTED VERB THAT DOES NOT EXIST, or an existing verb no document mentions. The
   verb table is data (`cli.VERBS`), so a rename lands in one file and leaves every
   example that used the old name reading plausibly and running not at all. The reader
   who finds out is the one following the quickstart on a fresh machine, which is the
   worst possible place to learn it.

2. AN EXAMPLE THE SCHEMA OR THE GATE REJECTS. The documents teach the entry schema by
   showing it, and a reader who copies a shown entry is then told by a command that it is
   invalid. `validate()` is only part of that: the gate also FAILs a pilot with no
   `[pilot_exit]` and a measurable own/adopt entry with no telemetry matcher, and a whole
   entry shown in a document is held to both.

3. A REAL ACCOUNT NAME IN A PUBLISHED TREE. A path pasted into an example from a real
   machine carries that machine's account name into whatever the engine is published to.
   The rule already exists - it is one of the two FORM rules `gate.check_publishable`
   applies - so this test borrows that implementation rather than growing a second regex
   that could disagree with it.

WHAT THIS TEST CANNOT CHECK, by design: whether a document names a particular catalogue,
the tools in one, or the layout of one operator's machines. Checking names requires
holding the list of names, and a public repository that held that list would be
publishing the very thing the check exists to keep out (`gate.check_publishable` says the
same in its own docstring, from the other side). The list lives with a catalogue, whose
publication check can run the naming half against this tree from outside it. Here the
documents are held to forms, and to human reading.

`RADAR_ENGINE_SRC` selects which tree is read, the same way the rest of the suite does.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

# The FORM rule, borrowed rather than restated. `_user_literal_hits` is the publication
# check's own reader of `C:/Users/<segment>` literals, including its judgement about which
# segments are placeholders (`{user}`, `<you>`, `%USERNAME%`) and therefore fine to write
# in an example. Two copies of that judgement would drift, and the copy that drifted
# lenient is the one nobody would notice.
from stack_radar.cli import SUMMARY, VERBS
from stack_radar.gate import _user_literal_hits
from stack_radar.radar_lib import missing_pilot_exit, missing_telemetry, validate

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))
PYPROJECT = REPO / "pyproject.toml"

# The documents this repository ships. Named individually rather than globbed: a glob
# turns a deleted file into a shorter list, which is a passing test, and the whole point
# of the list is that each of these is promised to a reader somewhere else.
DOCUMENTS = (
    "README.md",
    "docs/concepts.md",
    "docs/new-environment.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "LICENSE",
)

# A fenced ```toml block. The documents teach the entry schema by showing it, and a shown
# entry that the schema would reject is the same defect as a documented verb that does not
# exist - except that this one is read by somebody who then writes a file and gets told it
# is invalid by a command, having copied it from the manual.
_TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.M | re.S)

# `radar <verb>` as a document writes it, and ONLY where a document is quoting a command:
# an inline code span, or a line inside a fenced block. Anchoring on that is what keeps the
# check from reading ordinary prose - "the radar reads the stack" is not a claim that a verb
# called `reads` exists, and a pattern that could not tell the two apart would push the
# documents into stilted English to keep a test quiet.
_INLINE = re.compile(r"`radar\s+([a-z][a-z-]*)")
_COMMAND_LINE = re.compile(r"^\s*(?:\$\s+)?radar\s+([a-z][a-z-]*)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def documents() -> dict[str, str]:
    """Each shipped document's text, by repo-relative path."""
    return {rel: (REPO / rel).read_text(encoding="utf-8") for rel in DOCUMENTS}


def invoked_verbs(text: str) -> set[str]:
    """The verbs a document tells a reader to run."""
    found = {m.group(1) for m in _INLINE.finditer(text)}
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            m = _COMMAND_LINE.match(line)
            if m:
                found.add(m.group(1))
    return found


def test_every_shipped_document_exists():
    missing = [rel for rel in DOCUMENTS if not (REPO / rel).is_file()]
    assert not missing, (
        f"missing shipped document(s): {', '.join(missing)}. Each is promised somewhere a "
        "reader will look - the README's own links, `[project].readme`, the licence a "
        "package index displays - and a promise to a file that is not there is worse than "
        "no promise, because it is only discovered by the person who followed it"
    )


def test_the_readme_is_the_declared_package_readme():
    # `[project].readme` is what a package index renders on the project's page. A key
    # naming a file that is absent fails the BUILD, which is loud; a key that is absent
    # ships a project page with no description at all, which is silent. This asserts the
    # second, since the build already asserts the first.
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = project.get("readme")
    assert declared == "README.md", (
        f"`[project].readme` is {declared!r} - a published package with no readme renders "
        "an empty page on the index, and nothing else in this repository would notice"
    )


def test_no_document_invokes_a_verb_the_cli_does_not_have():
    stale: dict[str, set[str]] = {}
    for rel, text in documents().items():
        unknown = invoked_verbs(text) - set(VERBS)
        if unknown:
            stale[rel] = unknown
    assert not stale, (
        "document(s) invoke a verb that does not exist: "
        + "; ".join(f"{rel} -> {', '.join(sorted(v))}" for rel, v in sorted(stale.items()))
        + ". The verb table is data, so a rename lands in one file and leaves every "
        "example reading plausibly and running not at all"
    )


def test_the_readme_names_every_verb_the_cli_exposes():
    # The other direction, and the one a rename does not cause: a verb SHIPS undocumented.
    # `radar --help` lists it, so the omission is invisible to anyone who already knows the
    # command exists - which is everybody except the reader the README is for.
    documented = invoked_verbs((REPO / "README.md").read_text(encoding="utf-8"))
    undocumented = sorted(set(VERBS) - documented)
    assert not undocumented, (
        f"README.md names no `radar {undocumented[0]}` (and {len(undocumented) - 1} other "
        f"verb(s): {', '.join(undocumented[1:]) or 'none'}). A verb the README does not "
        "name is a verb only a reader of `--help` will ever find"
    )


# One row of the README's verb table: | `radar <verb>` | <what it does> |
_VERB_ROW = re.compile(r"^\| `radar ([a-z][a-z-]*)` \| (.+?) \|$", re.M)


def test_the_readme_verb_table_says_what_radar_help_says():
    # `radar --help` prints `cli.SUMMARY`, one line per verb, and the README prints the
    # same list as a table. Two descriptions of one verb drift apart one edit at a time,
    # so the table is held to the list the command prints.
    rows = dict(_VERB_ROW.findall((REPO / "README.md").read_text(encoding="utf-8")))
    assert rows == SUMMARY, {
        verb: (rows.get(verb), SUMMARY.get(verb))
        for verb in sorted(set(rows) | set(SUMMARY))
        if rows.get(verb) != SUMMARY.get(verb)
    }


def toml_blocks() -> list[tuple[str, int, str]]:
    """`(document, ordinal, source)` for every fenced ```toml block that ships."""
    out = []
    for rel, text in documents().items():
        for i, m in enumerate(_TOML_BLOCK.finditer(text)):
            out.append((rel, i, m.group(1)))
    return out


# A whole entry, so that a FRAGMENT can be judged on what it is actually showing. Several
# examples teach one table at a time - a `[[history]]` block, a `[telemetry]` block - and
# `validate()` reads whole entries, so a fragment handed over as-is comes back with seven
# "missing field" errors and the one real finding buried among them. Wrapping restores the
# signal without inventing a second schema: the fragment still meets the real `validate()`.
_SKELETON = {
    "name": "example",
    "repo": "https://example.invalid/example",
    "axis": "lint-format",
    "ring": "observe",
    "license": "MIT",
    "visibility": "public",
    "note": "a fixture, so a fragment can be validated as part of an entry",
    "artifact": "cli",
}


def as_entry(block: dict) -> dict:
    """One documented example as a whole entry: complete blocks unchanged, fragments wrapped."""
    if "name" in block:
        return dict(block)
    entry = _SKELETON | block
    # The ring and the newest history ring have to agree, and a `[[history]]` fragment is
    # showing a ring change - so the skeleton follows the fragment rather than the fragment
    # being failed for a disagreement the example never claimed.
    history = entry.get("history") or [
        {"date": "2026-01-01", "ring": entry["ring"], "reason": "a fixture"}
    ]
    entry["history"] = history
    entry["ring"] = history[-1].get("ring", entry["ring"])
    return entry


def test_every_toml_example_parses():
    broken = []
    for rel, i, src in toml_blocks():
        try:
            tomllib.loads(src)
        except tomllib.TOMLDecodeError as exc:
            broken.append(f"{rel} block {i}: {exc}")
    assert not broken, "TOML example(s) do not parse: " + "; ".join(broken)


def test_every_entry_example_passes_the_entry_schema():
    rejected = []
    for rel, i, src in toml_blocks():
        entry = as_entry(tomllib.loads(src))
        entry["_file"] = f"{rel} block {i}"
        for err in validate(entry):
            rejected.append(f"{rel} block {i}: {err}")
    assert not rejected, (
        "entry example(s) the schema rejects: "
        + "; ".join(rejected)
        + ". The documents teach the schema by showing it, so a rejected example is read by "
        "somebody who copies it and is then told by a command that the manual was wrong"
    )


def test_every_whole_entry_example_passes_the_gate_rules_outside_the_schema():
    # Only whole entries: a fragment is wrapped in a skeleton whose ring and artifact the
    # example never chose, so holding it to ring-dependent rules would judge the skeleton.
    rejected = []
    for rel, i, src in toml_blocks():
        block = tomllib.loads(src)
        if "name" not in block:
            continue
        if missing_pilot_exit(block):
            rejected.append(f"{rel} block {i}: ring=pilot with no [pilot_exit]")
        if missing_telemetry(block):
            rejected.append(
                f"{rel} block {i}: ring={block.get('ring')} with no [telemetry] matcher"
            )
    assert not rejected, (
        "whole entry example(s) the gate FAILs: "
        + "; ".join(rejected)
        + ". validate() passing is not enough: a reader who copies the entry into tools/ "
        "runs `radar gate`, not validate()"
    )


def test_no_document_carries_a_real_account_name():
    # The half of the publication check a PUBLIC repository is allowed to hold: a form,
    # never a name. See this module's docstring for why the other half cannot live here.
    hits: dict[str, list[str]] = {}
    for rel, text in documents().items():
        found = _user_literal_hits(text)
        if found:
            hits[rel] = found
    assert not hits, (
        "document(s) carry a real account name in a machine path: "
        + "; ".join(f"{rel} -> {', '.join(v)}" for rel, v in sorted(hits.items()))
        + ". Write the placeholder a profile fills (`C:/Users/{user}/...`), which is what "
        "the example is actually saying"
    )
