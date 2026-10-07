"""`radar render-feedback-targets` writes only where a feedback-targets file belongs.

Its destination is catalogue data: `[paths].feedback_targets` in a profile, and one inbox
directory per tool under `[paths].feedback_root`. A profile edited in a pull request could
point the first at `settings.json`, or at any path the operator can write, and a tool
named `../x` could move the second. So the file must end in `.toml`, resolve inside the
profile's `claude_home` or the data root - where it must be named `feedback-targets.toml`
and sit outside the catalogue's own directories - and, when a file is already there, be
one this command wrote. A tool's inbox directory is named after the tool, so the name must
be one plain path component that resolves under `feedback_root`. A tool name that is not
a TOML bare key is written as a quoted key, so a dotted name stays one target instead of
opening a nested table.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from engine_run import Catalogue, entry


def feedback_tool(cat: Catalogue, name: str, file: str | None = None) -> None:
    wt = cat.base / "worktrees" / "tool"
    wt.mkdir(parents=True, exist_ok=True)
    cat.tool(
        name,
        entry(
            name,
            ring="own",
            top={"visibility": "private"},
            tables=f'[feedback]\ndir = "feedback/tool"\nworktree = "{wt.as_posix()}"\n',
        ),
        file=file,
    )


def with_destination(cat: Catalogue, dest: Path) -> None:
    cat.profile(
        'name = "default"\nrings = ["own"]\n\n[paths]\n'
        f'claude_home = "{cat.claude_home.as_posix()}"\n'
        f'feedback_root = "{(cat.claude_home / "feedback").as_posix()}"\n'
        f'feedback_targets = "{dest.as_posix()}"\n'
    )


def render(cat: Catalogue):
    return cat.run("render_feedback_targets", "--env", "default")


def test_a_destination_under_claude_home_is_written(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "widget" in tomllib.loads(dest.read_text(encoding="utf-8"))["targets"]


def test_a_destination_inside_the_data_root_is_written(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = cat.root / "out" / "feedback-targets.toml"
    dest.parent.mkdir()
    with_destination(cat, dest)
    done = render(cat)
    assert done.returncode == 0, done.stdout + done.stderr
    assert dest.is_file()


@pytest.mark.parametrize(
    "where",
    ["elsewhere/feedback-targets.toml", "home/.claude/../outside.toml"],
)
def test_a_destination_outside_claude_home_and_the_data_root_is_refused(tmp_path, where):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = tmp_path / where
    dest.parent.mkdir(parents=True, exist_ok=True)
    with_destination(cat, dest)
    done = render(cat)
    assert "[FAIL] default: [paths].feedback_targets" in done.stdout, done.stdout
    assert "refused" in done.stdout, done.stdout
    assert not dest.exists(), "the artefact was written outside where it belongs"
    assert done.returncode == 1


def test_a_destination_that_is_not_toml_is_refused(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    settings = cat.claude_home / "settings.json"
    settings.write_text('{"keep": true}\n', encoding="utf-8")
    with_destination(cat, settings)
    done = render(cat)
    assert "does not end in .toml - refused" in done.stdout, done.stdout
    assert settings.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert done.returncode == 1


def test_a_tool_name_that_leaves_feedback_root_is_refused(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "../escaped", file="escaped")
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert "[FAIL] ../escaped: this tool name" in done.stdout, done.stdout
    assert not (cat.claude_home / "escaped").exists(), "an inbox was created outside"
    assert not dest.exists()
    assert done.returncode == 1


def test_a_tool_named_dot_is_refused(tmp_path: Path):
    # `.` resolves onto feedback_root itself, which would register the root that holds
    # every tool's inbox as one tool's feedback directory.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, ".", file="dot")
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert "[FAIL] .: this tool name" in done.stdout, done.stdout
    assert not dest.exists()
    assert done.returncode == 1


def test_a_dotted_tool_name_is_one_target(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "docs.server", file="docs-server")
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert done.returncode == 0, done.stdout + done.stderr
    targets = tomllib.loads(dest.read_text(encoding="utf-8"))["targets"]
    assert list(targets) == ["docs.server"], targets


@pytest.mark.parametrize(
    "where",
    [
        "radar.toml",
        "tools/added.toml",
        "environments/default.toml",
        "tools/feedback-targets.toml",
        "uv.toml",
        "ruff.toml",
        ".ruff.toml",
        "ty.toml",
    ],
)
def test_inside_the_data_root_only_the_targets_file_name_is_written(tmp_path: Path, where: str):
    # Inside the data root, anything but a feedback-targets.toml outside the catalogue's
    # own directories would let a profile rewrite the catalogue, add a tool entry, or
    # create a configuration file the catalogue's own toolchain reads.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = cat.root / where
    with_destination(cat, dest)
    before = dest.read_bytes() if dest.exists() else None
    done = render(cat)
    assert "[FAIL] default: [paths].feedback_targets" in done.stdout, done.stdout
    assert "refused" in done.stdout, done.stdout
    assert (dest.read_bytes() if dest.exists() else None) == before
    assert done.returncode == 1


def test_a_claude_home_inside_the_data_root_does_not_widen_what_is_written(tmp_path: Path):
    # claude_home is profile data too: naming the data root as claude_home must not make
    # every .toml name there acceptable.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = cat.root / "uv.toml"
    cat.profile(
        'name = "default"\nrings = ["own"]\n\n[paths]\n'
        f'claude_home = "{cat.root.as_posix()}"\n'
        f'feedback_root = "{(cat.claude_home / "feedback").as_posix()}"\n'
        f'feedback_targets = "{dest.as_posix()}"\n'
    )
    done = render(cat)
    assert "is not named feedback-targets.toml - refused" in done.stdout, done.stdout
    assert not dest.exists()
    assert done.returncode == 1


@pytest.mark.parametrize(("name", "file"), [("a/b", "nested"), ("</script>", "markup")])
def test_a_tool_name_that_is_not_a_plain_file_name_is_refused(tmp_path: Path, name: str, file: str):
    # The name becomes an inbox directory: `a/b` would nest one, and `<` or `>` are
    # characters some file systems refuse, which would fail halfway through the write.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, name, file=file)
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert f"[FAIL] {name}: this tool name is not a plain file name" in done.stdout, (
        done.stdout + done.stderr
    )
    assert "Traceback" not in done.stderr, done.stderr
    assert not dest.exists()
    assert not (cat.claude_home / "feedback" / "a").exists()
    assert done.returncode == 1


def test_a_file_this_command_did_not_write_is_never_overwritten(tmp_path: Path):
    # claude_home comes from the same profile as the destination, so a profile can make
    # any directory "claude_home". What stops the overwrite is the header check.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    elsewhere = tmp_path / "some-app"
    elsewhere.mkdir()
    config = elsewhere / "feedback-targets.toml"
    config.write_text('keep = "this"\n', encoding="utf-8")
    cat.profile(
        'name = "default"\nrings = ["own"]\n\n[paths]\n'
        f'claude_home = "{elsewhere.as_posix()}"\n'
        f'feedback_root = "{(elsewhere / "feedback").as_posix()}"\n'
        f'feedback_targets = "{config.as_posix()}"\n'
    )
    done = render(cat)
    assert "exists and was not written by this command - refused" in done.stdout, done.stdout
    assert config.read_text(encoding="utf-8") == 'keep = "this"\n'
    assert done.returncode == 1


def test_a_file_this_command_wrote_is_rewritten(tmp_path: Path):
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    dest = cat.claude_home / "feedback-targets.toml"
    with_destination(cat, dest)
    assert render(cat).returncode == 0
    feedback_tool(cat, "docs-server")
    again = render(cat)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "wrote" in again.stdout, again.stdout
    assert set(tomllib.loads(dest.read_text(encoding="utf-8"))["targets"]) == {
        "widget",
        "docs-server",
    }


@pytest.mark.parametrize(
    "value",
    ["plain", "a\nb", "tab\there", 'q"uote', "back\\slash", "bell\x07", "del\x7f", "caf\u00e9"],
)
def test_every_string_written_reads_back_as_itself(value: str):
    # A value holding a newline or another control character written raw into a basic
    # string would make the whole generated file unreadable for every consumer.
    from stack_radar.render_feedback_targets import toml_str

    assert tomllib.loads(f"x = {toml_str(value)}\n")["x"] == value


def test_outside_the_data_root_only_the_targets_file_name_is_written(tmp_path: Path):
    # Under claude_home too: a profile arriving in a pull request could otherwise create
    # a pyproject.toml, uv.toml or ruff.toml in any directory there, and each changes how
    # the tools that find it configure the projects below it.
    cat = Catalogue(tmp_path, under_git=False)
    feedback_tool(cat, "widget")
    project = cat.claude_home / "someproject"
    project.mkdir()
    dest = project / "pyproject.toml"
    with_destination(cat, dest)
    done = render(cat)
    assert "is not named feedback-targets.toml - refused" in done.stdout, done.stdout
    assert not dest.exists(), "a file of another name was created under claude_home"
    assert done.returncode == 1
