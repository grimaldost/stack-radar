"""`radar versions`: where an installed version is read from, and the `github:` upstream.

The installed version comes from the installer's own record wherever the engine can read
one - uv's tool directory, Claude Code's `installed_plugins.json`, the tags of a local
working tree - and from the entry's `[install].check` only where it cannot. The records
need no catalogue command, so reading them needs no approval; a check probe is catalogue
text and runs only once approved.

A `github:<owner>/<repo>` registry resolves to the repository's highest X.Y.Z tag through
`git ls-remote --tags`, a fixed argv the engine writes. Pre-release tags are ignored, and
`--offline` skips the lookup like every other upstream lookup.

Every fixture here is synthetic: a uv tool directory with one receipt, a plugin record,
a git repository made in tmp_path, and a fake `git` that records the argv it was given.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from engine_run import Catalogue, entry, fake_program, git, probe_script

from stack_radar import versions

LS_REMOTE = (
    "1111111111111111111111111111111111111111\trefs/tags/v1.9.0\n"
    "2222222222222222222222222222222222222222\trefs/tags/v2.0.0\n"
    "3333333333333333333333333333333333333333\trefs/tags/2.0.1\n"
    "4444444444444444444444444444444444444444\trefs/tags/2.0.1^{}\n"
    "5555555555555555555555555555555555555555\trefs/tags/v2.1.0-rc1\n"
    "6666666666666666666666666666666666666666\trefs/tags/nightly\n"
)


def uv_tool(home: Path, package: str, version: str) -> None:
    """One tool environment the way uv lays it out: a receipt, and the package's dist-info."""
    env = home / "uv-tools" / package
    env.mkdir(parents=True)
    (env / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "{package}" }}]\n', encoding="utf-8"
    )
    site = (
        env / "Lib" / "site-packages"
        if sys.platform == "win32"
        else env / "lib" / "python3.12" / "site-packages"
    )
    (site / f"{package.replace('-', '_')}-{version}.dist-info").mkdir(parents=True)


def row(stdout: str, name: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith(name + " "))


def recording_git(directory: Path, argv_file: Path) -> None:
    fake_program(
        directory,
        "git",
        "import json, pathlib, sys\n"
        f"pathlib.Path({argv_file.as_posix()!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write({LS_REMOTE!r})\n",
    )


# ------------------------------------------------------------ installed records


def test_a_uv_tool_is_read_from_uv_s_records_and_its_check_does_not_run(tmp_path: Path):
    cat = Catalogue(tmp_path)
    uv_tool(cat.home, "widget", "1.4.0")
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker)
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": "x"}))
    done = cat.run("versions", "--env", "default", "--no-plugins", "--offline")
    assert "1.4.0" in row(done.stdout, "widget"), done.stdout
    assert not marker.exists(), "the check ran although uv's own record answered"
    assert "[UNTRUSTED]" not in done.stdout, done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_plugin_is_read_from_installed_plugins_json(tmp_path: Path):
    cat = Catalogue(tmp_path)
    record = cat.claude_home / "plugins" / "installed_plugins.json"
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
    assert "2.1.0" in row(done.stdout, "docs-server"), done.stdout
    assert "[UNTRUSTED]" not in done.stdout, done.stdout


def test_a_local_repository_is_read_from_its_checked_out_tag(tmp_path: Path):
    cat = Catalogue(tmp_path)
    repo = tmp_path / "repos" / "widget"
    repo.mkdir(parents=True)
    git(repo, cat.home, "init", "-q")
    (repo / "f").write_text("1", encoding="utf-8")
    git(repo, cat.home, "add", "f")
    git(repo, cat.home, "commit", "-q", "-m", "one")
    git(repo, cat.home, "tag", "v1.2.0")
    (repo / "f").write_text("2", encoding="utf-8")
    git(repo, cat.home, "commit", "-q", "-am", "two")
    git(repo, cat.home, "tag", "v1.3.0")
    cat.tool(
        "widget",
        entry(
            "widget",
            ring="own",
            top={"repo": repo.as_posix(), "visibility": "private"},
            install={"kind": "repo", "check": "git status"},
        ),
    )
    done = cat.run("versions", "--env", "default", "--no-plugins", "--offline")
    assert "1.3.0" in row(done.stdout, "widget"), done.stdout
    assert "[UNTRUSTED]" not in done.stdout, done.stdout


