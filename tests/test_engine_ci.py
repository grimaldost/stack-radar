"""This repository's own CI runs the checks the documentation says it runs.

`radar changelog-gate` and `radar check-version-sites` judge the repository they are run
from, and this repository is meant to be judged by them on every pull request. The suite
tests their rules against synthetic trees; only the workflow runs them over this one. A
job that quietly stops running a verb leaves every test green, so the workflow's shape is
asserted here.

The workflow that `radar init` writes is part of what this repository ships, so it is
scanned by the same auditor as this repository's own - rendered, because the rendered
file is what reaches a catalogue.

Read as text rather than parsed: the engine has no YAML parser and gains none for a test.
Each job is the block of lines under its two-space-indented name. The checkout is located
from `RADAR_ENGINE_SRC` when it is set, so the red proof reads an earlier commit's
workflow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKOUT = (
    Path(os.environ["RADAR_ENGINE_SRC"]).parent if os.environ.get("RADAR_ENGINE_SRC") else REPO
)
WORKFLOW = CHECKOUT / ".github" / "workflows" / "ci.yml"

_JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)
_USES = re.compile(r"uses:\s*[\w.-]+/[\w.-]+@(\S+)")


def jobs() -> dict[str, str]:
    """`name -> the job's lines`, for every job under `jobs:`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    marks = list(_JOB.finditer(body))
    return {
        m.group(1): body[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(body)]
        for i, m in enumerate(marks)
    }


def test_the_workflow_has_jobs():
    # Non-vacuity: every assertion below reads a job by name.
    assert {"checks", "zizmor"} <= set(jobs()), sorted(jobs())


def test_the_checks_job_runs_check_version_sites():
    assert "- run: uv run radar check-version-sites" in jobs()["checks"]


def test_a_pull_request_job_runs_the_changelog_gate():
    job = jobs().get("changelog-gate")
    assert job is not None, sorted(jobs())
    assert "if: github.event_name == 'pull_request'" in job, job
    assert 'run: uv run radar changelog-gate "origin/$BASE_REF"' in job, job
    assert "BASE_REF: ${{ github.base_ref }}" in job, job
    # The gate walks every commit since the merge base, which a shallow clone lacks.
    assert "fetch-depth: 0" in job, job
    assert "persist-credentials: false" in job, job


def test_the_zizmor_job_scans_the_workflow_init_writes():
    job = jobs()["zizmor"]
    rendered = re.search(r'uv run radar init "(\$RUNNER_TEMP/[^"]+)"', job)
    assert rendered, job
    assert re.search(rf'uvx zizmor==\S+ .*"{re.escape(rendered.group(1))}', job), job


def test_every_action_is_pinned_to_a_commit():
    refs = _USES.findall(WORKFLOW.read_text(encoding="utf-8"))
    assert refs, "no `uses:` line found"
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), refs


def test_setup_uv_pins_the_uv_version():
    # Without a `version:` input, setup-uv installs whatever uv release is current at run
    # time, so every later `uv` step drifts without a commit. Each setup-uv step pins it.
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = text.count("astral-sh/setup-uv@")
    pins = len(re.findall(r"^\s+version: '\d[\w.]*'$", text, re.M))
    assert steps and pins == steps, f"{pins} version pins for {steps} setup-uv step(s)"


def test_no_job_widens_the_token():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read$", text, re.M), text
    assert "write" not in "".join(re.findall(r"permissions:[^\n]*(?:\n +[^\n]+)*", text))


def test_one_leg_runs_the_suite_with_every_command_in_a_real_process():
    # The default run calls most engine commands in-process (tests/engine_run.py). This
    # step keeps the end-to-end form of every such test running somewhere.
    job = jobs()["checks"]
    assert "RADAR_TEST_SUBPROCESS: '1'" in job, job


def test_windows_also_runs_the_suite_with_every_command_in_a_real_process():
    # Console encoding and argument quoting at a Windows process start are what the
    # in-process default and the Linux leg both miss.
    job = jobs().get("real-process-windows")
    assert job is not None, sorted(jobs())
    assert "runs-on: windows-latest" in job, job
    assert "RADAR_TEST_SUBPROCESS: '1'" in job, job
    assert "github.event_name == 'schedule'" in job, job


def test_the_build_backend_is_pinned_and_dependabot_moves_it():
    # A build from the git source - `uv build` in the stranger-path job, `uvx --from git+...`
    # in every catalogue's CI - fetches the build backend from the index. Pinned, it fetches
    # the version reviewed here; Dependabot's `uv` entry proposes the next one.
    import tomllib

    project = tomllib.loads((CHECKOUT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = project["build-system"]["requires"]
    assert requires and all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d[\w.]*", r) for r in requires), (
        requires
    )
    dependabot = (CHECKOUT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert 'package-ecosystem: "uv"' in dependabot, dependabot
