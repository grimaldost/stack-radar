"""Discover a brand-new environment and write its profile or overlay. Installs nothing.

THE PROBLEM THIS SOLVES
-----------------------
A committed environment profile cannot know the machine it will land on. A committed
profile may ship with placeholder paths (`<home>/Documents`) precisely because guessing
would be worse than failing loudly — `radar apply --check` complains rather than inventing
a path. But somebody still has to turn those placeholders into real values, and asking
an agent to guess a home directory is exactly the kind of judgement that should not be a
judgement at all. It is computable, so it is computed here.

WHAT IS AND IS NOT AUTOMATED
----------------------------
Discovery is deterministic: paths, versions, what is already installed. That is this
module. Policy is not: whether this machine may reach an external MCP server, whether
network installs are permitted, which site-specific tools exist. Those need a human or an
agent that can read the local rules, and they live in the bootstrap prompt of
`docs/new-environment.md` in the engine's repository (`engine_docs`), instead.

What `--write` writes depends on what exists. With no committed profile for the
environment, it writes a starter `environments/<env>.toml`, with the home directory
written as `~`, for review and commit. With a committed profile, it compares the paths
every other verb would read - the profile with its overlay layered on - with this
machine, and when they disagree it writes the overlay, `environments/<env>.local.toml`,
which is gitignored: those paths are true of one workstation and false everywhere else,
and a committed profile that carried them would break every other machine that pulled
it. An overlay that exists already keeps every key it sets; only the `[paths]` this
machine disagrees on change, and a path the committed profile now gets right is dropped
from it, so a stale overlay is found and mended like a stale profile.

Read-only unless `--write` is passed. Nothing here installs, mounts or configures
anything — `radar apply` does that, and it asks first.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tomllib
from pathlib import Path

from .paths import UnsafeWrite, env_dir, write_inside
from .radar_lib import (
    NAME_RULE,
    OVERLAY_KEYS,
    begin_command,
    engine_docs,
    environment_name,
    overlay_name,
    read_committed_profile,
    read_overlay,
    reports_profile_errors,
    with_home,
)
from .trust import run_program

MIN_PYTHON = (3, 11)  # tomllib

# Commands worth knowing about before proposing a plan. `claude` is what makes an agent
# session possible at all; uv is how most entries install; git is how this repo arrived.
PROBES = [
    ("git", ["git", "--version"]),
    ("python", [sys.executable, "--version"]),
    ("uv", ["uv", "--version"]),
    ("node", ["node", "--version"]),
    ("npm", ["npm", "--version"]),
    ("claude", ["claude", "--version"]),
]


def run(cmd: list[str], timeout: int = 20) -> str | None:
    """The first line `cmd` prints, or None when its program is not on PATH or cannot run."""
    try:
        p = run_program(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return out[0] if out else f"exit {p.returncode}"


def write_profile(root: Path, path: Path, text: str) -> None:
    """Write a profile or an overlay into environments/, or stop on a `[FAIL]` naming why
    the file may not be written there (`paths.write_inside`)."""
    try:
        write_inside(root, path, text)
    except UnsafeWrite as exc:
        print(f"  [FAIL] {exc} - nothing written")
        raise SystemExit(1) from None


def documents_dir(home: Path) -> Path | None:
    """The user's documents root, without pretending to be certain.

    Windows localises this folder and OneDrive relocates it, so a hardcoded 'Documents'
    is a guess that fails silently on a redirected profile. Candidates are probed in
    order of likelihood and the first that EXISTS wins; None is a legitimate answer and
    is reported as such rather than defaulted.
    """
    candidates = [home / "Documents", home / "documents"]
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveCommercial")
    if one:
        candidates.insert(0, Path(one) / "Documents")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def claude_home(home: Path) -> Path | None:
    p = home / ".claude"
    return p if p.is_dir() else None


def claude_config(home: Path) -> Path | None:
    """Claude Code's own top-level config, a SIBLING of claude_home rather than a file in it.

    Its own probe because the profiles need it as its own `[paths]` key: `reconcile.py`
    reads the mounted MCP servers from it, and deriving it from `claude_home` would be a
    second place that decides where it is.
    """
    p = home / ".claude.json"
    return p if p.is_file() else None


def home_dir() -> Path:
    """The user's home directory.

    THE ONE `Path.home()` IN THE ENGINE, and the reason it is a function here rather than a
    call at each site: this module's job is to find the machine, so the publication check
    exempts this file by name and fails the call anywhere else. A caller that needs the home
    directory - `init`, which creates a data root and therefore has no profile to read it
    from - asks this instead of reaching for the machine itself.
    """
    return Path.home()


def discovered_paths(home: Path) -> dict[str, str]:
    """The `[paths]` a profile can be given without asking anyone: every key, found or not.

    Every key is present and an absent one is the EMPTY STRING rather than missing, because
    the two callers need to tell "not found" from "not looked for": `main()` compares
    discovered against declared and must not report drift on a path it never probed, and
    `init` writes a commented-out line so the operator can see which value it failed to
    find rather than inheriting a guess.
    """
    docs, chome, cconfig = documents_dir(home), claude_home(home), claude_config(home)
    return {
        "documents": as_posix(docs) or "",
        "claude_home": as_posix(chome) or "",
        "claude_config": as_posix(cconfig) or "",
        "feedback_targets": (f"{as_posix(chome)}/feedback-targets.toml" if chome else ""),
        "feedback_root": f"{as_posix(chome)}/feedback" if chome else "",
    }


def installed_plugins(home: Path) -> list[str]:
    f = home / ".claude" / "plugins" / "installed_plugins.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted((data.get("plugins") or {}).keys())


def mounted_mcp(home: Path) -> list[str]:
    f = home / ".claude.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = set((data.get("mcpServers") or {}).keys())
    for proj in (data.get("projects") or {}).values():
        if isinstance(proj, dict):
            names |= set((proj.get("mcpServers") or {}).keys())
    return sorted(names)


def load_profile_raw(root: Path, name: str) -> dict | None:
    f = env_dir(root) / f"{name}.toml"
    if not f.is_file():
        return None
    # Expanded like every other reader's, or a declared `~/.claude` would read as drift
    # from the discovered path it names.
    return with_home(read_committed_profile(f), home_dir())


def as_posix(p: Path | None) -> str | None:
    return p.as_posix() if p else None


def _slashes(value: object) -> str:
    return str(value).replace("\\", "/")


def _declared_exists(declared: str, home: Path) -> bool:
    """Whether a declared `[paths]` value names something that exists on this machine.

    A leading `~` is the home the profile stands for (`radar_lib.expand_home`'s rule), so
    it is expanded before the check; any other value is taken as written. Used only to
    tell a declared path that is simply elsewhere - a valid custom location discovery does
    not probe - from one that is not on this machine at all."""
    if not declared:
        return False
    text = declared.replace("\\", "/")
    if text == "~" or text.startswith("~/"):
        text = home.as_posix() + text[1:]
    elif text.startswith("~"):
        # `~other` is another account's home; not this machine's to resolve or judge.
        return True
    try:
        return Path(text).exists()
    except OSError:
        return False


def _toml_key(key: str) -> str:
    bare = key and all(c.isascii() and (c.isalnum() or c in "_-") for c in key)
    return key if bare else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    """A TOML value for the shapes an overlay holds; ValueError for any other."""
    if isinstance(value, bool):  # before int: bool is an int in Python
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"cannot write {value!r} back; edit the overlay by hand")


def overlay_text(env: str, name: str, keep: dict | None, paths: dict[str, str]) -> str:
    """The overlay `radar bootstrap --write` writes: `paths` as its `[paths]`, and every
    other key `keep` (the overlay already there) sets, as it set it.

    There is no TOML writer in the standard library, and the overlay's keys are a closed
    set (`OVERLAY_KEYS`) of simple values, so this writes exactly those and reads the result
    back: a value it cannot write, or text that does not read back as what was meant, is a
    ValueError rather than a file that says something else.
    """
    keep = keep or {}
    top = [k for k in OVERLAY_KEYS if k not in ("name", "paths", "flags") and k in keep]
    flags = keep.get("flags") or {}
    lines = [
        f"name = {_toml_value(env)}",
        "",
        "# Machine-local overlay kept by `radar bootstrap --write`. GITIGNORED on purpose:",
        "# these paths are true of this workstation and false of every other one, so a",
        "# committed profile carrying them would break the next machine that pulled it.",
        f'# The {env!r} profile names this file (overlay = "{name}" under [flags]),',
        "# so every radar command that reads the profile layers this file over it.",
        "",
        *(f"{_toml_key(k)} = {_toml_value(keep[k])}" for k in top),
        *([""] if top else []),
        "[paths]",
        *(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in paths.items()),
    ]
    if flags:
        lines += ["", "[flags]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in flags.items()]
    text = "\n".join(lines) + "\n"
    meant = {"name": env, **{k: keep[k] for k in top}, "paths": paths}
    if flags:
        meant["flags"] = flags
    try:
        back = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the rewritten overlay would not read back ({exc})") from exc
    if back != meant:
        raise ValueError("the rewritten overlay would not read back as written")
    return text


def collapse_home(paths: dict[str, str], home: Path) -> dict[str, str]:
    """`[paths]` with a leading home directory written back as `~`.

    The inverse of `radar_lib.expand_home`, and the pair is the point: the starter profile
    lands in the TRACKED `environments/<env>.toml`, so it must not spell the account out.
    Every reader expands the `~` back, so the file says the same thing and names nobody.

    Only the home itself or the home followed by a separator counts. A sibling directory
    the home happens to be a prefix of - `<home>-old`, `<home>2` - is not this account's,
    and collapsing it would claim a path that is somebody else's.
    """
    h = home.as_posix()
    out: dict[str, str] = {}
    for key, value in paths.items():
        s = str(value).replace("\\", "/")
        if s == h:
            out[key] = "~"
        elif s.startswith(h + "/"):
            out[key] = "~" + s[len(h) :]
        else:
            out[key] = value
    return out


def starter_profile(name: str, paths: dict[str, str], home: Path | None = None) -> str:
    """A new profile, deliberately narrow. Widening is a decision; starting wide is a bet."""
    paths = collapse_home(paths, home or home_dir())
    body = [
        f'name = "{name}"',
        'description = "FILL IN: what this machine is and what policy applies to it."',
        "",
        "# Start with what is already measured and adopted. A pilot is an experiment:",
        '# add "pilot" only on a machine you are prepared to see broken by one.',
        'rings = ["own", "adopt"]',
        "",
        "# Exclusions with a reason each, so a later reader knows policy from preference.",
        "exclude = []",
        "",
        "# NAMES of required environment variables, never values. `radar apply` checks",
        "# presence and never prints contents.",
        "require_env = []",
        "",
        "[paths]",
    ]
    for k, v in paths.items():
        body.append(f'{k} = "{v}"' if v else f'# {k} = "FILL IN: not found automatically"')
    body += [
        "",
        "[flags]",
        "# Both default to false: err toward installing nothing until the local rules are",
        "# known. Turning these on is a policy decision, not a convenience.",
        "allow_external_mcp = false",
        "allow_network_install = false",
        "",
        '# "git-remote" if this machine can reach the catalogue\'s remote (the common case);',
        '# "git-bundle" for a machine that receives updates by carried bundle (append-only',
        "# reports make bundles conflict-free). A record for people; the engine does not read it.",
        'sync = "git-remote"',
        "",
        "# The gitignored file that holds the paths only this machine has. Every radar verb",
        "# that reads this profile layers that file over it when it exists, and",
        "# `radar bootstrap --write` keeps it in step with the machine.",
        f'overlay = "{name}.local.toml"',
    ]
    return "\n".join(body) + "\n"


@reports_profile_errors
def main() -> None:
    ap = argparse.ArgumentParser(prog="radar bootstrap", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--env",
        required=True,
        type=environment_name,
        help=f"environment name, e.g. laptop: {NAME_RULE}",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="write a starter profile if none exists, or else the overlay for this "
        "machine's differing paths; otherwise report only",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)

    if sys.version_info < MIN_PYTHON:
        print(
            f"[FAIL] python {'.'.join(map(str, MIN_PYTHON))}+ required "
            f"(tomllib); this is {platform.python_version()}"
        )
        raise SystemExit(2)

    home = home_dir()
    docs, chome = documents_dir(home), claude_home(home)

    print(f"stack-radar bootstrap - environment {args.env!r}")
    print(f"  platform : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  python   : {platform.python_version()}  ({sys.executable})")
    print(f"  home     : {home.as_posix()}")
    print(f"  documents: {as_posix(docs) or 'NOT FOUND - fill in by hand'}")
    print(f"  claude   : {as_posix(chome) or 'NOT FOUND - is Claude Code installed?'}")
    print()

    print("  commands found:")
    missing = []
    for label, cmd in PROBES:
        ver = run(cmd)
        print(f"    {label:8} {ver or '-- not found'}")
        if ver is None:
            missing.append(label)
    print()

    plugins, servers = installed_plugins(home), mounted_mcp(home)
    print(f"  plugins already installed : {', '.join(plugins) or 'none'}")
    print(f"  mcp servers already mounted: {', '.join(servers) or 'none'}")
    print()

    discovered = discovered_paths(home)

    profile = load_profile_raw(root, args.env)

    if profile is None:
        print(f"  no committed profile environments/{args.env}.toml")
        text = starter_profile(args.env, discovered)
        if args.write:
            dest = env_dir(root) / f"{args.env}.toml"
            write_profile(root, dest, text)
            print(f"  [WROTE] {dest.relative_to(root)} - review it, then commit it")
        else:
            print("  proposed starter profile (re-run with --write to create it):\n")
            print("\n".join("    " + ln for ln in text.splitlines()))
        raise SystemExit(0)

    committed = {**profile, "_file": f"{args.env}.toml"}
    named = overlay_name(committed)
    overlay_path = env_dir(root) / (named or f"{args.env}.local.toml")
    # The overlay as written, read even when the profile does not name it, so that
    # rewriting it never drops what it holds. Only a named overlay is layered for the
    # comparison, because only a named one is layered by the other verbs.
    existing = read_overlay(committed, root, overlay_path.name)
    layered = with_home(existing, home_dir()) if (existing and named) else {}
    committed_paths = profile.get("paths") or {}
    over_paths = layered.get("paths") or {}
    declared = {**committed_paths, **over_paths}
    drift = {k: v for k, v in discovered.items() if v and _slashes(declared.get(k, "")) != v}
    # A key the profile DECLARES that discovery did not find, and whose declared path does
    # not exist here. Drift covers only the keys discovery found (`if v`), so such a key
    # gets its own state: it is not drift (there is no discovered value to move to) and not
    # a match, because the path is not on this machine, and while one exists the summary
    # does not say that every declared path matches.
    missing_here = {
        k
        for k in discovered
        if declared.get(k) and not discovered[k] and not _declared_exists(str(declared[k]), home)
    }

    heading = f", {overlay_path.name} layered on:" if layered else ":"
    print("  declared vs discovered paths" + heading)
    for k in discovered:
        d, f = str(declared.get(k, "-")), discovered[k] or "-"
        mark = "->" if k in drift else ("!!" if k in missing_here else "ok ")
        print(f"    {mark} {k:17} declared={d}{' (overlay)' if k in over_paths else ''}")
        if k in drift:
            print(f"       {'':17} actual  ={f}")
        elif k in missing_here:
            print(f"       {'':17} does not exist here, and was not discovered")
    print()

    # The overlay's [paths] as they should be. A discovered path the committed profile
    # already has needs no overlay key, and one it lacks or disagrees with does; a key the
    # overlay sets that nothing here discovered is the operator's own and stays.
    written = dict((existing or {}).get("paths") or {})
    wanted = dict(written)
    expanded = (with_home(existing, home_dir()) if existing else {}).get("paths") or {}
    for k, v in discovered.items():
        if not v:
            continue
        if _slashes(committed_paths.get(k, "")) == v:
            wanted.pop(k, None)
        elif _slashes(expanded.get(k, "")) != v:
            wanted[k] = v
    changed = sorted(k for k in {*wanted, *written} if wanted.get(k) != written.get(k))

    if missing_here:
        # Never "matches this machine" while a declared path is absent. A stranger reading
        # the summary would otherwise trust a description the machine does not meet. This
        # is its own report, printed beside whatever the discovered drift asks for below.
        print(
            f"  declared path(s) not found here: {', '.join(sorted(missing_here))} - each is "
            "declared but does not exist on this machine and was not discovered. Correct "
            "the profile, or drop the key if this machine does not have it."
        )
        stale = sorted(over_paths.keys() & missing_here)
        if args.write and stale:
            print(
                f"  [NOTE] {overlay_path.name} sets {', '.join(stale)} to a path that does "
                "not exist here; drop or comment the key out if this machine does not have it."
            )

    if changed:
        try:
            text = overlay_text(args.env, overlay_path.name, existing, wanted)
        except ValueError as exc:
            print(f"  [FAIL] environments/{overlay_path.name}: {exc} - nothing written")
            raise SystemExit(1) from None
        if args.write:
            write_profile(root, overlay_path, text)
            where = overlay_path.relative_to(root)
            if existing is None:
                print(f"  [WROTE] {where} ({len(wanted)} path(s))")
            else:
                print(
                    f"  [WROTE] {where} - [paths] changed: {', '.join(changed)}; every other "
                    "key kept (comments are not)"
                )
        else:
            what = "updated" if existing is not None else "new"
            print(f"  {overlay_path.name} as --write would write it ({what}):\n")
            print("\n".join("    " + ln for ln in text.splitlines()))
    elif drift:
        # Nothing to write, but discovery differs from the committed profile: an overlay
        # the profile does not name already holds what this machine needs, and no verb
        # reads it.
        print(
            f"  {overlay_path.name} already sets {', '.join(sorted(drift))} as this machine "
            "has it; nothing to write."
        )
    elif not missing_here:
        if over_paths:
            print(
                f"  every declared path matches this machine, with {overlay_path.name} "
                f"layered on (it sets {', '.join(sorted(over_paths))})."
            )
        else:
            print("  every declared path matches this machine; no overlay needed.")
    if named != overlay_path.name and (changed or existing is not None):
        # A profile names its own overlay, and a file it does not name is read by no verb,
        # so writing one, or leaving one in place, without saying so would mislead.
        print(
            f"  [NOTE] environments/{args.env}.toml does not name this overlay: add "
            f'overlay = "{overlay_path.name}" under [flags], or no verb reads it'
        )
    print()

    # One command per line and nothing beside it: cmd has no trailing comment, so a
    # `# ...` after a command would reach it as arguments. What each step means is said
    # before and after the list instead.
    if missing:
        print(f"  install first: {', '.join(missing)}")
    print("  next:")
    print(f"    - radar apply --env {args.env} --plan")
    print("    - radar gate")
    print()
    print("  `--plan` writes nothing, and the gate should report 0 FAIL. The policy")
    print(f"  decisions are in {engine_docs('new-environment.md')}")


if __name__ == "__main__":
    main()