def test_without_git_the_tags_are_read_from_the_git_directory(tmp_path: Path):
    cat = Catalogue(tmp_path)
    repo = tmp_path / "repos" / "widget"
    repo.mkdir(parents=True)
    git(repo, cat.home, "init", "-q")
    (repo / "f").write_text("1", encoding="utf-8")
    git(repo, cat.home, "add", "f")
    git(repo, cat.home, "commit", "-q", "-m", "one")
    git(repo, cat.home, "tag", "v1.2.0")
    git(repo, cat.home, "tag", "v1.10.0")
    git(repo, cat.home, "pack-refs", "--all")
    git(repo, cat.home, "tag", "v2.0.0-rc1")
    cat.tool(
        "widget",
        entry(
            "widget",
            ring="own",
            top={"repo": repo.as_posix(), "visibility": "private"},
            install={"kind": "repo", "check": "git status"},
        ),
    )
    no_git = tmp_path / "empty-path"
    no_git.mkdir()
    done = cat.run(
        "versions", "--env", "default", "--no-plugins", "--offline", extra={"PATH": str(no_git)}
    )
    assert "1.10.0" in row(done.stdout, "widget"), done.stdout


def test_uv_tool_list_answers_when_the_tool_directory_is_not_there(tmp_path: Path):
    cat = Catalogue(tmp_path)
    fake_program(
        tmp_path / "fake-bin",
        "uv",
        "print('widget v1.7.0')\nprint('- widget')\nprint('other-tool v0.1.0')\n",
    )
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "check": "x", "apply": "x"}))
    done = cat.run(
        "versions",
        "--env",
        "default",
        "--no-plugins",
        "--offline",
        extra={"PATH": str(tmp_path / "fake-bin"), "UV_TOOL_DIR": str(tmp_path / "absent")},
    )
    assert "1.7.0" in row(done.stdout, "widget"), done.stdout
    assert "[UNTRUSTED]" not in done.stdout, done.stdout


def test_uv_tool_list_output_is_parsed_by_package():
    out = "widget v1.7.0\n- widget\n- widget-extra\nDocs_Server v2.0.0\n- docs\n"
    assert versions.parse_uv_tool_list(out) == {"widget": "1.7.0", "docs-server": "2.0.0"}


def test_repo_tags_reads_loose_and_packed_refs(tmp_path: Path):
    git_dir = tmp_path / "r" / ".git"
    (git_dir / "refs" / "tags" / "team").mkdir(parents=True)
    (git_dir / "refs" / "tags" / "v0.9.0").write_text("a\n", encoding="utf-8")
    (git_dir / "refs" / "tags" / "team" / "x").write_text("a\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "bbbb refs/tags/v1.1.0\n^cccc\n"
        "dddd refs/heads/main\n",
        encoding="utf-8",
    )
    tags = versions.repo_tags(tmp_path / "r")
    assert tags == ["team/x", "v0.9.0", "v1.1.0"]
    assert versions.highest_release(tags) == "1.1.0"


# -------------------------------------------------------------- the check probe


def test_a_pending_check_is_not_run_by_versions(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker, output="widget 3.2.1")
    cat.tool(
        "widget",
        entry(
            "widget",
            top={"registry": "pypi:widget"},
            install={"kind": "manual", "check": check, "instruction": "x"},
        ),
    )
    done = cat.run("versions", "--env", "default", "--no-plugins", "--offline")
    assert not marker.exists(), "versions ran a check probe without an approval"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert "check not approved" in row(done.stdout, "widget"), done.stdout
    assert done.returncode != 0

    approved = cat.run(
        "versions", "--env", "default", "--no-plugins", "--offline", "--trust-commands"
    )
    assert marker.exists(), approved.stdout + approved.stderr
    assert "3.2.1" in row(approved.stdout, "widget"), approved.stdout
    assert approved.returncode == 0, approved.stdout + approved.stderr


