"""Render an environment's feedback-targets file from the own-ring [feedback] blocks.

The output is a data-plane artefact: a flat TOML file of registered feedback-report
targets that a consumer reads directly. As a data-plane file it must be
self-sufficient and must not point back here - so the generated header says the
file is generated and hand-edits are lost, but never says by what, and the file
carries no path into the catalogue. Delete the catalogue and the artefact still
resolves exactly as it reads.

Content is a pure function of the inputs (no timestamp), so re-running is a no-op
and `--check` is meaningful. Paths in a `[feedback]` block may use the same
`{documents}` / `{claude_home}` placeholders as an `[install]` block, so one entry
renders correctly on every machine.

WHERE IT WRITES is catalogue data - `[paths].feedback_targets` in a profile, and one
inbox directory per tool under `[paths].feedback_root` - so each destination is checked
before anything is written:

- the file must end in `.toml` and resolve inside the profile's `claude_home` or the
  data root. `claude_home` comes from the same profile, so this bound catches a mistake
  rather than a hostile profile, which can name any `claude_home` it likes;
- inside the data root it must be named `feedback-targets.toml` and sit outside the
  catalogue's own directories (`tools/`, `environments/`, `feedback/`, `tools.local/`,
  `.git/`). A profile could otherwise rewrite a catalogue file (`radar.toml`), add a tool
  entry, or create a configuration file the catalogue's own toolchain reads (`uv.toml`,
  `ruff.toml`) - so the one name this command writes is the only one it accepts there,
  whatever `claude_home` the profile declares;
- a file already there must be one this command wrote - it starts with the generated
  header - so no other file, wherever the profile points, is ever overwritten. A file
  written some other way has to be moved aside by hand first.

A tool's inbox directory is named after the tool, so the name must be one plain path
component (the schema's name rule, `radar_lib.NAME_RULE`) and must
resolve under `feedback_root` and not onto it: a tool called `../x` cannot create a
directory beside it, and one called `a/b` cannot nest one.

    radar render-feedback-targets --env <name> [--stdout|--check]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .paths import is_link, replace_file, write_error
from .radar_lib import (
    NAME_RULE,
    begin_command,
    environment_name,
    is_plain_name,
    load_environment,
    reports_profile_errors,
    tools_for_environment,
)
from .trust import inside, strictly_inside

SCHEMA = "feedback-targets/v1"

# A TOML bare key. A tool name outside it is written as a quoted key, so a name with a
# dot does not open a nested table and a name with a bracket does not close this one.
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

HEADER = """# feedback-targets - the registered feedback-report destinations.
#
# GENERATED FILE - do not hand-edit. It is rewritten wholesale on every
# regeneration and hand edits are lost; change the source of the binding instead.
#
# Plain data, self-sufficient: absolute paths, no includes, no tooling required to
# read it, and no dependency on whatever wrote it. There is deliberately no
# timestamp - the content is a pure function of its inputs, so an unchanged
# binding regenerates byte-identically.
"""


# TOML's short escapes. Every other control character is written as a \uXXXX escape: a
# raw newline or tab in a basic string makes the whole generated file fail to parse, for
# every consumer of it.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def toml_str(s: str) -> str:
    """`s` as a TOML basic string, with every character TOML forbids raw escaped."""
    out = []
    for ch in s:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def posix(p: str | Path) -> str:
    # Paths are stored with Windows separators in places; the artefact is
    # single-quoted-free TOML, so normalise to forward slashes and escape nothing.
    return Path(str(p).replace("\\", "/")).as_posix()


def subst(s: str, paths: dict) -> str:
    """Expand the environment profile's [paths] placeholders, as install blocks do."""
    for key in ("documents", "claude_home", "feedback_root"):
        v = paths.get(key)
        if v:
            s = s.replace("{" + key + "}", posix(v))
    return s


# The first line of HEADER: what marks a file as one this command wrote.
GENERATED = HEADER.splitlines()[0]

# The only name a destination may have, wherever it is, and the catalogue's own
# directories it may not sit in inside the data root. A name rather than a list of files
# to avoid, because the files a toolchain reads are open-ended (pyproject.toml, uv.toml,
# ruff.toml, ...), and a new one in a directory changes how that toolchain configures
# every project below it.
DATA_ROOT_NAME = "feedback-targets.toml"
CATALOGUE_DIRS = frozenset({"tools", "environments", "feedback", "tools.local", ".git"})


