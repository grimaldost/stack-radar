"""`radar sync-feedback`: the inbox -> archive -> inbox round trip.

THE INDEX BUILDER is resolved by a declared key, not by a tool's name. An entry that ships
the builder declares `[feedback].index_builder`, relative to its own `worktree`, and the
resolver walks entries in load order (alphabetical by file name, `radar_lib.load_tools`),
taking the first one whose declared path RESOLVES to a real file - not the first that
merely declares one. A dead key (the worktree moved, the script was renamed) has to fall
through to the next candidate instead of ending the search, or one stale entry would
silently stop every index from being regenerated. The builder is a script the catalogue
names and the engine runs, so it runs only once the operator approved it as it stands.

`--check` IS THE BOUNDARY: it reports the pending targets and touches nothing, which is
what makes it safe to run against a real inbox before ingesting anything.

A REPORT EXTENDED AFTER IT WAS INGESTED is an amendment, not a conflict: the archive takes
the longer copy. Only two copies of which neither extends the other are a conflict - two
reports written under one name - and those keep failing.

EVERY WRITE STAYS WHERE IT BELONGS: a tool's name is catalogue data, and one that resolves
outside `feedback_root` or the archive is refused before anything is read or written.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from engine_run import git, run_engine

from stack_radar import sync_feedback
from stack_radar.radar_lib import validate
from stack_radar.redact import EXTRA_HOMES_FILE

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))

BASE = {
    "name": "x",
    "repo": "https://example.invalid/x",
    "axis": "lint-format",
    "ring": "own",
    "license": "MIT",
    "visibility": "private",
    "note": "n",
    "artifact": "cli",
    "history": [{"date": "2026-08-01", "ring": "own", "evidence": "e", "reason": "r"}],
}


# --------------------------------------------------------------- index_builder()


class TestIndexBuilder:
    def test_resolves_the_builder_via_the_declared_key(self, tmp_path: Path):
        wt = tmp_path / "wt"
        rel = "scripts/build_index.py"
        (wt / "scripts").mkdir(parents=True)
        (wt / rel).write_text("# builder\n", encoding="utf-8")
        tools = [{"name": "z", "feedback": {"worktree": str(wt), "index_builder": rel}}]
        assert sync_feedback.index_builder(tools, {}) == ("z", wt / rel)

    def test_a_dead_key_falls_through_to_the_entry_that_actually_resolves(self, tmp_path: Path):
        # The first entry whose declared builder EXISTS, not the first that declares one:
        # entry "a" sorts first and declares a path that resolves to nothing (its worktree
        # moved); entry "b" declares a real one. The resolver must not stop at "a" and
        # report the builder missing.
        rel = "tool/build.py"
        real_wt = tmp_path / "real"
        (real_wt / "tool").mkdir(parents=True)
        (real_wt / rel).write_text("# builder\n", encoding="utf-8")
        tools = [
            {"name": "a", "feedback": {"worktree": str(tmp_path / "gone"), "index_builder": rel}},
            {"name": "b", "feedback": {"worktree": str(real_wt), "index_builder": rel}},
        ]
        assert sync_feedback.index_builder(tools, {}) == ("b", real_wt / rel)

    def test_no_entry_declaring_the_key_resolves_to_none(self):
        # The WARN path: every entry either has no [feedback], no index_builder, or a
        # dead one. Nothing here should raise - main() turns None into a printed WARN.
        tools = [
            {"name": "a", "feedback": {"dir": "feedback/a"}},
            {"name": "b"},
            {"name": "c", "feedback": {"worktree": "/nowhere", "index_builder": "x.py"}},
        ]
        assert sync_feedback.index_builder(tools, {}) is None

    def test_paths_placeholders_expand_before_the_worktree_is_probed(self, tmp_path: Path):
        (tmp_path / "b.py").write_text("# builder\n", encoding="utf-8")
        tools = [{"name": "a", "feedback": {"worktree": "{documents}", "index_builder": "b.py"}}]
        found = sync_feedback.index_builder(tools, {"documents": str(tmp_path)})
        assert found == ("a", tmp_path / "b.py")


# --------------------------------------------------------------- validate() knows the key


class TestValidateKnowsTheKey:
    def test_a_string_index_builder_validates(self):
        t = {
            **BASE,
            "feedback": {
                "dir": "feedback/x",
                "worktree": "/home/x",
                "index_builder": "scripts/build.py",
            },
        }
        assert validate(t) == []

    def test_a_non_string_index_builder_is_a_schema_error(self):
        t = {
            **BASE,
            "feedback": {
                "dir": "feedback/x",
                "worktree": "/home/x",
                "index_builder": ["scripts/build.py"],
            },
        }
        errs = validate(t)
        assert any("index_builder" in e for e in errs), errs


# --------------------------------------------------------------- --check is the boundary


TOOL_TOML = """\
name = "{name}"
repo = "https://example.invalid/tool"
axis = "lint-format"
ring = "own"
license = "MIT"
visibility = "private"
note = "n"
artifact = "cli"

