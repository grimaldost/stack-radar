"""`radar <verb>` - one subcommand per command the engine has.

WHY THE VERBS ARE A TABLE AND NOT AN `argparse` TREE. Each command owns its own parser,
its own `--help` and its own exit codes, and several of them have no parser at all
(`render` and `snapshot` take no arguments). Rebuilding them as subparsers here would
mean one file that has to be edited in step with every command, and the first thing to
rot would be the flags - which is the failure this repository spends a whole gate on
elsewhere. So this dispatches: it resolves the verb, hands the remaining arguments to the
command unchanged, and stays out of the way.

`sys.argv` IS REWRITTEN, deliberately, and it is the one piece of machinery here worth
knowing about. The commands read `sys.argv` directly - their parsers do, and so does the
`--data-root` reader in `paths`, which exists precisely because two commands have no
parser to thread an override through. Rewriting argv to `[<program> <verb>, *rest]` is
what makes `radar gate --data-root X` and the module run directly land on identical
arguments. Passing `rest` to a parser while leaving `sys.argv` alone would give the two
readers different strings, and the disagreement would show up only on the flag that has
no parser behind it.
"""

from __future__ import annotations

import importlib
import sys
import textwrap

# verb -> (module, callable). The module is imported ON DISPATCH rather than here: every
# command resolves a data root when it starts, `--help` must work in a directory that has
# none, and an import that reached the resolver would make the two contradict each other.
VERBS: dict[str, tuple[str, str]] = {
    "init": ("init", "main"),
    "add": ("init", "add"),
    "gate": ("gate", "main"),
    "render": ("render", "main"),
    "apply": ("apply", "main"),
    "field": ("field_report", "main"),
    "reconcile": ("reconcile", "main"),
    "snapshot": ("snapshot", "main"),
    "versions": ("versions", "main"),
    "sync-feedback": ("sync_feedback", "main"),
    "render-feedback-targets": ("render_feedback_targets", "main"),
    "redact-backfill": ("redact_backfill", "main"),
    "bootstrap": ("bootstrap", "main"),
    "changelog-gate": ("changelog_gate", "main"),
    "check-version-sites": (__name__.rsplit(".", 1)[-1], "check_version_sites"),
}

# One line each, for the verb list. Not read from the modules' docstrings: those open with
# the reason the command exists, which is the right first line for a reader inside the
# file and the wrong one for a list of every verb.
SUMMARY: dict[str, str] = {
    "init": "create a catalogue: the marker, a profile, the commit lane and CI",
    "add": "write one validated entry, with its first dated history block",
    "gate": "judge the catalogue and the governed stack; exit 1 on any FAIL",
    "render": "write the catalogue's artefacts from its entries",
    "apply": "converge a machine towards what an environment declares",
    "field": "field telemetry for an entry, from session transcripts",
    "reconcile": "compare what is installed against what the catalogue claims",
    "snapshot": "record today's upstream licence, activity and download figures",
    "versions": "compare installed versions against what upstream publishes",
    "sync-feedback": "move feedback reports from the inbox into the catalogue and index them",
    "render-feedback-targets": "write the feedback-target registry into the stack",
    "redact-backfill": "map private tool names and the home directory out of tracked content",
    "bootstrap": "survey a machine and propose an environment profile",
    "changelog-gate": "fail a change that moves a repository's code unrecorded",
    "check-version-sites": "check that the version sites of a repository agree",
}

# THE TWO THAT JUDGE THE DIRECTORY THEY ARE RUN FROM. They take no data
# root and check no `requires_framework`, which is how this engine's own CI runs them in a
# repository that carries no `radar.toml` at all. Listed here so the fact is stated once.
ROOTLESS = ("changelog-gate", "check-version-sites")

# The third verb that resolves no root, and it is a different case rather than a third
# member of the tuple above: `init` does not judge the directory it is run from, it MAKES
# the root the others need. A resolver here would search upwards and write into whatever
# catalogue happened to sit above the target.
CREATES_ROOT = ("init",)