def table_key(name: str) -> str:
    return name if BARE_KEY.match(name) else toml_str(name)


def destination_error(dest: Path, paths: dict, root: Path) -> str | None:
    """Why the artefact may not be written at `dest`, or None when it may.

    Each rule is applied to the path as spelled and to where it resolves, so a symbolic
    link cannot carry the write to another name, another suffix or another directory.
    """
    if dest.suffix != ".toml":
        return f"{dest.as_posix()} does not end in .toml - refused"
    if is_link(dest):
        return f"{dest.as_posix()} is a symbolic link; the registry is not written through one - refused"
    try:
        real = dest.resolve()
    except (OSError, RuntimeError):
        return f"{dest.as_posix()} cannot be resolved - refused"
    if real.suffix != ".toml":
        return f"{dest.as_posix()} resolves to {real.as_posix()}, which does not end in .toml - refused"
    if dest.name != DATA_ROOT_NAME or real.name != DATA_ROOT_NAME:
        return f"{dest.as_posix()} is not named {DATA_ROOT_NAME} - refused"
    homes = [root]
    if paths.get("claude_home"):
        homes.insert(0, Path(posix(paths["claude_home"])))
    if not any(inside(dest, h) for h in homes):
        allowed = " or ".join(h.as_posix() for h in homes)
        return f"{dest.as_posix()} is outside {allowed} - refused"
    spelled_in_root = Path(os.path.abspath(dest)).is_relative_to(os.path.abspath(root))
    if spelled_in_root or inside(dest, root):
        # Inside the data root, a link on the way, or a path that resolves out of it, is
        # refused whatever it leads to (`paths.write_error`).
        problem = write_error(root, dest)
        if problem:
            return f"{problem} - refused"
        rel = real.relative_to(root.resolve())
        if rel.name != DATA_ROOT_NAME:
            return (
                f"{dest.as_posix()} is inside the data root and is not named "
                f"{DATA_ROOT_NAME} - refused"
            )
        if rel.parts and rel.parts[0] in CATALOGUE_DIRS:
            return f"{dest.as_posix()} is inside the catalogue's own {rel.parts[0]}/ - refused"
    if dest.exists():
        try:
            with dest.open(encoding="utf-8", errors="replace") as fh:
                first = fh.readline().rstrip("\n")
        except OSError:
            first = ""
        if first != GENERATED:
            return (
                f"{dest.as_posix()} exists and was not written by this command - refused; "
                "move it aside if it may be replaced"
            )
    return None


def worktree_of(tool: dict, paths: dict) -> str | None:
    """The local checkout the consumer reads manifests and sources from."""
    fb = tool.get("feedback") or {}
    wt = fb.get("worktree")
    if wt:
        return posix(subst(str(wt), paths))
    repo = str(tool.get("repo", ""))
    if repo.startswith(("http://", "https://", "git@")):
        return None
    return posix(subst(repo, paths))


