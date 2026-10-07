"""`radar reconcile`: what is installed that the catalogue does not account for.

Reconcile compared declared rings with evidence in one direction only - does each
adopt/pilot entry have something behind it. The other direction is the machine carrying
things the catalogue never mentions, or mentions only to say they are not in use. Both are
read from the installers' own records - uv's tool directory, Claude Code's
`installed_plugins.json`, the user skills directory - so listing them runs no catalogue
command. The report proposes nothing and changes no exit code.

Its check probes are catalogue commands like any other, and run only once approved.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine_run import Catalogue, engine_eval, entry, probe_script


def uv_tool(home: Path, package: str, version: str) -> None:
    env = home / "uv-tools" / package
    env.mkdir(parents=True)
    (env / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "{package}" }}]\n', encoding="utf-8"
    )
    (env / "Lib" / "site-packages" / f"{package.replace('-', '_')}-{version}.dist-info").mkdir(
        parents=True
    )


def synthetic_machine(cat: Catalogue) -> None:
    """A home with three uv tools, two plugins and two skills, against four entries."""
    uv_tool(cat.home, "widget", "1.0.0")  # entry at adopt: accounted for
    uv_tool(cat.home, "loose-tool", "0.3.0")  # no entry
    uv_tool(cat.home, "ruff", "0.9.1")  # entry at discard
    record = cat.claude_home / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "docs-server@market": [{"version": "2.1.0"}],  # entry at observe
                    "stray-plugin@market": [{"version": "0.1.0"}],  # no entry
                },
            }
        ),
        encoding="utf-8",
    )
    (cat.claude_home / "skills" / "my-skill").mkdir(parents=True)  # no entry
    (cat.claude_home / "skills" / "just").mkdir(parents=True)  # entry at adopt
    cat.tool("widget", entry("widget"))
    cat.tool("just", entry("just"))
    cat.tool("ruff", entry("ruff", ring="discard"))
    cat.tool("docs-server", entry("docs-server", ring="observe", artifact="cc-plugin"))


def section(stdout: str, heading: str) -> list[str]:
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    out = []
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        out.append(line.strip())
    return out


def reconcile(cat: Catalogue, *args: str):
    projects = cat.base / "no-projects"
    projects.mkdir(exist_ok=True)
    return cat.run("reconcile", "--env", "default", "--projects-root", str(projects), *args)


def test_installed_things_with_no_entry_are_listed(tmp_path: Path):
    cat = Catalogue(tmp_path)
    synthetic_machine(cat)
    done = reconcile(cat, "--no-probe")
    unlisted = section(done.stdout, "installed, with no catalogue entry")
    assert [line.split()[:3] for line in unlisted] == [
        ["uv", "tool", "loose-tool"],
        ["plugin", "stray-plugin@market", "0.1.0"],
        ["skill", "my-skill"],
    ], done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


def test_installed_things_whose_entry_is_idle_are_listed(tmp_path: Path):
    cat = Catalogue(tmp_path)
    synthetic_machine(cat)
    done = reconcile(cat, "--no-probe")
    idle = section(done.stdout, "installed, while the catalogue says observe or discard")
    assert len(idle) == 2, done.stdout
    assert idle[0].startswith("uv tool  ruff 0.9.1") and "ring=discard" in idle[0], idle
    assert idle[1].startswith("plugin   docs-server@market") and "ring=observe" in idle[1], idle


def test_a_pending_check_is_not_run_and_demotes_nothing(tmp_path: Path):
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker, code=1)
    cat.tool(
        "widget", entry("widget", install={"kind": "manual", "check": check, "instruction": "x"})
    )
    done = reconcile(cat)
    assert not marker.exists(), "reconcile ran a check probe without an approval"
    assert "[UNTRUSTED] widget: [install].check not run" in done.stdout, done.stdout
    assert "widget: probe not approved - presence unknown, not demoted" in done.stdout
    assert "demote  widget" not in done.stdout
    # Unknown is not backed: the summary must not claim evidence it never saw.
    assert "every adopt/pilot entry is backed" not in done.stdout, done.stdout
    assert "no demotion proposed; 1 entry could not be judged" in done.stdout, done.stdout
    assert done.returncode != 0

    # The run above listed the probe, so the flag approves it.
    approved = reconcile(cat, "--trust-commands")
    assert marker.exists(), approved.stdout + approved.stderr
    # Approved and absent: now the evidence is real, and the proposal follows it.
    assert "demote  widget: adopt -> observe" in approved.stdout, approved.stdout


def test_a_github_registry_is_not_a_package_name(tmp_path: Path):
    done = engine_eval(
        "from stack_radar.reconcile import identities\n"
        "print(sorted(identities({'name': 'widget', 'registry': 'github:example-org/w'})))\n",
        tmp_path / "home",
        tmp_path,
    )
    assert done.stdout.strip() == "['widget']", done.stdout + done.stderr


def test_an_entry_not_wanted_here_is_not_probed(tmp_path: Path):
    # Excluded or not wanted in this environment: reported and not judged, so its probe
    # would only ask for an approval whose answer is thrown away.
    cat = Catalogue(tmp_path)
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker)
    cat.tool(
        "widget", entry("widget", install={"kind": "manual", "check": check, "instruction": "x"})
    )
    cat.profile(
        'name = "default"\nrings = ["own", "adopt", "pilot"]\nexclude = ["widget"]\n\n'
        f'[paths]\nclaude_home = "{cat.claude_home.as_posix()}"\n'
    )
    done = reconcile(cat, "--trust-commands")
    assert not marker.exists(), "an excluded entry's probe ran"
    assert "[UNTRUSTED]" not in done.stdout and "[TRUSTED]" not in done.stdout, done.stdout
    assert "widget: excluded by profile (adopt); not judged here" in done.stdout, done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_entry_the_installer_records_list_is_not_proposed_for_demotion(tmp_path: Path):
    # No check probe, so nothing ran - but uv's own record lists the tool, and that is
    # evidence of presence. Proposing its demotion would contradict the inventory printed
    # below it in the same run.
    cat = Catalogue(tmp_path)
    uv_tool(cat.home, "widget", "1.0.0")
    cat.tool("widget", entry("widget", install={"kind": "uv-tool"}))
    done = reconcile(cat)
    assert "demote  widget" not in done.stdout, done.stdout
    assert "installed (uv tool record)" in done.stdout, done.stdout
    assert "every adopt/pilot entry is backed by observed evidence." in done.stdout
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_entry_not_judged_here_is_counted_in_the_summary(tmp_path: Path):
    cat = Catalogue(tmp_path)
    cat.tool("widget", entry("widget", install={"kind": "none"}))
    done = reconcile(cat)
    assert "every adopt/pilot entry is backed" not in done.stdout, done.stdout
    assert "no demotion proposed; 1 entry not judged here" in done.stdout, done.stdout


def test_a_record_is_evidence_when_the_probe_did_not_answer(tmp_path: Path):
    # A probe that has not been approved ran no more than a missing one: the installer's
    # record is the only observation, and it backs the entry.
    cat = Catalogue(tmp_path)
    uv_tool(cat.home, "widget", "1.0.0")
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker, code=1)
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": "x"}))
    done = reconcile(cat)
    assert not marker.exists(), "reconcile ran a check probe without an approval"
    assert "installed (uv tool record)" in done.stdout, done.stdout
    assert "demote  widget" not in done.stdout, done.stdout


def test_an_approved_probe_that_disagrees_with_a_record_is_a_note(tmp_path: Path):
    # The probe ran and said absent, while uv's record lists the tool. Neither wins
    # silently: the record is not counted as evidence, the disagreement is named, and the
    # entry is left alone, because its presence is exactly what the two disagree about.
    cat = Catalogue(tmp_path)
    uv_tool(cat.home, "widget", "1.0.0")
    marker = cat.markers / "probe-ran"
    check = probe_script(cat.base / "probes" / "probe.py", marker, code=1)
    cat.tool("widget", entry("widget", install={"kind": "uv-tool", "check": check, "apply": "x"}))
    reconcile(cat)
    done = reconcile(cat, "--trust-commands")
    assert marker.exists(), done.stdout + done.stderr
    assert "installed (uv tool record)" not in done.stdout, done.stdout
    assert (
        "[NOTE] widget: the approved check probe says absent, while the uv tool record "
        "lists it installed" in done.stdout
    ), done.stdout
    assert "demote  widget" not in done.stdout, done.stdout
    assert "no demotion proposed; 1 entry could not be judged" in done.stdout, done.stdout


def test_the_catalogue_s_own_pyproject_is_not_a_project(tmp_path: Path):
    # The default projects root is the directory holding the data root, and the catalogue
    # carries a pyproject.toml of its own with its lint tools in it. That is not evidence
    # that any project uses them.
    cat = Catalogue(tmp_path)
    (cat.root / "pyproject.toml").write_text(
        '[project]\nname = "catalogue"\nversion = "0.1.0"\n\n'
        '[dependency-groups]\ndev = ["ruff>=0.13"]\n\n[tool.ruff]\nline-length = 100\n',
        encoding="utf-8",
    )
    (cat.root / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: ruff\n", encoding="utf-8"
    )
    cat.tool("ruff", entry("ruff"))
    done = cat.run("reconcile", "--env", "default", "--no-probe")
    assert "dep in 1 project" not in done.stdout, done.stdout
    assert "configured in 1" not in done.stdout, done.stdout
    assert "demote  ruff: adopt -> observe" in done.stdout, done.stdout
