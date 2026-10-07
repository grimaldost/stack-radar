"""The version sites agree, by a test that fails rather than by convention.

A release rule that exists only in prose fails silently: nothing goes red when it is
broken, so it is found by whoever installs the wrong version. The remedy is equality of
version sites by a FAILING test - never by convention, and never by an assertion of mere
presence.

`[project].version` is the anchor, and every other site is compared against it: the newest
RELEASED heading of CHANGELOG.md, the package's own `__version__` (the site an INSTALLED
copy answers from - `radar --version` reads it), `uv.lock`'s entry for the project itself,
and every install line in README.md and docs/ that installs this project from a tagged git
source.

Every site after the first two is OPTIONAL, and that is what lets one implementation
serve two kinds of repository. `version_site_errors` is called against a data root as
well - a catalogue has a `pyproject.toml` and a `CHANGELOG.md` and usually no package and
no install lines - so a missing site is skipped rather than failed. The package is located
from `[project].name`, which is the key that already says what is built; a name that stops
matching the directory is caught here rather than at install time.

The tests that plant a disagreement run the `check-version-sites` verb as a subprocess, so
`RADAR_ENGINE_SRC` selects which tree answers. It exists for the red proof: point it at a
`src/` extracted from an earlier commit and the same tests run against the pre-change
files. It is the same variable the rest of the suite uses to choose an engine.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from engine_run import run_engine
from stack_radar.radar_lib import release_headings, version_site_errors

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("RADAR_ENGINE_SRC") or (REPO / "src"))
CHANGELOG = REPO / "CHANGELOG.md"
PYPROJECT = REPO / "pyproject.toml"

_DUNDER = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.M)


def headings() -> list[tuple[str, str]]:
    return release_headings(REPO)


def package_init() -> Path:
    name = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"]
    return SRC / name.replace("-", "_") / "__init__.py"


def test_the_changelog_exists_and_carries_a_released_heading():
    # Without this the equality test below would pass vacuously on an empty file: no
    # headings, nothing to compare, green - a release rule written down and never
    # exercised.
    assert CHANGELOG.is_file(), f"no CHANGELOG.md at {CHANGELOG}"
    assert headings(), (
        "CHANGELOG.md carries no `## [X.Y.Z] - YYYY-MM-DD` heading - the version site "
        "this test compares against does not exist"
    )


def test_the_package_version_equals_the_newest_released_heading():
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    newest, date = headings()[0]
    assert declared == newest, (
        f"pyproject.toml says {declared}, CHANGELOG.md's newest released heading says "
        f"{newest} (dated {date}). A release rolls both in the same metadata-only commit."
    )


def test_the_package_version_site_exists_and_is_read_as_text():
    # Non-vacuity for the test below, and it is not the same assertion twice: a package
    # whose `__init__` stopped declaring `__version__` would leave the comparison with
    # nothing to disagree with, and `version_site_errors` treats an absent package as a
    # legitimate repository without that site. Here - in the engine's own suite, where the site is
    # not optional - its absence has to be the failure it would be.
    init = package_init()
    assert init.is_file(), (
        f"no {init} - `[project].name` names a package that is not there, so its "
        "`__version__` site cannot be read and the check silently skips it"
    )
    assert _DUNDER.search(init.read_text(encoding="utf-8")), (
        f"{init} declares no `__version__` - a distributed package carries its version in "
        "its own namespace, and this is the site an installed copy answers from"
    )


def test_the_dunder_version_equals_the_pyproject_version():
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    found = _DUNDER.search(package_init().read_text(encoding="utf-8"))
    assert found and found.group(1) == declared, (
        f"pyproject.toml says {declared}, {package_init().name} says "
        f"{found.group(1) if found else '(nothing)'}. They are the same fact, and a "
        "release rolls every version site in one metadata-only commit."
    )


def test_the_release_headings_descend():
    # A heading that does not move the version forward is how a version comes to name two
    # different trees. Compared as tuples, so 0.10.0 sorts after 0.9.0.
    versions = [tuple(int(p) for p in v.split(".")) for v, _ in headings()]
    assert versions == sorted(versions, reverse=True), (
        f"the release headings are not in descending version order: {versions}"
    )


def _repo(root: Path, *, pyproject: str, changelog: str, dunder: str | None) -> Path:
    """A synthetic repository with a pyproject and a changelog, and a package `__version__`
    when `dunder` is given."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "mini-engine"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{changelog}] - 2026-09-13\n\nthe fixture's own release.\n",
        encoding="utf-8",
    )
    if dunder is not None:
        pkg = root / "src" / "mini_engine"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f'__version__ = "{dunder}"\n', encoding="utf-8")
    return root


