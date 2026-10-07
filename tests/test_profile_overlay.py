"""Every verb that reads a profile layers the overlay the profile names.

A profile is tracked and is true of every machine that pulls the catalogue; the paths one
workstation disagrees on go in `environments/<ENV>.local.toml`, which is gitignored and
which `radar bootstrap --write` writes and keeps. The profile names it with
`overlay = "<ENV>.local.toml"` under `[flags]`, and every reader layers it through the same
resolver (`radar_lib.load_profile`): the overlay may set `description`, `rings`, `exclude`,
`require_env`, `[paths]` and `[flags]`; tables merge key by key, `exclude` and
`require_env` accumulate, `description` and `rings` replace, and any other key is refused.
`radar bootstrap` compares the profile with its overlay layered on against the machine, so
a stale overlay is found, and `--write` changes only the overlay's `[paths]`. So a
`claude_home` fixed in the overlay is the `claude_home` that `radar field`,
`radar versions`, `radar render-feedback-targets` and the gate's invariant scan read, and a
`feedback_root` fixed there is the one `radar sync-feedback` reads.

A `.local.toml` the profile does not name is not read: the profile says which file is its
overlay, so a stray file beside it changes nothing. An overlay that cannot be read, or that
gives a value a type the profile's readers do not expect (a table that is not a table, a
path that is not a string, a key whose type differs from the committed profile's), is a
`[FAIL]` naming the file, and the command stops before it acts on a half-read profile. A
committed profile of the wrong shape is the same `[FAIL]`, naming the committed file.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from engine_run import Catalogue, entry

OVERLAY_FLAG = '\n[flags]\noverlay = "default.local.toml"\n'


def committed(cat: Catalogue, *, names_overlay: bool = True, extra: str = "") -> Path:
    """The tracked profile: its `claude_home` is a directory that exists and holds nothing."""
    home = cat.base / "committed-home"
    home.mkdir(exist_ok=True)
    body = (
        'name = "default"\nrings = ["own", "adopt", "pilot"]\n\n[paths]\n'
        f'claude_home = "{home.as_posix()}"\n{extra}'
    )
    cat.profile(body + (OVERLAY_FLAG if names_overlay else ""))
    return home


def overlay(cat: Catalogue, body: str) -> Path:
    path = cat.root / "environments" / "default.local.toml"
    path.write_text(body, encoding="utf-8")
    return path


def machine_home(cat: Catalogue) -> Path:
    """The `claude_home` only the overlay names, as `radar bootstrap --write` writes it."""
    home = cat.base / "machine-home"
    home.mkdir(exist_ok=True)
    overlay(cat, f'name = "default"\n\n[paths]\nclaude_home = "{home.as_posix()}"\n')
    return home


def no_traceback(done) -> None:
    assert "Traceback" not in done.stderr, done.stderr


# --------------------------------------------------------------- each reader


def test_field_reads_the_overlay_claude_home(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    home = machine_home(cat)
    done = cat.run("field_report", "widget", "--env", "default")
    assert f"no transcript directory at {home / 'projects'}" in done.stdout, done.stdout


def test_versions_reads_the_overlay_claude_home(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    home = machine_home(cat)
    record = home / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"version": 2, "plugins": {"docs-server@market": [{"version": "2.1.0"}]}}),
        encoding="utf-8",
    )
    cat.tool(
        "docs-server",
        entry(
            "docs-server",
            artifact="cc-plugin",
            install={"kind": "claude-plugin", "check": "claude plugin list", "apply": "x"},
        ),
    )
    done = cat.run("versions", "--env", "default", "--no-plugins", "--offline")
    no_traceback(done)
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("docs-server "))
    assert "2.1.0" in line, done.stdout


def test_sync_feedback_reads_the_overlay_feedback_root(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    inbox = cat.base / "inbox"
    inbox.mkdir()
    overlay(cat, f'[paths]\nfeedback_root = "{inbox.as_posix()}"\n')
    done = cat.run("sync_feedback", "--env", "default", "--check")
    no_traceback(done)
    assert "feedback_root is not set" not in done.stdout, done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


def test_render_feedback_targets_reads_the_overlay_claude_home(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    home = cat.base / "machine-home"
    home.mkdir()
    dest = home / "feedback-targets.toml"
    # Inside the overlay's claude_home and outside the committed one: only a reader that
    # layered the overlay accepts this destination.
    overlay(
        cat,
        f'[paths]\nclaude_home = "{home.as_posix()}"\nfeedback_targets = "{dest.as_posix()}"\n',
    )
    done = cat.run("render_feedback_targets", "--env", "default")
    no_traceback(done)
    assert "refused" not in done.stdout, done.stdout
    assert dest.is_file(), done.stdout


def test_the_gate_scans_the_overlay_claude_home(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    home = machine_home(cat)
    (home / "settings.json").write_text(
        json.dumps({"note": f"{cat.root.as_posix()}/tools"}), encoding="utf-8"
    )
    done = cat.run("gate")
    no_traceback(done)
    assert "[FAIL] invariant:" in done.stdout, done.stdout
    assert "settings.json" in done.stdout, done.stdout


# ------------------------------------------------------ which file, and when not


def test_a_profile_that_names_no_overlay_ignores_a_stray_local_file(tmp_path: Path):
    cat = Catalogue(tmp_path)
    home = committed(cat, names_overlay=False)
    stray = machine_home(cat)
    done = cat.run("field_report", "widget", "--env", "default")
    assert f"no transcript directory at {home / 'projects'}" in done.stdout, done.stdout
    assert str(stray) not in done.stdout, done.stdout


def test_an_overlay_leaves_the_keys_it_does_not_set(tmp_path: Path):
    cat = Catalogue(tmp_path)
    inbox = cat.base / "inbox"
    inbox.mkdir()
    committed(cat, extra=f'feedback_root = "{inbox.as_posix()}"\n')
    machine_home(cat)
    done = cat.run("sync_feedback", "--env", "default", "--check")
    assert "feedback_root is not set" not in done.stdout, done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


# ------------------------------------------------------------- a broken overlay


def test_an_overlay_that_is_not_toml_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    overlay(cat, "[paths\nclaude_home = \n")
    for module, args in (
        ("versions", ("--env", "default", "--offline")),
        ("field_report", ("widget", "--env", "default")),
        ("sync_feedback", ("--env", "default", "--check")),
        ("render_feedback_targets", ("--env", "default", "--stdout")),
        ("apply", ("--env", "default", "--plan")),
        ("reconcile", ("--env", "default")),
        ("gate", ()),
    ):
        done = cat.run(module, *args)
        no_traceback(done)
        assert "[FAIL] environments/default.local.toml:" in done.stdout, (module, done.stdout)
        assert done.returncode == 1, (module, done.stdout + done.stderr)


def test_an_overlay_that_gives_a_table_as_a_value_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    overlay(cat, 'paths = "elsewhere"\n')
    done = cat.run("versions", "--env", "default", "--offline")
    no_traceback(done)
    assert "[FAIL] environments/default.local.toml:" in done.stdout, done.stdout
    assert "[paths]" in done.stdout, done.stdout
    assert done.returncode == 1


VERBS = (
    ("versions", ("--env", "default", "--offline")),
    ("field_report", ("--all", "--env", "default")),
    ("sync_feedback", ("--env", "default", "--check")),
    ("render_feedback_targets", ("--env", "default", "--stdout")),
    ("apply", ("--env", "default", "--plan")),
    ("reconcile", ("--env", "default")),
    ("gate", ()),
)


def named_fail(done, file: str, key: str) -> None:
    no_traceback(done)
    assert f"[FAIL] environments/{file}:" in done.stdout, done.stdout
    assert key in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def test_an_overlay_path_that_is_not_a_string_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    overlay(cat, "[paths]\nclaude_home = 5\n")
    for module, args in VERBS:
        named_fail(cat.run(module, *args), "default.local.toml", "[paths].claude_home")


def test_an_overlay_value_whose_type_differs_from_the_profile_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat, names_overlay=False)
    profile = cat.root / "environments" / "default.toml"
    profile.write_text(
        profile.read_text(encoding="utf-8")
        + '\n[flags]\noverlay = "default.local.toml"\nallow_network_install = false\n',
        encoding="utf-8",
    )
    for body, key in (
        ('rings = "own"\n', "`rings`"),
        ("exclude = [1]\n", "`exclude`"),
        ('[flags]\nallow_network_install = "yes"\n', "[flags].allow_network_install"),
    ):
        overlay(cat, body)
        named_fail(cat.run("versions", "--env", "default", "--offline"), "default.local.toml", key)


def test_a_committed_profile_value_of_the_wrong_type_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.profile('name = "default"\nrings = ["own"]\n\n[paths]\nclaude_home = 5\n')
    for module, args in (*VERBS, ("bootstrap", ("--env", "default"))):
        done = cat.run(module, *args)
        no_traceback(done)
        assert "[FAIL] environments/default.toml:" in done.stdout, (module, done.stdout)
        assert "[paths].claude_home" in done.stdout, (module, done.stdout)
        assert done.returncode == 1, (module, done.stdout + done.stderr)


def test_bootstrap_names_a_committed_profile_that_is_not_toml(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.profile('name = "default"\n[paths\n')
    done = cat.run("bootstrap", "--env", "default")
    no_traceback(done)
    assert "[FAIL] environments/default.toml: not readable as TOML" in done.stdout, done.stdout
    assert done.returncode == 1, done.stdout + done.stderr


def test_an_overlay_name_outside_environments_is_a_named_fail(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat, names_overlay=False, extra='\n[flags]\noverlay = "../radar.toml"\n')
    done = cat.run("versions", "--env", "default", "--offline")
    no_traceback(done)
    assert "[FAIL] environments/default.toml:" in done.stdout, done.stdout
    assert "overlay" in done.stdout, done.stdout
    assert done.returncode == 1


# ------------------------------------------------------- the overlay bootstrap writes


def test_the_overlay_bootstrap_writes_reaches_the_other_verbs(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    done = cat.run("bootstrap", "--env", "default", "--write")
    assert done.returncode == 0, done.stdout + done.stderr
    written = cat.root / "environments" / "default.local.toml"
    assert written.is_file(), done.stdout
    assert "layers this file over" in written.read_text(encoding="utf-8")
    assert "does not name this overlay" not in done.stdout, done.stdout
    field = cat.run("field_report", "widget", "--env", "default")
    assert f"no transcript directory at {cat.claude_home / 'projects'}" in field.stdout, (
        field.stdout
    )


def test_bootstrap_says_when_the_profile_does_not_name_its_overlay(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat, names_overlay=False)
    done = cat.run("bootstrap", "--env", "default")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "does not name this overlay" in done.stdout, done.stdout
    assert 'overlay = "default.local.toml" under [flags]' in done.stdout, done.stdout


# ------------------------------------------------------- the keys an overlay may set


def test_a_key_an_overlay_may_not_set_is_a_named_fail(tmp_path: Path):
    # The overlay may set `description`, `rings`, `exclude`, `require_env`, `[paths]` and
    # `[flags]`; which file is the overlay is the committed profile's to say.
    cat = Catalogue(tmp_path)
    committed(cat)
    for body, key in (
        ('overlay = "other.local.toml"\n', "`overlay`"),
        ('[flags]\noverlay = "other.local.toml"\n', "[flags].overlay"),
        ('schedule = "weekly"\n', "`schedule`"),
    ):
        overlay(cat, body)
        named_fail(cat.run("versions", "--env", "default", "--offline"), "default.local.toml", key)


# The machine's documents folder is found through these; cleared, so bootstrap finds only
# what the test's home holds.
NO_ONEDRIVE = {"OneDrive": "", "OneDriveCommercial": ""}


def test_bootstrap_write_keeps_what_the_overlay_already_sets(tmp_path: Path):
    cat = Catalogue(tmp_path)
    committed(cat)
    inbox = cat.base / "inbox"
    written = overlay(
        cat,
        'name = "default"\nexclude = ["gadget"]\n\n[paths]\n'
        f'claude_home = "{(cat.base / "old-home").as_posix()}"\n'
        f'extra_dir = "{inbox.as_posix()}"\n\n[flags]\nallow_network_install = true\n',
    )
    done = cat.run("bootstrap", "--env", "default", "--write", extra=NO_ONEDRIVE)
    assert done.returncode == 0, done.stdout + done.stderr
    kept = tomllib.loads(written.read_text(encoding="utf-8"))
    assert kept["flags"] == {"allow_network_install": True}, kept
    assert kept["exclude"] == ["gadget"], kept
    assert kept["paths"]["extra_dir"] == inbox.as_posix(), kept
    assert kept["paths"]["claude_home"] == cat.claude_home.as_posix(), kept
    assert "every other key kept" in done.stdout, done.stdout


def matching_profile(cat: Catalogue) -> None:
    """A committed profile whose paths are exactly what bootstrap discovers here."""
    chome = cat.claude_home.as_posix()
    cat.profile(
        'name = "default"\nrings = ["own", "adopt", "pilot"]\n\n[paths]\n'
        f'claude_home = "{chome}"\nfeedback_targets = "{chome}/feedback-targets.toml"\n'
        f'feedback_root = "{chome}/feedback"\n' + OVERLAY_FLAG
    )


def test_bootstrap_notices_a_stale_overlay(tmp_path: Path):
    # The committed profile matches the machine, but a leftover overlay points claude_home
    # somewhere else - and every other verb layers that overlay.
    cat = Catalogue(tmp_path)
    matching_profile(cat)
    clean = cat.run("bootstrap", "--env", "default", extra=NO_ONEDRIVE)
    assert "no overlay needed" in clean.stdout, clean.stdout

    stale = (cat.base / "nowhere" / ".claude").as_posix()
    written = overlay(cat, f'name = "default"\n\n[paths]\nclaude_home = "{stale}"\n')
    done = cat.run("bootstrap", "--env", "default", extra=NO_ONEDRIVE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "no overlay needed" not in done.stdout, done.stdout
    assert f"declared={stale} (overlay)" in done.stdout, done.stdout

    fixed = cat.run("bootstrap", "--env", "default", "--write", extra=NO_ONEDRIVE)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "claude_home" not in tomllib.loads(written.read_text(encoding="utf-8"))["paths"]
    field = cat.run("field_report", "widget", "--env", "default")
    assert f"no transcript directory at {cat.claude_home / 'projects'}" in field.stdout, (
        field.stdout
    )


def test_bootstrap_does_not_call_an_unnamed_overlay_a_match(tmp_path: Path):
    # The overlay holds this machine's claude_home and the committed profile a wrong one,
    # but the profile does not name the overlay, so every verb reads the wrong path.
    cat = Catalogue(tmp_path)
    chome = cat.claude_home.as_posix()
    wrong = (cat.base / "wrong" / ".claude").as_posix()
    cat.profile(
        'name = "default"\nrings = ["own", "adopt", "pilot"]\n\n[paths]\n'
        f'claude_home = "{wrong}"\nfeedback_targets = "{chome}/feedback-targets.toml"\n'
        f'feedback_root = "{chome}/feedback"\n'
    )
    overlay(cat, f'name = "default"\n\n[paths]\nclaude_home = "{chome}"\n')
    done = cat.run("bootstrap", "--env", "default", extra=NO_ONEDRIVE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"declared={wrong}" in done.stdout, done.stdout
    assert "every declared path matches" not in done.stdout, done.stdout
    assert "default.local.toml already sets claude_home" in done.stdout, done.stdout
    assert "does not name this overlay" in done.stdout, done.stdout


def test_bootstrap_says_an_unnamed_overlay_is_not_read_when_nothing_differs(tmp_path: Path):
    cat = Catalogue(tmp_path)
    matching_profile(cat)
    cat.profile(
        (cat.root / "environments" / "default.toml")
        .read_text(encoding="utf-8")
        .replace(OVERLAY_FLAG, "\n")
    )
    overlay(cat, 'name = "default"\nexclude = ["gadget"]\n')
    done = cat.run("bootstrap", "--env", "default", extra=NO_ONEDRIVE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "every declared path matches" in done.stdout, done.stdout
    assert "does not name this overlay" in done.stdout, done.stdout