def test_under_policy_any_a_pending_check_is_not_a_refusal(tmp_path: Path):
    # No registry and no [version] block: policy=any, so the version is never compared.
    # An unapproved probe is not run and not counted - the run does not fail over a
    # figure nothing reads - and --trust-commands still approves and runs it.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker, output="widget 3.2.1")
    cat.tool(
        "widget", entry("widget", install={"kind": "manual", "check": check, "instruction": "x"})
    )
    done = cat.run("versions", "--env", "default", "--no-plugins", "--offline")
    assert not marker.exists(), "versions ran a check probe without an approval"
    assert "[UNTRUSTED]" not in done.stdout, done.stdout
    # The row says the version is unknown because the probe was not needed, not that an
    # approval is being asked for.
    assert "not needed at policy any" in row(done.stdout, "widget"), done.stdout
    assert "check not approved" not in row(done.stdout, "widget"), done.stdout
    assert done.returncode == 0, done.stdout + done.stderr

    # A quiet run showed the operator nothing, so the flag lists the probe first and
    # approves it on the run after.
    listed = cat.run(
        "versions", "--env", "default", "--no-plugins", "--offline", "--trust-commands"
    )
    assert not marker.exists(), "the flag approved a probe no run had listed"
    assert "[UNTRUSTED] widget: [install].check not run" in listed.stdout, listed.stdout
    approved = cat.run(
        "versions", "--env", "default", "--no-plugins", "--offline", "--trust-commands"
    )
    assert marker.exists(), approved.stdout + approved.stderr
    assert "3.2.1" in row(approved.stdout, "widget"), approved.stdout


@pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows runs a .cmd program through cmd.exe"
)
def test_the_probe_is_judged_as_the_program_versions_would_run(tmp_path: Path):
    # versions runs a probe on the PATH without the running project's virtualenv, so the
    # program it would run is the one found there, and that is the one the approval
    # judges: here a batch file whose path cmd.exe would read as syntax.
    cat = Catalogue(tmp_path)
    venv = cat.base / "venv"
    fake_program(venv / "Scripts", "probe", "print('widget 1.0.0')\n")
    fake_program(cat.base / "g&bin", "probe", "print('widget 2.0.0')\n")
    cat.tool(
        "widget",
        entry(
            "widget",
            top={"registry": "pypi:widget"},
            install={"kind": "manual", "check": "probe --version", "instruction": "x"},
        ),
    )
    env = {
        "VIRTUAL_ENV": str(venv),
        "PATH": os.pathsep.join(
            [str(venv / "Scripts"), str(cat.base / "g&bin"), os.environ.get("PATH", "")]
        ),
    }
    args = ("versions", "--env", "default", "--no-plugins", "--offline")
    cat.run(*args, extra=env)
    done = cat.run(*args, "--trust-commands", extra=env)
    assert "[TRUSTED]" not in done.stdout, done.stdout
    assert "[fail] widget: [install].check not run - probe.cmd is a batch file" in (
        done.stdout.lower()
    ), done.stdout
    assert "g&bin/probe.cmd holds '&'" in done.stdout.lower(), done.stdout
    assert done.returncode != 0


# ------------------------------------------------------------------ github:


def github_entry(cat: Catalogue, registry: str) -> None:
    uv_tool(cat.home, "widget", "2.0.0")
    cat.tool(
        "widget",
        entry(
            "widget",
            top={"registry": registry},
            install={"kind": "uv-tool", "check": "widget --version", "apply": "x"},
        ),
    )


