"""A profile's `[paths]` may start with `~`, and every reader of a profile expands it.

A profile is tracked, and the paths it declares are true of one machine: written out in
full, each one carries the account name into the catalogue. `~` says the same thing
without naming anybody, but only if the code that reads the profile turns it back into
the home directory - otherwise `~/.claude` is a relative path to a folder called `~`.

Two places read a profile, and each is held to it here: `radar_lib.load_profile`, which
every command that takes `--env` reads through and which layers the machine-local overlay
the profile names, and `bootstrap.load_profile_raw`, which compares the committed profile's
declared paths with the discovered ones. The home comes from a patched `Path.home`, so it
is one the test invented, and the expansion is written with forward slashes - the
spelling profiles already use, so a path that was spelled out and one that was written
with `~` load equal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stack_radar import bootstrap
from stack_radar.radar_lib import load_environment, load_profile

MARKER = 'schema = "radar-data/v1"\ndefault_environment = "default"\n'

PROFILE = """\
name = "default"
rings = ["own"]

[paths]
claude_home = "~/.claude"
documents = "~/Documents"
bare = "~"
spelled_out = "/opt/elsewhere"
template = "{documents}/repo"
someone_else = "~other/x"
not_leading = "a/~/b"

[flags]
overlay = "default.local.toml"
"""

OVERLAY = """\
[paths]
feedback_root = "~/.claude/feedback"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "Users" / "someone"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "radar"
    (r / "environments").mkdir(parents=True)
    (r / "radar.toml").write_text(MARKER, encoding="utf-8")
    (r / "environments" / "default.toml").write_text(PROFILE, encoding="utf-8")
    (r / "environments" / "default.local.toml").write_text(OVERLAY, encoding="utf-8")
    monkeypatch.chdir(r)
    monkeypatch.delenv("RADAR_DATA_ROOT", raising=False)
    return r


def test_load_environment_expands_a_leading_tilde(root: Path, home: Path):
    paths = load_environment("default", root)["paths"]
    assert paths["claude_home"] == f"{home.as_posix()}/.claude"
    assert paths["documents"] == f"{home.as_posix()}/Documents"
    assert paths["bare"] == home.as_posix()


def test_values_that_do_not_start_with_the_home_are_left_alone(root: Path, home: Path):
    paths = load_environment("default", root)["paths"]
    assert paths["spelled_out"] == "/opt/elsewhere"
    assert paths["template"] == "{documents}/repo"
    # `~other` is another account's home; guessing it would be wrong more often than not.
    assert paths["someone_else"] == "~other/x"
    assert paths["not_leading"] == "a/~/b"


def test_the_overlay_is_expanded_too(root: Path, home: Path):
    profile, _ = load_profile("default")
    assert profile["paths"]["feedback_root"] == f"{home.as_posix()}/.claude/feedback"
    assert profile["paths"]["claude_home"] == f"{home.as_posix()}/.claude"


def test_bootstrap_compares_the_expanded_path(root: Path, home: Path):
    profile = bootstrap.load_profile_raw(root, "default")
    assert profile["paths"]["claude_home"] == f"{home.as_posix()}/.claude"


# ---------------------------------------------------------------- the writing side
#
# `~` in a profile is only half of it. `radar bootstrap --write` writes the starter
# profile into the TRACKED `environments/<env>.toml`. A writer that emitted the discovered
# paths verbatim would produce a file that a commit lane running `radar redact-backfill
# --check` refuses, because that maps the home out of every tracked text file. So the
# writer emits `~`, the form the readers above already expand.


