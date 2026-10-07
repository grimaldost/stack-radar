"""Rules that several commands apply, held in one place so they cannot disagree.

- a name that becomes one path component (a tool name, an environment name) is
  `radar_lib.NAME_PATTERN`, applied with fullmatch;
- every `--env` is parsed by `radar_lib.environment_name`, so a value such as `../../x`
  is refused with exit 2 before any verb builds a path from it;
- a `github:` registry target is read by `radar_lib.github_slug`, the same function
  `radar versions` uses before it builds a URL;
- a github.com URL in an entry's `repo` is read by `radar_lib.github_repo`, which
  `radar snapshot` looks the repository up by and the gate reads to tell what a snapshot
  can measure.

Each rule is checked by behaviour, and the single source by a scan of the package: a
module that grew its own copy is what these tests exist to catch.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from engine_run import ENGINE_SRC, run_engine

import stack_radar
from stack_radar import radar_lib, versions

PACKAGE = Path(stack_radar.__file__).resolve().parent

HOSTILE_NAMES = ["", ".", "..", ".hidden", "a/b", "a\\b", "../../x", "ok\n", "a b", "<x>"]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_name_outside_the_pattern_is_not_plain(name: str) -> None:
    assert not radar_lib.is_plain_name(name)


@pytest.mark.parametrize("name", ["ruff", "a.b", "a_b", "a-b", "0x"])
def test_a_plain_name_is_plain(name: str) -> None:
    assert radar_lib.is_plain_name(name)


def test_validate_refuses_a_name_that_ends_in_a_newline() -> None:
    errs = radar_lib.validate({"name": "ok\n"})
    assert any(e.startswith("bad name") for e in errs), errs


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_the_name_pattern_is_written_once() -> None:
    # A second copy of the pattern is how the schema and a command come to disagree.
    pattern = radar_lib.NAME_PATTERN.pattern
    holders = sorted(
        path.name
        for path in PACKAGE.glob("*.py")
        if pattern in _string_constants(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert holders == ["radar_lib.py"], holders


def test_the_name_rule_is_described_once() -> None:
    # Every message that states the rule reads `radar_lib.NAME_RULE`, so the words a
    # refusal prints cannot drift from each other or from the pattern. Docstrings count:
    # a module that restated the rule in prose would be a second description to keep true.
    phrase = "starting with a letter or digit"
    holders = sorted(
        (path.name, text.count(phrase))
        for path in PACKAGE.glob("*.py")
        if phrase in (text := path.read_text(encoding="utf-8"))
    )
    assert holders == [("radar_lib.py", 1)], holders


@pytest.mark.parametrize(
    ("module", "args", "expected"),
    [
        ("field_report", ("a b", "--env", "default"), "not a valid entry name"),
        ("bootstrap", ("--help",), "environment name"),
    ],
)
def test_the_messages_that_state_the_rule_state_it_alike(
    module: str, args: tuple[str, ...], expected: str, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "catalogue"
    (root / "environments").mkdir(parents=True)
    (root / "radar.toml").write_text('schema = "radar-data/v1"\n', encoding="utf-8")
    (root / "environments" / "default.toml").write_text(
        f'name = "default"\n[paths]\nclaude_home = "{(home / ".claude").as_posix()}"\n',
        encoding="utf-8",
    )
    (home / ".claude" / "projects").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tools" / "a-b.toml").write_text('name = "a b"\n', encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("RADAR_DATA_ROOT", None)
    done = run_engine(module, list(args), cwd=root, env=env)
    text = " ".join((done.stdout + done.stderr).split())
    assert expected in text, text
    assert radar_lib.NAME_RULE in text, text


# ------------------------------------------------------------ reserved device names
#
# Windows keeps CON, PRN, AUX, NUL, COM0-9 and LPT0-9 for devices in every directory, with
# or without an extension and in any case: `tools/nul.toml` cannot be created or checked
# out there, so the name rule refuses them everywhere it applies.


@pytest.mark.parametrize("name", ["CON", "nul", "Aux", "com1", "LPT9", "aux.txt", "con.tool"])
def test_a_reserved_device_name_is_refused_by_radar_add(name: str, tmp_path: Path) -> None:
    root = tmp_path / "catalogue"
    (root / "tools").mkdir(parents=True)
    (root / "radar.toml").write_text('schema = "radar-data/v1"\n', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ENGINE_SRC)}
    env.pop("RADAR_DATA_ROOT", None)
    args = [
        "add",
        name,
        "--repo",
        "https://example.invalid/x",
        "--axis",
        "lint-format",
        "--ring",
        "observe",
        "--license",
        "MIT",
        "--visibility",
        "public",
        "--artifact",
        "cli",
        "--note",
        "a synthetic entry",
        "--reason",
        "a synthetic fixture",
    ]
    done = run_engine("cli", args, cwd=root, env=env)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "bad name" in done.stdout, done.stdout
    assert list((root / "tools").iterdir()) == []


@pytest.mark.parametrize("name", ["con-tool", "nullable", "com10", "lpt", "auxiliary.x"])
def test_a_name_that_only_starts_like_a_device_is_plain(name: str) -> None:
    assert radar_lib.is_plain_name(name)


def test_a_reserved_env_is_refused_with_exit_2(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ENGINE_SRC)}
    env.pop("RADAR_DATA_ROOT", None)
    done = run_engine("cli", ["apply", "--env", "nul"], cwd=tmp_path, env=env)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "is not an environment name" in done.stderr, done.stderr


# ------------------------------------------------------------------------- --env

ENV_VERBS = (
    "apply",
    "bootstrap",
    "field",
    "reconcile",
    "render-feedback-targets",
    "sync-feedback",
    "versions",
)


def test_every_env_flag_is_parsed_by_the_shared_validator() -> None:
    # Found by scanning, so a verb that adds `--env` later is covered the day it lands.
    unchecked = []
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "--env"
            ):
                continue
            kinds = {k.arg: k.value for k in node.keywords}
            given = kinds.get("type")
            if not (isinstance(given, ast.Name) and given.id == "environment_name"):
                unchecked.append(f"{path.name}:{node.lineno}")
    assert unchecked == [], unchecked


@pytest.mark.parametrize("verb", ENV_VERBS)
def test_a_traversing_env_is_refused_with_exit_2(verb: str, tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ENGINE_SRC)}
    env.pop("RADAR_DATA_ROOT", None)
    done = run_engine("cli", [verb, "--env", "../../x"], cwd=tmp_path, env=env)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "is not an environment name" in done.stderr, done.stderr
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------- github:

SLUGS = [
    ("example/widget", "example/widget"),
    ("example/widget.git", "example/widget"),
    ("ex-ample/wid.get_x", "ex-ample/wid.get_x"),
    ("example/.", None),
    ("example/..", None),
    ("example/.git", None),
    ("-example/widget", None),
    ("example/widget\n", None),
    ("example", None),
    ("example/widget/extra", None),
]


@pytest.mark.parametrize(("target", "slug"), SLUGS)
def test_the_gate_and_versions_read_a_github_target_alike(target: str, slug: str | None) -> None:
    refused = bool(radar_lib.registry_errors(f"github:{target}"))
    assert refused == (slug is None), target
    assert radar_lib.github_slug(target) == slug
    if slug is None:
        # `radar versions` refuses it before any URL is built.
        assert versions.latest_github(target)[0] is None
        assert "malformed" in versions.latest_github(target)[1]


REPO_URLS = [
    ("https://github.com/example/widget", "example/widget"),
    ("https://github.com/example/widget/", "example/widget"),
    ("https://github.com/example/widget.git", "example/widget"),
    ("https://github.com/example/widget/tree/main/docs", "example/widget"),
    ("https://github.com/example/widget#readme", "example/widget"),
    ("https://github.com/example/widget?tab=readme", "example/widget"),
    ("https://github.com/example/widget.git#main", "example/widget"),
    ("http://GitHub.com/example/widget", "example/widget"),
    ("git@github.com:example/widget.git", "example/widget"),
    ("ssh://git@github.com/example/widget", "example/widget"),
    ("https://notgithub.com/example/widget", None),
    ("https://github.com.example.invalid/example/widget", None),
    ("https://example.invalid/github.com/example/widget", None),
    ("https://gist.github.com/example/widget", None),
    ("https://github.com/example", None),
    ("{documents}/widget", None),
    ("(repo does not exist)", None),
    ("", None),
]


@pytest.mark.parametrize(("repo", "slug"), REPO_URLS)
def test_a_github_repo_url_names_its_repository_and_nothing_else(repo: str, slug: str | None):
    assert radar_lib.github_repo(repo) == slug
