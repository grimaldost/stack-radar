"""The home-directory half of the redaction, as a pure function.

`redact_home` is handed the home rather than finding it, so every spelling a report can
carry is checked here for both path flavours on any operating system - a Windows home on a
Linux runner and the other way round. The account is synthetic and is joined onto its
parent rather than written out as one literal, which keeps this file clean under the
same publication rule it helps enforce.

The second half is the DECLARED homes - the ones this machine cannot find because they
belong to another one. Same mapping, read from a gitignored file at the data root.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from stack_radar.redact import (
    EXTRA_HOMES_FILE,
    extra_homes,
    redact_home,
    redact_homes,
    scoped_terms,
)

ACCOUNT = "someone"
WIN = PureWindowsPath("C:/Users") / ACCOUNT
NIX = PurePosixPath("/home") / ACCOUNT

# A second account, equally invented. It contains a dot because a dot is valid in an
# account name and is a regex metacharacter.
OTHER = "other.account"
OTHER_WIN = PureWindowsPath("C:/Users") / OTHER
OTHER_NIX = PurePosixPath("/home") / OTHER


class TestSlugForm:
    """Claude Code names a project folder after its working directory, with every
    character outside ASCII letters and digits turned into `-`."""

    def test_a_project_slug_loses_its_home_prefix(self):
        text = f'"slug": "C--Users-{ACCOUNT}-Documents-proj"'
        assert redact_home(text, WIN) == '"slug": "~-Documents-proj"'

    def test_a_session_run_in_the_home_itself(self):
        assert redact_home(f"| `C--Users-{ACCOUNT}` | 3 |", WIN) == "| `~` | 3 |"

    def test_a_scratchpad_slug_carries_the_home_twice(self):
        # A scratchpad directory is itself named after a slug, so a session run in one
        # nests the home inside its own folder name.
        text = (
            f"C--Users-{ACCOUNT}-AppData-Local-Temp-claude-"
            f"C--Users-{ACCOUNT}-Documents-proj-0f1e-scratchpad"
        )
        assert redact_home(text, WIN) == (
            "~-AppData-Local-Temp-claude-~-Documents-proj-0f1e-scratchpad"
        )

    def test_the_drive_letter_is_matched_in_either_case(self):
        # Either spelling can appear: the drive letter follows however the working
        # directory was typed.
        text = f"c--Users-{ACCOUNT}-Documents-proj"
        assert redact_home(text, WIN) == "~-Documents-proj"

    def test_a_posix_home(self):
        assert redact_home(f"-home-{ACCOUNT}-work-proj", NIX) == "~-work-proj"


class TestPathForm:
    def test_the_native_spelling(self):
        text = f"claude_home: `{WIN}\\.claude`"
        assert redact_home(text, WIN) == "claude_home: `~\\.claude`"

    def test_the_forward_slash_spelling(self):
        text = f"index path: `{WIN.as_posix()}/data/transcript-index`"
        assert redact_home(text, WIN) == "index path: `~/data/transcript-index`"

    def test_the_json_escaped_spelling(self):
        # json.dumps doubles every backslash, so the native spelling never occurs in a
        # JSON report's text at all.
        text = json.dumps({"claude_home": f"{WIN}\\.claude"})
        assert redact_home(text, WIN) == json.dumps({"claude_home": "~\\.claude"})

    def test_a_posix_path(self):
        assert redact_home(f"`{NIX}/.claude`", NIX) == "`~/.claude`"

    def test_the_home_at_the_end_of_a_sentence(self):
        assert redact_home(f"no index under {WIN}.", WIN) == ("no index under ~.")


class TestNothingElseChanges:
    def test_near_misses_are_left_alone(self):
        # Every line names something that shares a prefix or a segment with the home and
        # is not it: other accounts, another drive, the account name in prose, the same
        # tail under another root, a slug of another account.
        text = "\n".join(
            [
                f"{WIN}2\\.claude",
                f"{WIN}-old\\Documents",
                f"{WIN}.bak\\x",
                f"{WIN.as_posix()}_x/y",
                str(PureWindowsPath("D:/Users") / ACCOUNT / "Documents"),
                f"{ACCOUNT} ran the review",
                f"/mnt{NIX}/.claude",
                f"C--Users-{ACCOUNT}2-Documents-proj",
                f"xC--Users-{ACCOUNT}-Documents",
                "~-Documents-proj and ~\\.claude, already redacted",
            ]
        )
        assert redact_home(text, WIN) == text
        assert redact_home(text.replace("\\", "/"), NIX) == text.replace("\\", "/")

    def test_a_report_changes_only_where_the_home_was(self):
        lines = [
            "# Field report - ruff",
            "- environment: `default`  claude_home: `{home}\\.claude`",
            "| `{slug}-Documents-proj` | 12 |",
            "| `C--Users-other-Documents-proj` | 3 |",
            "| `cmd:ruff` | 15 | 4 | 0 | 812.0 | 2400.5 |",
            "- index path: `{posix}/data/transcript-index`",
        ]
        slug = str(WIN).replace(":", "-").replace("\\", "-")
        text = "\n".join(lines).format(home=WIN, slug=slug, posix=WIN.as_posix())
        out = redact_home(text, WIN).splitlines()
        assert out == [
            "# Field report - ruff",
            "- environment: `default`  claude_home: `~\\.claude`",
            "| `~-Documents-proj` | 12 |",
            "| `C--Users-other-Documents-proj` | 3 |",
            "| `cmd:ruff` | 15 | 4 | 0 | 812.0 | 2400.5 |",
            "- index path: `~/data/transcript-index`",
        ]

    def test_it_is_idempotent(self):
        text = f"{WIN}\\.claude C--Users-{ACCOUNT}-Documents"
        once = redact_home(text, WIN)
        assert redact_home(once, WIN) == once

    def test_a_home_that_is_a_bare_root_redacts_nothing(self):
        # A home of `/` or `C:\` would turn every path in the report into `~`. There is
        # no account to protect in it, so the function declines rather than guesses.
        text = f"{WIN}\\.claude and /usr/bin and C--Users-x"
        assert redact_home(text, PureWindowsPath("C:/")) == text
        assert redact_home(text, PurePosixPath("/")) == text

    def test_no_home_redacts_nothing(self):
        text = f"{WIN}\\.claude"
        assert redact_home(text, None) == text


def declare(root, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / EXTRA_HOMES_FILE).write_text(body, encoding="utf-8")


class TestExtraHomes:
    """The homes this machine cannot find, because they are not this machine's.

    A report written on another workstation and synced here carries that machine's home,
    and `Path.home()` answers with this one. So the others are declared, in a file the
    data root keeps out of git - an account name is exactly what the redaction exists to
    withhold, and a tracked declaration would publish it in order to say it must not be
    published.
    """

    def test_no_file_declares_nothing(self, tmp_path):
        assert extra_homes(tmp_path) == []

    def test_an_empty_file_declares_nothing(self, tmp_path):
        declare(tmp_path, "")
        assert extra_homes(tmp_path) == []

    def test_a_file_with_no_entries_declares_nothing(self, tmp_path):
        declare(tmp_path, "homes = []\n")
        assert extra_homes(tmp_path) == []

    def test_a_blank_entry_is_not_a_home(self, tmp_path):
        # A blank string is a bare root by another spelling, and mapping it would turn
        # every path in the corpus into `~`.
        declare(tmp_path, 'homes = ["", "   "]\n')
        assert extra_homes(tmp_path) == []

    def test_a_declared_windows_home_keeps_both_spellings_anywhere(self, tmp_path):
        # The flavour is read off the STRING, not off the machine running the test: a
        # Windows home declared on a Linux runner would otherwise lose its native
        # spelling, and that is the spelling most of a mined corpus uses.
        declare(tmp_path, f'homes = ["{OTHER_WIN.as_posix()}"]\n')
        (home,) = extra_homes(tmp_path)
        assert redact_home(f"`{OTHER_WIN}\\Downloads`", home) == "`~\\Downloads`"
        assert redact_home(f"`{OTHER_WIN.as_posix()}/Downloads`", home) == "`~/Downloads`"

    def test_a_declared_posix_home(self, tmp_path):
        declare(tmp_path, f'homes = ["{OTHER_NIX}"]\n')
        (home,) = extra_homes(tmp_path)
        assert redact_home(f"{OTHER_NIX}/work", home) == "~/work"

    def test_a_single_string_is_read_as_one_home(self, tmp_path):
        # Iterating a bare string would yield one home per CHARACTER, each too short to
        # match anything - a declaration that silently declared nothing.
        declare(tmp_path, f'homes = "{OTHER_NIX}"\n')
        assert [str(h) for h in extra_homes(tmp_path)] == [str(OTHER_NIX)]

    def test_a_file_that_does_not_parse_is_an_error(self, tmp_path):
        # Not the same thing as an absent file. A declaration that could not be read must
        # not look like one that declared nothing - that is the shape of a silent leak.
        declare(tmp_path, "homes = [\n")
        with pytest.raises(tomllib.TOMLDecodeError):
            extra_homes(tmp_path)


class TestRedactHomes:
    def test_every_home_in_the_list_is_mapped(self):
        text = f"{WIN}\\.claude and {OTHER_WIN}\\Downloads"
        assert redact_homes(text, [WIN, OTHER_WIN]) == "~\\.claude and ~\\Downloads"

    def test_no_homes_changes_nothing(self):
        text = f"{WIN}\\.claude"
        assert redact_homes(text, []) == text

    def test_a_none_among_them_is_skipped(self):
        text = f"{OTHER_WIN}\\Downloads"
        assert redact_homes(text, [None, OTHER_WIN]) == "~\\Downloads"

    def test_the_longer_home_is_mapped_first_when_one_nests_in_another(self):
        # Two declared homes where one is the other's parent. Shortest-first would leave
        # `~/nested/x`, naming the directory the longer home exists to hide.
        outer = PureWindowsPath("C:/Users") / OTHER
        inner = outer / "nested"
        assert redact_homes(f"{inner.as_posix()}/x", [outer, inner]) == "~/x"


class TestScopedTerms:
    """The placeholder a scoped name maps to is one plain file-name component, because it
    replaces the name inside tracked file names as well as in text."""

    def test_a_declared_placeholder_is_used(self):
        tools = [{"name": "local-widget", "redact_as": "local-lib", "_local": True}]
        assert scoped_terms(tools) == [("local-widget", "local-lib")]

    @pytest.mark.parametrize("placeholder", ["../../escaped", "a/b", ".x", ""])
    def test_a_placeholder_that_is_not_a_plain_name_is_not_passed_through(self, placeholder):
        tools = [{"name": "local-widget", "redact_as": placeholder, "_local": True}]
        assert scoped_terms(tools) == [("local-widget", "scoped-tool")]