class TestTheStarterProfileWritesTilde:
    def discovered(self, home: Path) -> dict[str, str]:
        return {
            "documents": f"{home.as_posix()}/Documents",
            "claude_home": f"{home.as_posix()}/.claude",
            "feedback_root": f"{home.as_posix()}/.claude/feedback",
            "elsewhere": "/opt/shared",
            "missing": "",
        }

    def test_no_discovered_path_carries_the_home(self, home: Path):
        text = bootstrap.starter_profile("laptop", self.discovered(home))
        assert home.as_posix() not in text, text
        assert str(home) not in text, text
        assert 'claude_home = "~/.claude"' in text, text
        assert 'documents = "~/Documents"' in text, text

    def test_a_path_outside_the_home_is_written_as_it_is(self, home: Path):
        text = bootstrap.starter_profile("laptop", self.discovered(home))
        assert 'elsewhere = "/opt/shared"' in text, text
        # A key nothing was found for keeps its commented FILL IN line.
        assert '# missing = "FILL IN' in text, text

    def test_the_profile_it_writes_loads_back_to_the_discovered_paths(self, root: Path, home: Path):
        # The whole point: the tracked file names no account, and the readers give the
        # same absolute paths the discovery found.
        found = self.discovered(home)
        (root / "environments" / "laptop.toml").write_text(
            bootstrap.starter_profile("laptop", found), encoding="utf-8"
        )
        loaded = load_environment("laptop", root)["paths"]
        for key, value in found.items():
            if value:
                assert loaded[key] == value, key

    def test_a_second_bootstrap_run_reports_no_drift(self, root: Path, home: Path):
        # `main()` compares the DECLARED paths with the discovered ones. If the profile it
        # wrote read back as a literal `~`, every path would look like drift and the
        # command would propose an overlay against the file it had just written.
        found = self.discovered(home)
        (root / "environments" / "laptop.toml").write_text(
            bootstrap.starter_profile("laptop", found), encoding="utf-8"
        )
        declared = bootstrap.load_profile_raw(root, "laptop")["paths"]
        drift = {k: v for k, v in found.items() if v and declared.get(k, "") != v}
        assert drift == {}, drift

    def test_a_bare_home_becomes_a_bare_tilde(self, home: Path):
        text = bootstrap.starter_profile("laptop", {"documents": home.as_posix()})
        assert 'documents = "~"' in text, text

    def test_a_sibling_of_the_home_is_not_collapsed(self, home: Path):
        # `<home>-old` is another directory, not this one. The same guard the redaction
        # applies: a name the home is a prefix of is somebody else's.
        sibling = f"{home.as_posix()}-old/Documents"
        text = bootstrap.starter_profile("laptop", {"documents": sibling})
        assert f'documents = "{sibling}"' in text, text


def _bootstrap(root, home, monkeypatch, capsys, *args):
    monkeypatch.chdir(root)
    monkeypatch.delenv("RADAR_DATA_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["bootstrap", "--env", "default", *args])
    try:
        bootstrap.main()
    except SystemExit:
        pass
    return capsys.readouterr().out


class TestADeclaredPathThatIsAbsentIsNotCalledAMatch:
    """A profile can declare a path that does not exist on this machine. Bootstrap must
    not print it `ok` and conclude the machine matches, because the next step (`radar
    apply --plan`) then warns about the very paths bootstrap called a match.

    The `home` fixture exists but holds no `.claude` and no `Documents`, so the profile's
    `~/.claude` and `~/Documents` are declared and absent."""

    def test_an_empty_home_is_not_reported_as_a_match(self, root, home, monkeypatch, capsys):
        out = _bootstrap(root, home, monkeypatch, capsys)
        assert "matches this machine" not in out, out
        assert "declared path(s) not found here" in out, out
        assert "claude_home" in out and "documents" in out, out
        # The absent keys are flagged in the per-path table, not shown as ok.
        table = [ln for ln in out.splitlines() if "claude_home" in ln and "declared=" in ln]
        assert table and table[0].lstrip().startswith("!!"), out

    def test_a_declared_path_that_exists_is_still_a_match(self, root, home, monkeypatch, capsys):
        (home / ".claude").mkdir(parents=True)
        (home / "Documents").mkdir()
        (home / ".claude.json").write_text("{}\n", encoding="utf-8")
        (home / ".claude" / "feedback").mkdir()
        out = _bootstrap(root, home, monkeypatch, capsys)
        assert "not found here" not in out, out

    def test_write_names_a_stale_overlay_key_whose_path_is_absent(
        self, root, home, monkeypatch, capsys
    ):
        # The overlay declares feedback_root under a home that does not exist here.
        out = _bootstrap(root, home, monkeypatch, capsys, "--write")
        assert "not found here" in out, out
        assert "feedback_root" in out, out

    def test_through_the_launcher_so_the_engine_is_the_one_under_test(self, tmp_path):
        # The in-process tests above patch Path.home; this one runs the engine as a
        # subprocess with an empty HOME, so RADAR_ENGINE_SRC chooses which engine is
        # judged (the red proof for this change).
        from engine_run import hermetic_env, run_engine

        root = tmp_path / "radar"
        (root / "environments").mkdir(parents=True)
        (root / "radar.toml").write_text(MARKER, encoding="utf-8")
        (root / "environments" / "default.toml").write_text(
            'name = "default"\nrings = ["own"]\n\n[paths]\n'
            'claude_home = "~/.claude"\ndocuments = "~/Documents"\n',
            encoding="utf-8",
        )
        empty = tmp_path / "empty-home"
        done = run_engine("bootstrap", ["--env", "default"], cwd=root, env=hermetic_env(empty))
        assert done.returncode == 0, done.stdout + done.stderr
        assert "matches this machine" not in done.stdout, done.stdout
        assert "declared path(s) not found here" in done.stdout, done.stdout