def test_a_github_registry_resolves_to_the_highest_release_tag(tmp_path: Path):
    cat = Catalogue(tmp_path)
    github_entry(cat, "github:example-org/widget")
    argv_file = tmp_path / "git-argv.json"
    recording_git(tmp_path / "fake-bin", argv_file)
    done = cat.run(
        "versions", "--env", "default", "--no-plugins", extra={"PATH": str(tmp_path / "fake-bin")}
    )
    line = row(done.stdout, "widget")
    assert "2.0.0" in line and "2.0.1" in line and "BEHIND" in line, done.stdout
    assert "[WARN] widget: 2.0.0 installed, 2.0.1 available (github:example-org/widget)" in (
        done.stdout
    )
    assert json.loads(argv_file.read_text()) == [
        *versions.GIT_SAFE_CONFIG,
        "-c",
        "credential.helper=",
        "ls-remote",
        "--tags",
        "https://github.com/example-org/widget",
    ]


def test_offline_skips_the_tag_lookup(tmp_path: Path):
    cat = Catalogue(tmp_path)
    github_entry(cat, "github:example-org/widget")
    argv_file = tmp_path / "git-argv.json"
    recording_git(tmp_path / "fake-bin", argv_file)
    done = cat.run(
        "versions",
        "--env",
        "default",
        "--no-plugins",
        "--offline",
        extra={"PATH": str(tmp_path / "fake-bin")},
    )
    assert "offline" in row(done.stdout, "widget"), done.stdout
    assert not argv_file.exists(), "git ran under --offline"


def test_a_malformed_github_registry_runs_nothing(tmp_path: Path):
    cat = Catalogue(tmp_path)
    github_entry(cat, "github:../../elsewhere")
    argv_file = tmp_path / "git-argv.json"
    recording_git(tmp_path / "fake-bin", argv_file)
    done = cat.run(
        "versions", "--env", "default", "--no-plugins", extra={"PATH": str(tmp_path / "fake-bin")}
    )
    assert "malformed github registry" in row(done.stdout, "widget"), done.stdout
    assert not argv_file.exists()


def test_ls_remote_parsing_ignores_pre_releases_and_peeled_lines():
    tags = versions.ls_remote_tags(LS_REMOTE)
    assert tags == ["2.0.1", "nightly", "v1.9.0", "v2.0.0", "v2.1.0-rc1"]
    assert versions.highest_release(tags) == "2.0.1"


def test_a_github_registry_is_not_a_package_identity(tmp_path: Path):
    t = {"name": "widget", "registry": "github:example-org/widget-repo", "aliases": ["wdg"]}
    assert versions.identities(t) == {"widget", "wdg"}
    assert versions.identities({"name": "widget", "registry": "pypi:widget_py"}) == {
        "widget",
        "widget-py",
    }


# ------------------------------------------------------- a registry put into a URL


def test_a_malformed_registry_is_never_put_into_a_request(tmp_path: Path):
    # `radar versions` and `radar snapshot` do not run the gate, so an entry the gate would
    # refuse still reaches them; its target must not reach a URL. Run in a subprocess, so
    # RADAR_ENGINE_SRC decides which engine is judged.
    from engine_run import engine_eval

    code = "\n".join(
        [
            "import urllib.request",
            "from stack_radar import snapshot, versions",
            "asked = []",
            "def refuse(url, *a, **k):",
            "    asked.append(url if isinstance(url, str) else url.full_url)",
            "    raise OSError('offline')",
            "urllib.request.urlopen = refuse",
            "snapshot.time.sleep = lambda s: None",
            "for reg in ('pypi:../../simple/x?y#z', 'npm:../x', 'pypi:a/b'):",
            "    versions.upstream(reg)",
            "    snapshot.downloads(reg)",
            "print('asked', len(asked), asked)",
        ]
    )
    done = engine_eval(code, tmp_path / "home", tmp_path)
    assert "asked 0 []" in done.stdout, done.stdout + done.stderr
