"""Compare installed versions against upstream, under a declared policy. Upgrades nothing.

WHY THIS EXISTS
---------------
Presence and version are different questions: a presence probe cannot tell an installed
tool from a current one. Two failure modes follow from asking only the first:

- A tool sits a patch release behind upstream, and nothing notices.
- A plugin runs one version in one surface and another version in a second surface, and
  the difference is observable: a hook, or a setting that gates it, can exist in one
  version and be gone from the other, so the same `settings.json` is live config in one
  kind of session and dead config in the other.

Neither is caught by presence probes, and neither is caught by staleness of the upstream
repository (which the gate checks) - an actively developed tool with a fresh last-push
date is exactly the kind you fall behind.

WHERE THE INSTALLED VERSION COMES FROM
--------------------------------------
From the installer's own records wherever the engine can read them, and from the entry's
`[install].check` probe only where it cannot:

    uv-tool        uv's tool directory: the receipt names the package and the tool
                   environment's dist-info carries its version (`uv tool list` when the
                   directory is not where uv keeps it by default)
    claude-plugin  <claude_home>/plugins/installed_plugins.json, which Claude Code
    mcp-plugin     writes when it installs a plugin
    repo           the tag checked out in the entry's local working tree

The records come first because reading them needs no command from the catalogue. A check
probe is catalogue text, and runs only once the operator has approved that exact command
(trust.py); the commands this module runs on its own account - `uv tool list`,
`git describe`, `git ls-remote` - have a fixed argv the engine wrote.

THE IDENTITY RULE, which matters more here than anywhere
--------------------------------------------------------
A version comparison needs a registry, and resolving a registry by NAME is the trap the
gate already forbids: a short, ordinary tool name queried on PyPI can return an unrelated
package at a LOWER version, which next to the local one reads as "you are ahead of
upstream" - a comparison between two different projects. So a check runs ONLY for an
entry that declares `registry`, and a private tool that declares none is reported as
unversioned rather than guessed at.

    pypi:<name>            the newest release on PyPI
    npm:<name>             the `latest` tag on npm
    github:<owner>/<repo>  the highest X.Y.Z or vX.Y.Z tag of the repository, read with
                           `git ls-remote --tags`; pre-release tags are ignored. GitHub
                           publishes no download count, so nothing else is read

POLICIES
--------
    latest  warn when behind the registry's newest release (the default for a tool you
            install from a registry: you want to know, and you decide)
    pin     fail on any deviation from `pinned` - for a tool whose behaviour you have
            measured at one version and must not have change under you
    floor   fail below `min`, silent above it - when a fix or feature is required but
            newer is fine
    any     never compared; for a tool where the version is not a meaningful axis

Reports only. Upgrading is a decision with a blast radius, and this command does not make
decisions - same discipline as `radar apply` (never executes a removal) and
`radar reconcile` (never edits a ring).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from .apply import expand
from .bootstrap import home_dir
from .radar_lib import (
    begin_command,
    default_environment,
    environment_name,
    github_slug,
    load_environment,
    load_tools,
    registry_errors,
    reports_profile_errors,
    tools_for_environment,
)
from .trust import (
    CHECK,
    FLAG,
    GIT_SAFE_CONFIG,
    Trust,
    batch_refusal,
    catalogue_argv,
    command_env,
    executable,
    git_common_dir,
    scratch_dir,
    split_command,
)

SEMVERISH = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-.][0-9A-Za-z.]+)?)")
POLICIES = ("latest", "pin", "floor", "any")
TIMEOUT = 25

# A release tag: X.Y.Z, optionally with a leading `v`, and nothing after it - so
# `1.4.0-rc1`, `1.4.0rc1` and `nightly` are not releases.
RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
PLUGIN_KINDS = ("claude-plugin", "mcp-plugin")
REMOTE_PREFIXES = ("http://", "https://", "git@")
# One line of `uv tool list`: `<package> v<version>`, before the `- <entrypoint>` lines.
UV_LIST_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*) v(\S+)")
NOT_APPROVED = "check not approved"
# The row of a policy=any entry whose probe is not approved: no approval is asked for, so
# the state says why the version is unknown rather than that an approval is pending.
NOT_NEEDED = "probe not approved, not needed at policy any"


def parse_version(text: str) -> str | None:
    """First version-looking token in a `--version` output.

    Tools print wildly different shapes ('ruff 0.9.1', 'Python 3.12.4',
    'widget 1.2.3 (abc1234 build)', 'git version 2.45.0.windows.1'), so the first
    semver-ish run wins rather than trying to model each format.
    """
    m = SEMVERISH.search(text or "")
    return m.group(1) if m else None


def key(v: str) -> tuple:
    """Comparable tuple. Non-numeric suffixes sort BEFORE their release (0.1.0rc1 < 0.1.0)."""
    head = re.split(r"[-+]", v or "", maxsplit=1)[0]
    parts: list[tuple[int, int | str]] = []
    for chunk in head.split("."):
        m = re.match(r"^(\d+)(.*)$", chunk)
        if m:
            parts.append((0, int(m.group(1))))
            if m.group(2):
                parts.append((-1, m.group(2)))
        else:
            parts.append((-1, chunk))
    return tuple(parts)


def highest_release(tags: list[str]) -> str | None:
    """The highest release tag among `tags`, as X.Y.Z without the `v`."""
    found = [tuple(int(g) for g in m.groups()) for m in map(RELEASE_TAG.match, tags) if m]
    return ".".join(map(str, max(found))) if found else None


def pep503(name: str) -> str:
    """A package name in its normalized spelling: lower case, runs of `-_.` as one `-`."""
    return re.sub(r"[-_.]+", "-", str(name or "").strip().lower())


def _lower(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def identities(t: dict) -> set[str]:
    """The names this entry could legitimately appear under in the data plane: its own
    name, the package name its registry declares, and its aliases.

    A `github:` registry names a repository rather than a package, so it adds nothing:
    `owner/repo` is not a name anything is installed or depended on under.
    """
    out = {_lower(t["name"])}
    kind, _, package = str(t.get("registry") or "").partition(":")
    if package and kind != "github":
        out.add(_lower(package))
    for alias in t.get("aliases") or []:
        out.add(_lower(alias))
    return {o for o in out if o}


# --------------------------------------------------------------------- running


def global_probe_env() -> dict[str, str]:
    """The environment a GLOBAL-tool probe should search with - this process's own,
    minus the running project's virtualenv.

    Run through `uv run`, this command inherits the project's `.venv/Scripts` (Windows) or
    `.venv/bin` (POSIX) ahead of PATH and a `VIRTUAL_ENV` naming it, so the project finds
    its own pinned tools. A version PROBE wants the opposite: it asks "what does the
    operator have installed globally", and a lookup against this process's PATH answers a
    different question - a tool the project pins as a dev dependency resolves to the
    project's copy ahead of the global one, and the global install reads as behind (or
    ahead) when it was never read at all.

    `VIRTUAL_ENV` is dropped and its own bin/Scripts directory is stripped out of PATH,
    by directory identity (`os.path.normcase` + `normpath`, not a string prefix, so a
    trailing separator or a differently-cased drive letter still matches on Windows).
    Everything else on PATH - the operator's real global installs - passes through
    unchanged. A probe run with no virtualenv active is unaffected either way.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if not venv:
        return env
    venv_bin = os.path.normcase(
        os.path.normpath(str(Path(venv) / ("Scripts" if os.name == "nt" else "bin")))
    )
    parts = [
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p and os.path.normcase(os.path.normpath(p)) != venv_bin
    ]
    env["PATH"] = os.pathsep.join(parts)
    return env