def check_version_sites(argv: list[str]) -> int:
    """`radar check-version-sites [dir]` - the version sites of a repository agree.

    Rootless, like `changelog-gate`: the repository it judges is the working directory,
    not a data root, so it runs in this engine's own tree and in a catalogue alike. Which
    sites a repository has differs - a catalogue has no package - and the comparison is
    over whichever exist (`radar_lib.version_site_errors` lists them).
    """
    import argparse
    from pathlib import Path

    from .radar_lib import version_site_errors

    ap = argparse.ArgumentParser(
        prog="radar check-version-sites",
        description="Whether the version sites of a repository agree.",
    )
    ap.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="the repository to judge (default: the working directory)",
    )
    args = ap.parse_args(argv)
    root = Path(args.directory) if args.directory else Path.cwd()
    errs = version_site_errors(root)
    for err in errs:
        print(f"[FAIL] version sites: {err}")
    if not errs:
        print(f"[OK] the version sites of {root.as_posix()} agree")
    return 1 if errs else 0


def _quoted(verbs: tuple[str, ...]) -> str:
    return " and ".join("`" + v + "`" for v in verbs)


def usage() -> str:
    # WRAPPED RATHER THAN HAND-BROKEN. The paragraph names the two tuples above, so a verb
    # added to either one changes its length - and hand-broken lines with a name spliced
    # into them go ragged the moment that happens, which is how help text starts looking
    # unmaintained. `textwrap` is stdlib, like everything else here.
    blurb = textwrap.fill(
        "A tech radar for a Claude Code stack: adoption state, evidence-gated ring "
        "transitions, and a gate that judges both. The catalogue it reads lives in a "
        "separate directory marked by a `radar.toml` at its top, and most verbs find one by "
        "walking up from the working directory, or take `--data-root`. The exceptions: "
        f"{_quoted(ROOTLESS)} judge whatever repository they are run from, and "
        f"{_quoted(CREATES_ROOT)} creates the root the rest of them need.",
        width=80,
        initial_indent="  ",
        subsequent_indent="  ",
    )
    width = max(len(v) for v in VERBS)
    lines = [
        "usage: radar <verb> [args...]",
        "",
        blurb,
        "",
        "verbs:",
    ]
    lines += [f"  {verb:<{width}}  {SUMMARY[verb]}" for verb in VERBS]
    lines += ["", "`radar <verb> --help` for a verb's own options."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        # Exit 0 only when help was ASKED for. A bare `radar` is a command that did
        # nothing, and a shell that cannot tell it apart from a successful run is how a
        # typo in a script passes CI.
        return 0 if args else 2
    if args[0] in ("-V", "--version", "version"):
        from . import __version__

        print(__version__)
        return 0

    verb, rest = args[0], args[1:]
    if verb not in VERBS:
        near = [v for v in VERBS if v.startswith(verb[:3])]
        print(f"radar: unknown verb {verb!r}", file=sys.stderr)
        if near:
            print(f"did you mean: {', '.join(near)}", file=sys.stderr)
        print(f"verbs: {', '.join(VERBS)}", file=sys.stderr)
        return 2

    module_name, attr = VERBS[verb]
    if module_name == __name__.rsplit(".", 1)[-1]:
        command = globals()[attr]
    else:
        command = getattr(importlib.import_module(f".{module_name}", __package__), attr)

    # See the module docstring: the command's parser and the `--data-root` reader both
    # read `sys.argv`, so they are pointed at the same strings rather than one being
    # handed a copy.
    saved = sys.argv
    sys.argv = [f"radar {verb}", *rest]
    try:
        result = command(rest) if _takes_argv(command) else command()
    finally:
        sys.argv = saved
    return 0 if result is None else int(result)


def _takes_argv(command) -> bool:
    """Whether the command wants the remaining arguments as a list.

    The commands are not uniform and were not made uniform for this: `changelog_gate.main`
    has always taken an argv list and returned a code, while most take none and spend
    `sys.exit` themselves. Asking the signature is what lets both keep their own shape -
    the alternative is editing every command to suit its launcher.
    """
    import inspect

    try:
        return bool(inspect.signature(command).parameters)
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(main())