class TestThePackageVersionSiteIsActuallyCompared:
    """The shared implementation reads the package `__version__`, proven by making it
    disagree.

    The tests above read the files themselves, so they would all stay green if
    `version_site_errors` quietly stopped reading one - which is the exact shape of the
    defect this whole module exists to prevent, one fact with two implementations. These
    plant the disagreement in a synthetic repository instead, where the only thing that
    can notice it is the implementation.
    """

    def test_a_package_version_that_disagrees_is_reported(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.1.0", changelog="0.1.0", dunder="0.2.0")
        errs = version_site_errors(root)
        assert errs, "the package's __version__ disagrees with the others and nothing said so"
        assert "0.2.0" in " ".join(errs), errs

    def test_the_sites_that_agree_are_silent(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.1.0", changelog="0.1.0", dunder="0.1.0")
        assert version_site_errors(root) == []

    def test_a_repository_with_no_package_skips_that_site(self, tmp_path: Path):
        # The optional arm, and it carries its weight: the same verb is run against a data
        # root, which has a pyproject and a changelog and no package at all. Treating the
        # absent site as a failure would make the check unusable there; treating it as
        # agreement would be the silence this module refuses everywhere else - so it is
        # reported as neither, and the sites that DO exist are still compared.
        root = _repo(tmp_path / "r", pyproject="0.1.0", changelog="0.1.0", dunder=None)
        assert version_site_errors(root) == []

    def test_a_missing_package_does_not_mask_the_other_sites(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.1.0", changelog="0.2.0", dunder=None)
        assert version_site_errors(root), (
            "a repository without a package stopped comparing its other sites"
        )


def test_the_gate_reads_the_same_rule():
    # The gate asks the same question during a scheduled review, and two implementations
    # of one fact drift silently. So both call radar_lib.version_site_errors, and this
    # pins that they still agree.
    #
    # It is also the only test here that exercises the package `__version__` site through
    # the shared implementation rather than re-reading the file itself: the tests above
    # are the oracle, and this is the code the gate and the `check-version-sites` verb run.
    assert version_site_errors(REPO) == []


def check_version_sites(root: Path) -> subprocess.CompletedProcess[str]:
    """`radar check-version-sites <root>`, run by the engine `RADAR_ENGINE_SRC` selects."""
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    env.pop("RADAR_DATA_ROOT", None)
    return run_engine("cli", ["check-version-sites", str(root)], cwd=Path.cwd(), env=env)


def _lock(root: Path, version: str, *, source: str = '{ editable = "." }') -> None:
    """A `uv.lock` whose own entry records `version`, next to an unrelated dependency."""
    (root / "uv.lock").write_text(
        "version = 1\n"
        'requires-python = ">=3.11"\n\n'
        "[[package]]\n"
        'name = "mini-engine"\n'
        f'version = "{version}"\n'
        f"source = {source}\n\n"
        "[[package]]\n"
        'name = "ruff"\n'
        'version = "9.9.9"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )


class TestTheLockIsAVersionSite:
    """`uv.lock` records the project's own version, and a stale one is reported."""

    def test_a_lock_behind_the_version_is_reported(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        _lock(root, "0.1.0")
        done = check_version_sites(root)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "uv.lock" in done.stdout and "0.1.0" in done.stdout, done.stdout

    def test_a_virtual_project_s_lock_is_compared_too(self, tmp_path: Path):
        # A data root builds nothing, so uv records it as `virtual` rather than `editable`.
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder=None)
        _lock(root, "0.1.0", source='{ virtual = "." }')
        done = check_version_sites(root)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "uv.lock" in done.stdout, done.stdout

    def test_a_lock_that_agrees_is_silent(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        _lock(root, "0.2.0")
        done = check_version_sites(root)
        assert done.returncode == 0, done.stdout + done.stderr

    def test_a_dependency_is_not_the_project(self, tmp_path: Path):
        # Only the entry whose source is the directory itself is the project; a package
        # of the same name pulled from an index is somebody else's release.
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        _lock(root, "0.1.0", source='{ registry = "https://pypi.org/simple" }')
        done = check_version_sites(root)
        assert done.returncode == 0, done.stdout + done.stderr


class TestInstallLinesAreVersionSites:
    """An install line for this project, in README.md or docs/, names the current release."""

    LINE = "uv tool install git+https://github.com/someone/mini-engine@v{}\n"

    def test_a_readme_line_at_the_previous_tag_is_reported(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        (root / "README.md").write_text("# mini\n\n" + self.LINE.format("0.1.0"), encoding="utf-8")
        done = check_version_sites(root)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "README.md:3" in done.stdout and "0.1.0" in done.stdout, done.stdout

    def test_a_line_in_a_nested_doc_is_reported(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        guide = root / "docs" / "guides" / "install.md"
        guide.parent.mkdir(parents=True)
        guide.write_text(self.LINE.format("0.1.9"), encoding="utf-8")
        done = check_version_sites(root)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "docs/guides/install.md:1" in done.stdout, done.stdout

    def test_lines_at_the_current_tag_are_silent(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        (root / "README.md").write_text(self.LINE.format("0.2.0"), encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "a.md").write_text(self.LINE.format("0.2.0"), encoding="utf-8")
        done = check_version_sites(root)
        assert done.returncode == 0, done.stdout + done.stderr

    def test_another_project_s_install_line_is_ignored(self, tmp_path: Path):
        # A README may tell its reader to install some other tool at some other version;
        # that line is the other project's site, not this one's.
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        (root / "README.md").write_text(
            "uv tool install git+https://github.com/someone/widget@v0.1.0\n", encoding="utf-8"
        )
        done = check_version_sites(root)
        assert done.returncode == 0, done.stdout + done.stderr

    def test_absent_documents_and_lock_are_skipped(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        done = check_version_sites(root)
        assert done.returncode == 0, done.stdout + done.stderr


class TestAnUndecodableFileIsNoCrash:
    """A document or lock that is not UTF-8 is read without a traceback.

    The install-line pattern is plain ASCII, so a document in another encoding still has
    its install lines found once the bytes that do not decode are replaced. `uv.lock` is
    UTF-8 by the TOML specification; one that is not is `uv lock --check`'s finding, and
    here it is a site that does not exist rather than a crash of the verb and the gate.
    """

    def test_a_latin_1_doc_is_still_searched(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        doc = root / "docs" / "x" / "latin1.md"
        doc.parent.mkdir(parents=True)
        doc.write_bytes(
            "Caf\u00e9\n".encode("latin-1")
            + TestInstallLinesAreVersionSites.LINE.format("0.1.0").encode("ascii")
        )
        done = check_version_sites(root)
        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode == 1, done.stdout + done.stderr
        assert "docs/x/latin1.md:2" in done.stdout, done.stdout

    def test_a_latin_1_readme_without_install_lines_is_silent(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        (root / "README.md").write_bytes("# Caf\u00e9\n".encode("latin-1"))
        done = check_version_sites(root)
        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stdout + done.stderr

    def test_a_lock_that_is_not_utf_8_is_no_crash(self, tmp_path: Path):
        root = _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder="0.2.0")
        (root / "uv.lock").write_bytes(b"version = 1\n# \xe9\n")
        done = check_version_sites(root)
        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stdout + done.stderr


class TestAnUnreadableAnchorIsAFinding:
    """pyproject.toml and CHANGELOG.md are the anchors. One that cannot be read is a
    [FAIL] line naming the file, never a traceback out of the verb."""

    def _repo(self, tmp_path: Path) -> Path:
        return _repo(tmp_path / "r", pyproject="0.2.0", changelog="0.2.0", dunder=None)

    def _judged(self, root: Path) -> str:
        done = check_version_sites(root)
        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode == 1, done.stdout + done.stderr
        return done.stdout

    def test_a_pyproject_that_is_not_utf_8(self, tmp_path: Path):
        root = self._repo(tmp_path)
        (root / "pyproject.toml").write_bytes(b'[project]\nname = "caf\xe9"\nversion = "0.2.0"\n')
        assert "[FAIL] version sites: pyproject.toml could not be read" in self._judged(root)

    def test_a_pyproject_that_is_not_toml(self, tmp_path: Path):
        root = self._repo(tmp_path)
        (root / "pyproject.toml").write_text("[project\n", encoding="utf-8")
        assert "pyproject.toml could not be read as TOML" in self._judged(root)

    def test_a_pyproject_with_no_project_table(self, tmp_path: Path):
        root = self._repo(tmp_path)
        (root / "pyproject.toml").write_text("[tool.x]\ny = 1\n", encoding="utf-8")
        assert "no `[project].version` string" in self._judged(root)

    def test_a_missing_pyproject(self, tmp_path: Path):
        root = self._repo(tmp_path)
        (root / "pyproject.toml").unlink()
        assert "no pyproject.toml at" in self._judged(root)

    def test_a_changelog_that_is_not_utf_8(self, tmp_path: Path):
        root = self._repo(tmp_path)
        (root / "CHANGELOG.md").write_bytes(b"# Changelog\n\n## [0.2.0] - 2026-09-13\n\xe9\n")
        assert "CHANGELOG.md could not be read" in self._judged(root)