def _exec(
    argv: list[str], base: Path | None = None, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run `argv` without a shell, against the global environment, in an empty scratch
    directory. None when it cannot run at all.

    argv[0] is looked up on PATH only, never in a working directory; a relative argv[0]
    with a directory part is taken from `base`. A `.bat` or `.cmd` program, which Windows
    runs through cmd.exe, does not run when `trust.batch_refusal` refuses its path or an
    argument. The approval judges a catalogue probe on this same PATH (`Trust(path=...)`)
    and names such a refusal; this check only keeps the runner from ever passing one on.
    The scratch directory and PYTHONSAFEPATH (trust.py) keep whatever directory the engine
    was started from - usually the catalogue - out of what the program reads.
    """
    env = command_env({**global_probe_env(), **(extra or {})})
    exe = executable(argv[0], base, env.get("PATH"))
    if not exe or batch_refusal(exe, argv[1:]):
        return None
    try:
        with scratch_dir() as cwd:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                [exe, *argv[1:]],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=cwd,
            )
    except (OSError, subprocess.SubprocessError):
        return None


def run(cmd: str, root: Path | None = None) -> str | None:
    """Run a probe without a shell, against the GLOBAL environment (`global_probe_env`),
    never the running project's own virtualenv. None when it cannot run at all.

    Split by `trust.catalogue_argv`, the splitter `radar apply` and the approval both
    use, so a quoted argument holding a space stays one argument, one command text means
    one argv everywhere, and a path relative to the data root (`root`) is passed as the
    absolute path the approval hashed. The caller decides whether the command may run.
    """
    try:
        argv = catalogue_argv(cmd, root) if root else split_command(cmd)
    except ValueError:
        return None
    if not argv:
        return None
    p = _exec(argv, root)
    return None if p is None else (p.stdout or "") + (p.stderr or "")


# -------------------------------------------------------------------- upstream


def latest_pypi(pkg: str) -> tuple[str | None, str]:
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310 - fixed host
            return json.load(r)["info"]["version"], "ok"
    except urllib.error.HTTPError as e:
        return None, f"http {e.code} (not on PyPI under this name?)"
    except (OSError, ValueError, KeyError) as e:
        return None, f"{type(e).__name__}"


def latest_npm(pkg: str) -> tuple[str | None, str]:
    url = f"https://registry.npmjs.org/{pkg}/latest"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310 - fixed host
            return json.load(r)["version"], "ok"
    except urllib.error.HTTPError as e:
        return None, f"http {e.code} (not on npm under this name?)"
    except (OSError, ValueError, KeyError) as e:
        return None, f"{type(e).__name__}"


def ls_remote_tags(output: str) -> list[str]:
    """Tag names from `git ls-remote --tags` output. A peeled line (`<tag>^{}`) names the
    same tag as its unpeeled one."""
    tags: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/tags/"):
            tags.add(parts[1][len("refs/tags/") :].removesuffix("^{}"))
    return sorted(tags)


def latest_github(slug: str) -> tuple[str | None, str]:
    """The highest release tag of github.com/<slug>, from `git ls-remote --tags`.

    A fixed argv the engine wrote, so it needs no approval. `credential.helper` is emptied
    and terminal prompts are off: a repository that is missing or private answers with a
    login prompt, and a version report must not stop to ask for a password.
    """
    # Checked before it is put into a URL, by the rule validate() applies to the entry.
    owner_repo = github_slug(slug)
    if owner_repo is None:
        return None, "malformed github registry (want 'github:<owner>/<repo>')"
    p = _exec(
        [
            "git",
            *GIT_SAFE_CONFIG,
            "-c",
            "credential.helper=",
            "ls-remote",
            "--tags",
            f"https://github.com/{owner_repo}",
        ],
        extra={"GIT_TERMINAL_PROMPT": "0"},
    )
    if p is None:
        return None, "git not found"
    if p.returncode != 0:
        return None, f"git ls-remote exited {p.returncode} (repository missing or private?)"
    best = highest_release(ls_remote_tags(p.stdout))
    return (best, "ok") if best else (None, "no X.Y.Z release tag")


def upstream(registry: str) -> tuple[str | None, str]:
    if ":" not in registry:
        return None, (
            "malformed registry (want 'pypi:<name>', 'npm:<name>' or 'github:<owner>/<repo>')"
        )
    kind, name = registry.split(":", 1)
    # Checked before the name is put into a URL, by the rule the gate applies to the entry
    # (`latest_github` checks its own): this command does not run the gate, so an entry
    # the gate would refuse gets here.
    if kind in ("pypi", "npm") and registry_errors(registry):
        return None, f"malformed {kind} registry (want '{kind}:<name>')"
    if kind == "pypi":
        return latest_pypi(name)
    if kind == "npm":
        return latest_npm(name)
    if kind == "github":
        return latest_github(name)
    return None, f"unsupported registry kind {kind!r}"


# ------------------------------------------------------------ installed records


def uv_tool_dir() -> Path | None:
    """Where uv keeps its tool environments: `$UV_TOOL_DIR`, else uv's platform default."""
    given = os.environ.get("UV_TOOL_DIR")
    if given:
        return Path(given)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "uv" / "tools" if base else None
    base = os.environ.get("XDG_DATA_HOME") or str(home_dir() / ".local" / "share")
    return Path(base) / "uv" / "tools"


def _dist_version(tool_env: Path, package: str) -> str | None:
    """The version of `package` installed in a tool environment, from its dist-info name."""
    want = pep503(package)
    sites = [*tool_env.glob("Lib/site-packages"), *tool_env.glob("lib/python*/site-packages")]
    for site in sites:
        for info in site.glob("*.dist-info"):
            m = re.match(r"^(.+)-([^-]+)\.dist-info$", info.name)
            if m and pep503(m.group(1)) == want:
                return m.group(2)
    return None


def read_uv_tool_dir(directory: Path) -> dict[str, str | None]:
    """package -> version for every tool environment under `directory` that holds a
    receipt. The receipt's first requirement names the package the tool was installed
    from; the version is that package's dist-info in the environment."""
    out: dict[str, str | None] = {}
    for sub in sorted(p for p in directory.iterdir() if p.is_dir()):
        receipt = sub / "uv-receipt.toml"
        if not receipt.is_file():
            continue
        package = sub.name
        try:
            reqs = tomllib.loads(receipt.read_text(encoding="utf-8")).get("tool", {})
            first = (reqs.get("requirements") or [None])[0]
            if isinstance(first, dict) and first.get("name"):
                package = str(first["name"])
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            pass
        out[pep503(package)] = _dist_version(sub, package)
    return out


def parse_uv_tool_list(text: str) -> dict[str, str | None]:
    return {
        pep503(m.group(1)): m.group(2)
        for m in (UV_LIST_LINE.match(line) for line in text.splitlines())
        if m
    }


def uv_tools() -> tuple[dict[str, str | None] | None, str]:
    """(package -> installed version for every uv tool, where that was read). None when
    neither uv's tool directory nor `uv tool list` could be read."""
    directory = uv_tool_dir()
    if directory is not None and directory.is_dir():
        return read_uv_tool_dir(directory), directory.as_posix()
    p = _exec(["uv", "tool", "list"])
    if p is None or p.returncode != 0:
        return None, "no uv tool directory, and `uv tool list` did not run"
    return parse_uv_tool_list(p.stdout), "uv tool list"


def installed_plugins(claude_home: str | None) -> tuple[dict[str, list[str]] | None, str]:
    """(`plugin@marketplace` -> versions installed, detail) from Claude Code's own record
    at <claude_home>/plugins/installed_plugins.json. None when it cannot be read.

    Read from the declared claude_home, not from the account running the command, so a
    profile for another machine reads that machine's record.
    """
    if not claude_home:
        return None, "no [paths].claude_home declared"
    record = Path(claude_home) / "plugins" / "installed_plugins.json"
    if not record.is_file():
        return None, f"no {record.as_posix()}"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"{record.as_posix()} unreadable"
    plugins = data.get("plugins") if isinstance(data, dict) else None
    out: dict[str, list[str]] = {}
    for full, installs in (plugins if isinstance(plugins, dict) else {}).items():
        # One install per plugin in the older layout, a list of them (one per scope) now.
        if isinstance(installs, dict):
            installs = [installs]
        if not isinstance(installs, list):
            continue
        out[str(full)] = [str(i.get("version") or "?") for i in installs if isinstance(i, dict)]
    return out, "ok"


def repo_tags(repo: Path) -> list[str]:
    """Tag names of the repository at `repo`, read from its git directory - loose refs
    and packed-refs - with no git process."""
    common = git_common_dir(repo)
    if common is None:
        return []
    names: set[str] = set()
    loose = common / "refs" / "tags"
    if loose.is_dir():
        names |= {p.relative_to(loose).as_posix() for p in loose.rglob("*") if p.is_file()}
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/tags/"):
                names.add(parts[1][len("refs/tags/") :])
    return sorted(names)


def repo_version(repo: Path) -> str | None:
    """The version checked out in a local working tree, from its tags.

    `git describe --tags --abbrev=0` names the nearest tag behind HEAD - the version of
    what is checked out, not merely the newest tag the repository holds. It runs with
    `GIT_SAFE_CONFIG` because the repository is one the catalogue names, and a
    repository's own config can name a program git then runs on the operator's behalf:
    the file-system monitor is the one a read-only command can reach, and emptying it
    (with the rest of the set) means reading a tag runs nothing that config asks for.
    The repository must hold a `.git`: a directory laid out as a bare repository has none,
    and its config is exactly what reading it would otherwise run. Where git itself is not
    available, the tags are read from the git directory and the highest release wins.
    """
    if not (repo / ".git").exists():
        return None
    p = _exec(["git", *GIT_SAFE_CONFIG, "-C", str(repo), "describe", "--tags", "--abbrev=0"])
    if p is None:
        return highest_release(repo_tags(repo))
    return parse_version(p.stdout.strip()) if p.returncode == 0 else None


def local_worktree(t: dict, paths: dict[str, str]) -> Path | None:
    """The entry's local working tree - `[feedback].worktree`, else a `repo` that is a
    path rather than a URL - when it exists here."""
    raw = str((t.get("feedback") or {}).get("worktree") or t.get("repo") or "")
    if not raw or raw.startswith(REMOTE_PREFIXES):
        return None
    try:
        p = Path(expand(raw, paths).replace("\\", "/"))
    except KeyError:
        return None
    return p if p.is_absolute() and p.is_dir() else None


class Installed:
    """The installers' own records, each read at most once per run."""

    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths
        self._uv: tuple[dict[str, str | None] | None, str] | None = None
        self._plugins: tuple[dict[str, list[str]] | None, str] | None = None

    def uv(self) -> tuple[dict[str, str | None] | None, str]:
        if self._uv is None:
            self._uv = uv_tools()
        return self._uv

    def plugins(self) -> tuple[dict[str, list[str]] | None, str]:
        if self._plugins is None:
            self._plugins = installed_plugins(self.paths.get("claude_home"))
        return self._plugins

    def version_of(self, t: dict) -> tuple[str | None, str]:
        """(installed version, the record it came from), or (None, "") when no record the
        engine can read answers for this entry."""
        kind = (t.get("install") or {}).get("kind")
        ids = {pep503(i) for i in identities(t)}
        if kind == "uv-tool":
            tools, _ = self.uv()
            for i in sorted(ids):
                if tools and tools.get(i):
                    return tools[i], "uv"
        if kind in PLUGIN_KINDS or t.get("artifact") == "cc-plugin":
            plugins, _ = self.plugins()
            found = [
                v
                for full, versions in (plugins or {}).items()
                if pep503(full.split("@", 1)[0]) in ids
                for v in map(parse_version, versions)
                if v
            ]
            if found:
                return max(found, key=key), "installed_plugins.json"
        if kind == "repo":
            wt = local_worktree(t, self.paths)
            v = repo_version(wt) if wt else None
            if v:
                return v, "git tag"
        return None, ""


def plugin_skew(paths: dict[str, str]) -> list[str]:
    """Plugin versions per surface, because 'installed' is not one place.

    Claude Code's CLI resolves plugins from its own install record; a desktop-app session
    can serve its own pinned copy from a session directory. When those diverge the SAME
    settings.json means different things in the two surfaces - a hook present in one and
    removed in the other - which is invisible to every other check here.

    Both are read from the profile's declared `[paths].claude_home` rather than from the
    running account's home, so a comparison against a non-default environment reads that
    environment's machine and not whichever one happens to run the command.

    The app side is read from the session directory the Claude desktop app uses on Windows,
    `AppData/Roaming/Claude/local-agent-mode-sessions`, resolved against the parent folder
    of `claude_home` (the account home when `claude_home` is the default `~/.claude`). That
    layout belongs to the app and is not a documented interface, so the check is
    best-effort: where the directory does not exist - another platform, no desktop app, or
    a `claude_home` outside the account home - the served side is empty and the report says
    nothing about the app. `--no-plugins` skips the check.
    """
    out: list[str] = []
    claude_home = paths.get("claude_home")
    if not claude_home:
        return ["[NOTE] plugin skew: no [paths].claude_home declared"]
    plugins, detail = installed_plugins(claude_home)
    if plugins is None and "unreadable" in detail:
        out.append("[WARN] installed_plugins.json unreadable")
    cached: dict[str, str] = {}
    for full, versions in (plugins or {}).items():
        for v in versions:
            cached[full.split("@", 1)[0]] = v

    home = Path(claude_home).parent
    sessions = home / "AppData" / "Roaming" / "Claude" / "local-agent-mode-sessions"
    served: dict[str, set[str]] = {}
    if sessions.is_dir():
        for pj in sessions.glob("*/*/rpm/plugin_*/.claude-plugin/plugin.json"):
            try:
                d = json.loads(pj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            n, v = d.get("name"), str(d.get("version") or "?")
            if n:
                served.setdefault(str(n), set()).add(v)

    for name in sorted(set(cached) & set(served)):
        others = {v for v in served[name] if v != cached[name]}
        if others:
            out.append(
                f"[WARN] plugin {name}: CLI cache has {cached[name]}, an app session "
                f"serves {', '.join(sorted(others))} - the same settings.json can be live "
                "in one surface and dead config in the other"
            )
    for name in sorted(set(served) - set(cached)):
        out.append(
            f"[NOTE] plugin {name}: served to an app session "
            f"({', '.join(sorted(served[name]))}) but absent from the CLI cache"
        )
    return out


# ------------------------------------------------------------------------ main


@reports_profile_errors
def main() -> int:
    ap = argparse.ArgumentParser(prog="radar versions", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--env",
        type=environment_name,
        help="environment name (default: the top-level default_environment key of radar.toml)",
    )
    ap.add_argument("--offline", action="store_true", help="skip registry and tag lookups")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on any policy violation (pin/floor)",
    )
    ap.add_argument("--no-plugins", action="store_true", help="skip the plugin-skew check")
    ap.add_argument(
        FLAG,
        dest="trust_commands",
        action="store_true",
        help="approve, record and run the pending check probes an earlier "
        "`radar versions` run listed with the same id; one no such run listed, or changed "
        "since, is listed and not run",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)
    args.env = args.env or default_environment(root)
    # The probe runs on the global PATH (`_exec`), so that is where the approval looks the
    # program up too.
    trust = Trust(
        root,
        approve=args.trust_commands,
        verb="versions",
        path=global_probe_env().get("PATH", ""),
    )

    env = load_environment(args.env)
    if env is None:
        print(f"[FAIL] no environment named {args.env!r}", file=sys.stderr)
        return 2

    paths = {k: str(v) for k, v in (env.get("paths") or {}).items()}
    installed = Installed(paths)
    tools = tools_for_environment(env, load_tools())
    rows, findings, fails = [], [], 0

    for t in sorted(tools, key=lambda x: x["name"]):
        name = t["name"]
        ver = t.get("version") or {}
        policy = ver.get("policy") or ("latest" if t.get("registry") else "any")
        if policy not in POLICIES:
            findings.append(f"[FAIL] {name}: bad [version].policy {policy!r}")
            fails += 1
            continue
        if policy == "pin" and not ver.get("pinned"):
            findings.append(f"[FAIL] {name}: policy=pin needs `pinned`")
            fails += 1
            continue
        if policy == "floor" and not ver.get("min"):
            findings.append(f"[FAIL] {name}: policy=floor needs `min`")
            fails += 1
            continue

        # The installers' records first; the catalogue's own probe only where no record
        # answers, and only once the operator has approved it.
        have, _source = installed.version_of(t)
        check = (t.get("install") or {}).get("check")
        if have is None and check:
            try:
                cmd = expand(str(check), paths)
            except KeyError as exc:
                findings.append(
                    f"[WARN] {name}: check references unknown [paths] key: {exc.args[0]}"
                )
                cmd = None
            if cmd is not None:
                probe = trust.command(name, CHECK, cmd)
                # Under policy=any the version is shown and never compared, so a probe
                # that is not approved yet is not asked for: refusing it would fail the
                # run over a figure nothing reads. --trust-commands still approves it.
                quiet = policy == "any" and not trust.approve
                if not (trust.approved(probe) if quiet else trust.allows(probe)):
                    # Unknown, never ok: nothing was looked at.
                    rows.append((name, "?", "-", policy, NOT_NEEDED if quiet else NOT_APPROVED))
                    continue
                have = parse_version(run(cmd, trust.root) or "")

        if policy == "any":
            rows.append((name, have or "-", "-", "any", ""))
            continue

        if policy == "pin":
            want = ver["pinned"]
            verdict = "ok" if have == want else ("UNKNOWN" if not have else "PINNED->DRIFT")
            if verdict == "PINNED->DRIFT":
                findings.append(
                    f"[FAIL] {name}: pinned at {want}, installed {have} - a pin exists "
                    f"because behaviour was measured at that version"
                )
                fails += 1
            rows.append((name, have or "?", want, "pin", verdict))
            continue

        if policy == "floor":
            floor = ver["min"]
            if have and key(have) < key(floor):
                findings.append(f"[FAIL] {name}: below required floor {floor} (have {have})")
                fails += 1
                verdict = "BELOW FLOOR"
            else:
                verdict = "ok" if have else "UNKNOWN"
            rows.append((name, have or "?", f">={floor}", "floor", verdict))
            continue

        # policy == latest
        reg = t.get("registry")
        if not reg:
            # Never resolve a registry by name. Querying PyPI for a private tool's name
            # returns somebody else's project, and comparing the two is worse than silence.
            rows.append((name, have or "-", "-", "latest", "no registry declared"))
            continue
        if args.offline:
            rows.append((name, have or "-", "-", "latest", "offline"))
            continue
        up, note = upstream(str(reg))
        if not up:
            rows.append((name, have or "-", "-", "latest", note))
            continue
        if not have:
            rows.append((name, "?", up, "latest", "installed version unknown"))
            continue
        if key(have) < key(up):
            rows.append((name, have, up, "latest", "BEHIND"))
            findings.append(f"[WARN] {name}: {have} installed, {up} available ({reg})")
        elif key(have) > key(up):
            rows.append((name, have, up, "latest", "ahead of registry"))
        else:
            rows.append((name, have, up, "latest", "ok"))

    print(f"stack-radar versions - environment {args.env!r}")
    print()
    w = max((len(r[0]) for r in rows), default=4)
    print(f"{'tool'.ljust(w)}  installed    expected     policy  state")
    print(f"{'-' * w}  -----------  -----------  ------  -----")
    for n, have, want, pol, state in rows:
        print(f"{n.ljust(w)}  {have:11}  {want:11}  {pol:6}  {state}")

    if not args.no_plugins:
        skew = plugin_skew(paths)
        if skew:
            print()
            for s in skew:
                print(s)
                if s.startswith("[WARN]"):
                    findings.append(s)

    if findings:
        print()
        for f in findings:
            if not f.startswith("[WARN] plugin"):
                print(f)

    behind = sum(1 for r in rows if r[4] == "BEHIND")
    print()
    print(
        f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} checked - {behind} behind - "
        f"{fails} policy violation(s)"
    )
    if not any(t.get("version") for t in tools):
        print(
            "[NOTE] no entry declares a [version] block yet, so everything with a registry "
            "defaults to policy=latest. Add `pin` where a measurement depends on the "
            "version, and `floor` where a fix is required."
        )
    trust.closing()
    return 1 if ((args.strict and fails) or trust.skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