def render(env: dict) -> tuple[str, list[str]]:
    """The artefact text, plus findings. FAIL findings mean do not write."""
    findings: list[str] = []
    lines = [HEADER, f"schema = {toml_str(SCHEMA)}", ""]
    paths = env.get("paths") or {}
    inbox_root = posix(paths["feedback_root"]) if paths.get("feedback_root") else None

    targets = [t for t in tools_for_environment(env) if t.get("feedback")]
    for tool in sorted(targets, key=lambda t: t["name"]):
        name = tool["name"]
        fb = tool["feedback"]
        rel = fb.get("dir")
        if not rel:
            findings.append(f"[FAIL] {name}: [feedback] has no `dir`")
            continue
        wt = worktree_of(tool, paths)
        if not wt:
            findings.append(
                f"[FAIL] {name}: remote `repo` and no [feedback].worktree - "
                "the consumer needs a local checkout"
            )
            continue
        if not Path(wt).is_dir():
            findings.append(f"[WARN] {name}: worktree not present here: {wt}")

        # The registered destination is the data-plane INBOX, never the catalogue's archive.
        # Pointing the consumer at the archive would put a control-plane path into a
        # data-plane file and make the stack depend on the catalogue's presence: delete the
        # catalogue and the consumer would have nowhere to write. The radar ingests from the
        # inbox and mirrors the merged corpus back (`radar sync-feedback`).
        if not inbox_root:
            findings.append(f"[FAIL] {name}: [paths].feedback_root is not set for this environment")
            continue
        if not is_plain_name(name):
            findings.append(
                f"[FAIL] {name}: this tool name is not a plain file name ({NAME_RULE}) - "
                "a feedback directory cannot be registered for it"
            )
            continue
        if not strictly_inside(Path(inbox_root) / name, Path(inbox_root)):
            findings.append(
                f"[FAIL] {name}: this tool name resolves outside [paths].feedback_root, or "
                "onto it - a feedback directory cannot be registered for it"
            )
            continue
        lines.append(f"[targets.{table_key(name)}]")
        lines.append(f"repo = {toml_str(wt)}")
        lines.append(f"feedback_dir = {toml_str(posix(Path(inbox_root) / name))}")
        for key in ("format_doc", "triage_template"):
            v = fb.get(key)
            if not v:
                continue
            p = posix(Path(wt) / v)
            if not Path(p).is_file():
                findings.append(f"[WARN] {name}: {key} not found: {p}")
            lines.append(f"{key} = {toml_str(p)}")
        extras = fb.get("extras") or []
        if extras:
            lines.append("extras = [")
            for e in extras:
                lines.append(f"  {toml_str(e)},")
            lines.append("]")
        lines.append("")

    if not targets:
        findings.append(f"[WARN] {env['name']}: no tool in this environment declares [feedback]")
    return "\n".join(lines).rstrip("\n") + "\n", findings


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="radar render-feedback-targets",
        description="Write the feedback-target registry into the data plane.",
    )
    ap.add_argument("--env", required=True, type=environment_name, help="environment name")
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    ap.add_argument(
        "--stdout", action="store_true", help="print the artefact instead of writing it"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="report only whether the written file is current; write nothing",
    )
    return ap


@reports_profile_errors
def main() -> None:
    # Parsed before the data root is resolved, so `--help` prints usage without needing
    # a marker to be found.
    args = build_parser().parse_args()
    root = begin_command(args.data_root)
    env_name = args.env
    env = load_environment(env_name)
    if env is None:
        print(f"[FAIL] no environment named {env_name!r} in environments/")
        sys.exit(1)

    text, findings = render(env)
    fails = sum(1 for f in findings if f.startswith("[FAIL]"))
    for f in findings:
        print(f)

    if args.stdout:
        if not fails:
            print(text, end="" if text.endswith("\n") else "\n")
        sys.exit(1 if fails else 0)

    out = (env.get("paths") or {}).get("feedback_targets")
    if not out:
        print(f"[FAIL] {env_name}: [paths].feedback_targets is not set")
        sys.exit(1)
    dest = Path(posix(out))
    refusal = destination_error(dest, env.get("paths") or {}, root)
    if refusal:
        print(f"[FAIL] {env_name}: [paths].feedback_targets {refusal}")
        sys.exit(1)
    if not dest.parent.is_dir():
        print(
            f"[FAIL] {env_name}: {dest.parent} does not exist - run this on that "
            "machine, or use --stdout"
        )
        sys.exit(1)
    if fails:
        print(f"\nnot written ({fails} FAIL)")
        sys.exit(1)

    current = dest.read_text(encoding="utf-8") if dest.is_file() else None
    if args.check:
        if current == text:
            print(f"[NOTE] {dest} is current")
            sys.exit(0)
        print(f"[FAIL] {dest} is stale or missing - regenerate")
        sys.exit(1)

    # The artefact promises these inbox directories; make them real so the consumer's
    # first write does not have to. They live in the data plane and outlive the catalogue.
    # render() has already refused a name that resolves outside feedback_root, and a FAIL
    # stops before this point; the check is repeated because this is the write.
    inbox_root = (env.get("paths") or {}).get("feedback_root")
    if inbox_root:
        base = Path(posix(inbox_root))
        for tool in tools_for_environment(env):
            name = tool["name"]
            if not (tool.get("feedback") or {}).get("dir"):
                continue
            if not is_plain_name(name) or not strictly_inside(base / name, base):
                continue
            try:
                (base / name).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"[FAIL] {name}: could not create {(base / name).as_posix()}: {exc}")
                sys.exit(1)

    if current == text:
        print(f"[NOTE] {dest} unchanged")
    else:
        # Replaced rather than written through: destination_error refused a link, and a
        # link that appears since is replaced, not followed.
        replace_file(dest, text)
        print(f"wrote {dest}")


if __name__ == "__main__":
    main()