[feedback]
dir = "feedback/tool"
{extra}
[[history]]
date = "2026-08-01"
ring = "own"
evidence = "e"
reason = "r"
"""


def sync_root(
    dest: Path,
    *,
    pending: dict[str, list[str]],
    feedback_root: Path | None = None,
    under_git: bool = False,
    extra: str = "",
) -> Path:
    """A miniature radar with one inbox report per (target, body) in `pending`.

    Only what `sync_feedback` itself reads: no pyproject.toml (the marker declares
    no `requires_framework`, so `begin_command` never asks for one) and no
    tools.local/ (nothing here is machine-scoped). Returns the inbox root.
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "radar.toml").write_text(
        'schema = "radar-data/v1"\ndefault_environment = "default"\n', encoding="utf-8"
    )
    (dest / "tools").mkdir()
    (dest / "environments").mkdir()
    inbox = feedback_root or dest / "inbox"
    for i, (name, bodies) in enumerate(pending.items()):
        (dest / "tools" / f"tool-{i}.toml").write_text(
            TOOL_TOML.format(name=name, extra=extra), encoding="utf-8"
        )
        target = inbox / name
        target.mkdir(parents=True, exist_ok=True)
        for j, body in enumerate(bodies):
            (target / f"2026-09-12-report-{j}.md").write_text(body, encoding="utf-8")
    (dest / "environments" / "default.toml").write_text(
        f'name = "default"\nrings = ["own"]\n\n[paths]\nfeedback_root = "{inbox.as_posix()}"\n',
        encoding="utf-8",
    )
    if under_git:
        git(dest, dest.parent / "home", "init", "-q")
    return inbox


def run_sync(dest: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("RADAR_DATA_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return run_engine("sync_feedback", args, cwd=dest, env=env)


def tracked_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


class TestCheckIsTheBoundary:
    def test_check_reports_the_pending_target_and_writes_nothing(self, tmp_path: Path):
        dest = tmp_path / "radar"
        sync_root(dest, pending={"alpha": ["a pending report\n"]})
        before = tracked_files(dest)
        done = run_sync(dest, "--env", "default", "--check")
        after = tracked_files(dest)
        assert after == before, sorted(before ^ after)
        assert "alpha: would ingest 1" in done.stdout, done.stdout
        assert "1 target(s)" in done.stdout, done.stdout
        # --check with drift is a reported mismatch, not a crash.
        assert done.returncode == 1, done.stdout + done.stderr

    def test_check_with_nothing_pending_is_clean(self, tmp_path: Path):
        dest = tmp_path / "radar"
        sync_root(dest, pending={"alpha": []})
        done = run_sync(dest, "--env", "default", "--check")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "0 file(s) out of sync" in done.stdout, done.stdout

    def test_check_prints_the_warn_when_no_entry_resolves_a_builder(self, tmp_path: Path):
        # Every fixture entry here declares [feedback] with no index_builder at all, so
        # the WARN path has to still fire, worded without naming any one tool (the search
        # is not scoped to a fixed name).
        dest = tmp_path / "radar"
        sync_root(dest, pending={"alpha": ["a report\n"]})
        done = run_sync(dest, "--env", "default", "--check")
        assert "[WARN] no tool entry resolves a [feedback].index_builder" in done.stdout, (
            done.stdout
        )


# --------------------------------------------------------------- amendments


def archived(dest: Path, name: str = "alpha") -> Path:
    return dest / "feedback" / name / "2026-09-12-report-0.md"


class TestAmendments:
    def ingested(self, tmp_path: Path, body: str) -> tuple[Path, Path]:
        dest = tmp_path / "radar"
        inbox = sync_root(dest, pending={"alpha": [body]})
        first = run_sync(dest, "--env", "default", "--no-mirror")
        assert first.returncode == 0, first.stdout + first.stderr
        return dest, inbox / "alpha" / "2026-09-12-report-0.md"

    def test_an_extended_report_amends_the_archive(self, tmp_path: Path):
        dest, report = self.ingested(tmp_path, "first part\n")
        report.write_text("first part\nand a closing section\n", encoding="utf-8")
        done = run_sync(dest, "--env", "default", "--no-mirror")
        assert (
            "[AMENDED] feedback/alpha/2026-09-12-report-0.md - extended after it was ingested"
        ) in done.stdout, done.stdout
        assert archived(dest).read_text(encoding="utf-8") == "first part\nand a closing section\n"
        assert "[FAIL]" not in done.stdout and "0 conflict(s)" in done.stdout, done.stdout
        assert done.returncode == 0, done.stdout + done.stderr

    def test_check_reports_the_amendment_and_writes_nothing(self, tmp_path: Path):
        dest, report = self.ingested(tmp_path, "first part\n")
        report.write_text("first part\nmore\n", encoding="utf-8")
        done = run_sync(dest, "--env", "default", "--no-mirror", "--check")
        assert "[AMENDED] feedback/alpha/2026-09-12-report-0.md" in done.stdout, done.stdout
        assert archived(dest).read_text(encoding="utf-8") == "first part\n"
        assert done.returncode == 1, done.stdout + done.stderr

    def test_a_report_that_does_not_extend_the_archived_one_is_a_conflict(self, tmp_path: Path):
        dest, report = self.ingested(tmp_path, "first part\n")
        report.write_text("a different report under the same name\n", encoding="utf-8")
        done = run_sync(dest, "--env", "default", "--no-mirror")
        assert "[FAIL] alpha: 2026-09-12-report-0.md differs" in done.stdout, done.stdout
        assert "rename one report" in done.stdout, done.stdout
        assert archived(dest).read_text(encoding="utf-8") == "first part\n"
        assert done.returncode == 1

    def test_an_inbox_copy_behind_the_archive_is_refreshed_not_failed(self, tmp_path: Path):
        # Amended and synced on another machine: the archive holds the longer copy, and
        # this inbox still has the one it had before.
        dest, report = self.ingested(tmp_path, "first part\n")
        archived(dest).write_text("first part\namended elsewhere\n", encoding="utf-8")
        done = run_sync(dest, "--env", "default")
        assert "[FAIL]" not in done.stdout, done.stdout
        assert report.read_text(encoding="utf-8") == "first part\namended elsewhere\n"
        assert done.returncode == 0, done.stdout + done.stderr


# --------------------------------------------------------------- write destinations


class TestWriteDestinations:
    def test_a_tool_name_that_leaves_feedback_root_is_refused(self, tmp_path: Path):
        # feedback_root two levels down, so `../../escaped` lands beside the catalogue for
        # the inbox and one level above it for the archive: two different places outside.
        dest = tmp_path / "radar"
        feedback_root = dest / "data" / "inbox"
        sync_root(dest, pending={"../../escaped": ["a report\n"]}, feedback_root=feedback_root)
        outside = tmp_path / "escaped"
        before = {p for p in tmp_path.rglob("*")}
        done = run_sync(dest, "--env", "default")
        assert "[FAIL] ../../escaped: this tool name" in done.stdout, done.stdout
        assert "refused, nothing read or written for it" in done.stdout, done.stdout
        assert not outside.exists(), "a report was archived outside the catalogue"
        assert {p for p in tmp_path.rglob("*")} == before
        assert done.returncode == 1

    def test_a_tool_named_dot_is_refused(self, tmp_path: Path):
        # `.` stays inside feedback_root, but as feedback_root itself: its inbox would be
        # every tool's inbox and its archive the archive root.
        dest = tmp_path / "radar"
        sync_root(dest, pending={".": ["a report\n"]})
        done = run_sync(dest, "--env", "default")
        assert "[FAIL] .: this tool name" in done.stdout, done.stdout
        assert "refused, nothing read or written for it" in done.stdout, done.stdout
        assert not (dest / "feedback").exists(), sorted(
            p.relative_to(dest).as_posix() for p in (dest / "feedback").rglob("*")
        )
        assert done.returncode == 1

    def test_a_tool_name_with_a_separator_is_refused(self, tmp_path: Path):
        # `a/b` stays under feedback_root, but as a nested directory the schema's name
        # rule never allows; it must not create feedback/a/b in the archive.
        dest = tmp_path / "radar"
        sync_root(dest, pending={"a/b": ["a report\n"]})
        done = run_sync(dest, "--env", "default")
        assert "[FAIL] a/b: this tool name is not a plain file name" in done.stdout, done.stdout
        assert not (dest / "feedback").exists()
        assert done.returncode == 1
        # A refusal is not two reports disagreeing, and the summary keeps them apart.
        assert "0 conflict(s) - 1 refused" in done.stdout, done.stdout


# --------------------------------------------------------------- builder approval


BUILDER = """\
import pathlib, sys
pathlib.Path({marker!r}).write_text("ran", encoding="utf-8")
(pathlib.Path(sys.argv[1]) / "INDEX.md").write_text("# index\\n", encoding="utf-8")
"""


class TestIndexBuilderApproval:
    def setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        wt = tmp_path / "builder-wt"
        wt.mkdir()
        marker = tmp_path / "builder-ran"
        (wt / "build_index.py").write_text(
            BUILDER.format(marker=marker.as_posix()), encoding="utf-8"
        )
        dest = tmp_path / "radar"
        extra = f'worktree = "{wt.as_posix()}"\nindex_builder = "build_index.py"\n'
        inbox = sync_root(dest, pending={"alpha": ["one\n"]}, under_git=True, extra=extra)
        return dest, inbox / "alpha", marker

    def test_a_pending_builder_is_not_run(self, tmp_path: Path):
        dest, _, marker = self.setup(tmp_path)
        done = run_sync(dest, "--env", "default")
        assert not marker.exists(), "the index builder ran without an approval"
        assert "[UNTRUSTED] alpha: [feedback].index_builder not run" in done.stdout, done.stdout
        assert "--trust-commands" in done.stdout.splitlines()[-1], done.stdout
        assert done.returncode == 1

    def test_approving_later_rebuilds_the_index_the_skipped_run_left_behind(self, tmp_path: Path):
        # The skipped run ingested the report; the approving run finds nothing new. It
        # must still ask for the builder, or the advice to re-run could not be followed.
        dest, _, marker = self.setup(tmp_path)
        skipped = run_sync(dest, "--env", "default")
        assert skipped.returncode == 1 and not marker.exists(), skipped.stdout

        checked = run_sync(dest, "--env", "default", "--check")
        assert "[NOTE] alpha: INDEX.md is behind the archive" in checked.stdout, checked.stdout
        assert not marker.exists(), "--check ran the index builder"

        approved = run_sync(dest, "--env", "default", "--trust-commands")
        assert "[TRUSTED] alpha: [feedback].index_builder approved and recorded" in (
            approved.stdout
        ), approved.stdout
        assert marker.exists(), approved.stdout + approved.stderr
        assert (dest / "feedback" / "alpha" / "INDEX.md").is_file()
        assert approved.returncode == 0, approved.stdout + approved.stderr

        marker.unlink()
        settled = run_sync(dest, "--env", "default")
        assert not marker.exists(), "the builder ran again with the index already current"
        assert settled.returncode == 0, settled.stdout + settled.stderr

    def test_an_approved_builder_runs_until_it_changes(self, tmp_path: Path):
        dest, inbox, marker = self.setup(tmp_path)
        run_sync(dest, "--env", "default")
        done = run_sync(dest, "--env", "default", "--trust-commands")
        assert "[TRUSTED] alpha: [feedback].index_builder approved and recorded" in done.stdout
        assert marker.exists(), done.stdout + done.stderr
        assert done.returncode == 0, done.stdout + done.stderr

        marker.unlink()
        (inbox / "2026-09-13-report.md").write_text("two\n", encoding="utf-8")
        again = run_sync(dest, "--env", "default")
        assert marker.exists() and "[UNTRUSTED]" not in again.stdout, again.stdout

        marker.unlink()
        builder = tmp_path / "builder-wt" / "build_index.py"
        builder.write_text(builder.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        (inbox / "2026-09-14-report.md").write_text("three\n", encoding="utf-8")
        changed = run_sync(dest, "--env", "default")
        assert not marker.exists(), "an edited builder ran on its old approval"
        assert "[UNTRUSTED] alpha: [feedback].index_builder not run" in changed.stdout


class TestIndexBuilderRun:
    """An approved builder runs like every catalogue command: in an empty scratch
    directory, with PYTHONSAFEPATH set, and a failure is reported rather than ignored."""

    def setup(self, tmp_path: Path, body: str) -> Path:
        wt = tmp_path / "builder-wt"
        wt.mkdir()
        (wt / "build_index.py").write_text(body, encoding="utf-8")
        dest = tmp_path / "radar"
        extra = f'worktree = "{wt.as_posix()}"\nindex_builder = "build_index.py"\n'
        sync_root(dest, pending={"alpha": ["one\n"]}, under_git=True, extra=extra)
        return dest

    def test_it_runs_outside_the_data_root_and_imports_nothing_beside_itself(self, tmp_path: Path):
        seen = tmp_path / "builder-cwd"
        helper_ran = tmp_path / "helper-ran"
        dest = self.setup(
            tmp_path,
            "import os, pathlib, sys\n"
            "try:\n    import helper\nexcept ImportError:\n    pass\n"
            f"pathlib.Path({seen.as_posix()!r}).write_text(os.getcwd(), encoding='utf-8')\n"
            "(pathlib.Path(sys.argv[1]) / 'INDEX.md').write_text('# index\\n')\n",
        )
        (tmp_path / "builder-wt" / "helper.py").write_text(
            f"import pathlib\npathlib.Path({helper_ran.as_posix()!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        run_sync(dest, "--env", "default")
        done = run_sync(dest, "--env", "default", "--trust-commands")
        assert done.returncode == 0, done.stdout + done.stderr
        ran_in = Path(seen.read_text(encoding="utf-8"))
        assert not ran_in.resolve().is_relative_to(dest.resolve()), ran_in
        assert not helper_ran.exists(), "a module beside the approved builder was imported"

    def test_a_failing_builder_is_a_fail(self, tmp_path: Path):
        dest = self.setup(tmp_path, "import sys\nsys.exit('index broke')\n")
        run_sync(dest, "--env", "default")
        done = run_sync(dest, "--env", "default", "--trust-commands")
        assert "[FAIL] alpha: index builder exited 1: index broke" in done.stdout, done.stdout
        assert done.returncode == 1, done.stdout


# --------------------------------------------------------------- redaction on the way in


class TestIngestMapsTheHomeOut:
    """A report lands in the archive with the home written as `~`, like a field report.

    The inbox is data plane, written by sessions on this machine, and a session writes
    whatever path it worked in. The archive is tracked, so the home has to be mapped on the
    way in - the same place the scoped names already were - or every synced report puts
    the account back into the catalogue. `Path.home` is patched and the command runs in
    this process, so the home is one the test invented.
    """

    def ingest_once(self, dest: Path, monkeypatch, capsys) -> tuple[int, str]:
        monkeypatch.chdir(dest)
        monkeypatch.delenv("RADAR_DATA_ROOT", raising=False)
        monkeypatch.setattr(sys, "argv", ["sync_feedback", "--env", "default", "--no-mirror"])
        code = sync_feedback.main()
        return code, capsys.readouterr().out

    def setup(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
        home = tmp_path / "Users" / "someone"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(home))
        body = (
            f"Ran in `{home.as_posix()}/Documents/p` with private-widget.\n"
            f"claude_home: {home / '.claude'}\n"
            f"session filed under {slug}-Documents-p\n"
        )
        dest = tmp_path / "radar"
        sync_root(dest, pending={"alpha": [body]})
        (dest / "tools.local").mkdir()
        (dest / "tools.local" / "private-widget.toml").write_text(
            'name = "private-widget"\nredact_as = "scoped-tool"\n', encoding="utf-8"
        )
        return dest, home, body

    def test_the_archived_report_carries_neither_the_home_nor_the_scoped_name(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        dest, home, _ = self.setup(tmp_path, monkeypatch)
        code, out = self.ingest_once(dest, monkeypatch, capsys)
        assert code == 0, out
        assert archived(dest).read_text(encoding="utf-8") == (
            "Ran in `~/Documents/p` with scoped-tool.\n"
            f"claude_home: {Path('~') / '.claude'}\n"
            "session filed under ~-Documents-p\n"
        )

    def test_a_report_ingested_once_is_not_a_conflict_the_next_time(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        # The comparison is against the REDACTED inbox copy. Against the raw one, every
        # report carrying the home would be reported as a conflict on every later run.
        dest, _, _ = self.setup(tmp_path, monkeypatch)
        self.ingest_once(dest, monkeypatch, capsys)
        code, out = self.ingest_once(dest, monkeypatch, capsys)
        assert code == 0, out
        assert "[FAIL]" not in out, out
        assert "0 conflict(s)" in out, out

    def test_the_inbox_keeps_the_real_paths(self, tmp_path: Path, monkeypatch, capsys):
        # The inbox is on the machine that owns the paths; only the archive travels.
        dest, _, body = self.setup(tmp_path, monkeypatch)
        self.ingest_once(dest, monkeypatch, capsys)
        inbox = dest / "inbox" / "alpha" / "2026-09-12-report-0.md"
        assert inbox.read_text(encoding="utf-8") == body


class TestIngestMapsADeclaredHomeOut:
    """A report carrying ANOTHER machine's home is archived with that mapped too.

    The inbox merges corpora across machines, so a report reaching it was not necessarily
    written here - and `Path.home()` answers with this account whatever the report says.
    The other homes are declared in the gitignored `redact.local.toml` at the data root,
    and ingest applies them through the same mapping.
    """

    # Invented, and joined onto its parent rather than written as one literal.
    OTHER = PureWindowsPath("C:/Users") / "other.account"

    def ingest_once(self, dest: Path, monkeypatch, capsys) -> tuple[int, str]:
        monkeypatch.chdir(dest)
        monkeypatch.delenv("RADAR_DATA_ROOT", raising=False)
        monkeypatch.setattr(sys, "argv", ["sync_feedback", "--env", "default", "--no-mirror"])
        code = sync_feedback.main()
        return code, capsys.readouterr().out

    def setup(self, tmp_path: Path, monkeypatch, *, declare: bool) -> tuple[Path, str]:
        home = tmp_path / "Users" / "someone"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        body = f"Wrote it to `{self.OTHER.as_posix()}/Downloads`, from `{self.OTHER}\\work`.\n"
        dest = tmp_path / "radar"
        sync_root(dest, pending={"alpha": [body]})
        if declare:
            (dest / EXTRA_HOMES_FILE).write_text(
                f'homes = ["{self.OTHER.as_posix()}"]\n', encoding="utf-8"
            )
        return dest, body

    def test_the_declared_home_is_mapped_on_the_way_in(self, tmp_path: Path, monkeypatch, capsys):
        dest, _ = self.setup(tmp_path, monkeypatch, declare=True)
        code, out = self.ingest_once(dest, monkeypatch, capsys)
        assert code == 0, out
        assert archived(dest).read_text(encoding="utf-8") == (
            "Wrote it to `~/Downloads`, from `~\\work`.\n"
        )

    def test_without_the_declaration_it_is_archived_as_written(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        dest, body = self.setup(tmp_path, monkeypatch, declare=False)
        code, out = self.ingest_once(dest, monkeypatch, capsys)
        assert code == 0, out
        assert archived(dest).read_text(encoding="utf-8") == body

    def test_a_report_ingested_once_is_not_a_conflict_the_next_time(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        # The already-ingested comparison is against the redacted source, so the extra
        # homes have to be applied on both sides or every re-run reports a conflict.
        dest, _ = self.setup(tmp_path, monkeypatch, declare=True)
        self.ingest_once(dest, monkeypatch, capsys)
        code, out = self.ingest_once(dest, monkeypatch, capsys)
        assert code == 0, out
        assert "0 conflict(s)" in out, out
