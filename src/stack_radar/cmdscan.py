"""Shell-command scanning: a command line reduced to the executables it invokes.

It answers one question - which executables did this command run - and nothing about
why. Each case in tests/test_cmdscan.py is a mistake a simpler parser makes, and that
module is what keeps the answer from drifting.

WHAT IT COSTS TO GET WRONG, and in which direction: a simpler scanner errs BOTH ways.
Splitting a command on newlines reads heredoc bodies and quoted multi-line strings as
command lines, which invents invocations, and not following a runner chain loses them.
So correcting a scanner moves some per-tool counts down and others up; a correction
that only ever moved them down would be the easy case.

Stdlib only, no I/O, no state - the same constraint as the rest of the engine.
"""

from __future__ import annotations

import re

# A command line beginning with an environment assignment must resolve to the
# executable, not to a path fragment: F="C:/path/x" git -C "$F" status is git.
POSIX_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A bare redirection operator; the token after it is a filename, not a command.
REDIRECT = re.compile(r"^[0-9]*(?:>>|<<|>&|<&|&>|>|<)$")
ATTACHED_REDIRECT = re.compile(r"^[0-9]*(?:>>|>&|<&|&>|>|<)\S")

# Keywords that stand in front of a real command in the same segment.
TRANSPARENT = frozenset(
    {
        "!",
        "builtin",
        "command",
        "do",
        "elif",
        "else",
        "exec",
        "if",
        "nohup",
        "sudo",
        "then",
        "time",
        "{",
    }
)
# Keywords whose segment carries no invocation at all.
DEAD_SEGMENT = frozenset(
    {
        "case",
        "done",
        "esac",
        "fi",
        "for",
        "in",
        "select",
        "until",
        "while",
        "}",
    }
)

WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat")

# Runners that delegate to another executable. The value is the set of that
# runner's own flags which take a separate value token.
RUNNERS: dict[str, frozenset[str]] = {
    "uv": frozenset(
        {
            "--with",
            "--with-editable",
            "--with-requirements",
            "--python",
            "-p",
            "--project",
            "--directory",
            "--index",
            "--index-url",
            "--extra-index-url",
            "--constraint",
            "--override",
            "--cache-dir",
            "--config-file",
            "--color",
            "--refresh-package",
            "--env-file",
        }
    ),
    "uvx": frozenset({"--with", "--python", "-p", "--from", "--index", "--index-url"}),
    "npx": frozenset({"-p", "--package", "-c", "--call", "--node-options"}),
    "pnpm": frozenset(set()),
    "bunx": frozenset(set()),
    "pipx": frozenset({"--python", "--spec"}),
    "poetry": frozenset(set()),
    "pdm": frozenset(set()),
    "hatch": frozenset({"-e", "--env"}),
    "rye": frozenset(set()),
    "wsl": frozenset({"-d", "--distribution", "-u", "--user", "--cd"}),
    "xargs": frozenset({"-I", "-n", "-P", "-d", "-a", "-E", "-s"}),
}
# Runners that need a literal sub-verb before the delegated command.
RUNNER_VERB = {
    "uv": ("run", "tool"),
    "pipx": ("run",),
    "poetry": ("run",),
    "pdm": ("run",),
    "hatch": ("run",),
    "rye": ("run",),
    "pnpm": ("dlx", "exec"),
}
PYTHONS = re.compile(r"^(?:python|python[23](?:\.\d+)?|py|pypy[23]?)$")

# A backslash escapes only a shell metacharacter. Treating it as a universal
# escape mangles every Windows path -- C:\tools\gh.exe becomes C:toolsgh.exe --
# and transcripts written on Windows carry many PowerShell commands.
ESCAPABLE = frozenset(" \t\r\n\"'$`\\;|&<>()#")


def _skip_heredoc(s: str, i: int, delim: str) -> int:
    """Return the offset just past the heredoc terminated by ``delim``."""
    n = len(s)
    while i < n:
        j = s.find("\n", i)
        line = s[i:j] if j >= 0 else s[i:]
        if line.strip() == delim:
            return n if j < 0 else j + 1
        if j < 0:
            return n
        i = j + 1
    return n


def tokenize(command: str) -> list[list[str]]:
    """Split a command line into segments of tokens.

    Quoted spans, heredoc bodies and comments are consumed opaquely. This is the
    whole point: naive newline splitting treats embedded Python source and
    heredoc prose as commands, which is how "grep ruff docs/*.md" gets counted as
    a ruff invocation and how a heredoc mentioning a tool inflates its usage.
    """
    segments: list[list[str]] = []
    seg: list[str] = []
    tok: list[str] = []
    quoted = False
    heredocs: list[str] = []
    i, n = 0, len(command)

    def flush_tok() -> None:
        nonlocal quoted
        if tok or quoted:
            seg.append("".join(tok))
            tok.clear()
        quoted = False

    def flush_seg() -> None:
        flush_tok()
        if seg:
            segments.append(list(seg))
            seg.clear()

    while i < n:
        c = command[i]

        if c == "\\" and i + 1 < n and command[i + 1] in ESCAPABLE:
            if command[i + 1] == "\n":
                i += 2
                continue
            tok.append(command[i + 1])
            i += 2
            continue

        if c == "'":
            j = command.find("'", i + 1)
            j = n if j < 0 else j
            tok.append(command[i + 1 : j])
            quoted = True
            i = j + 1
            continue

        if c == '"':
            j, buf = i + 1, []
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    buf.append(command[j + 1])
                    j += 2
                    continue
                if command[j] == '"':
                    break
                buf.append(command[j])
                j += 1
            tok.append("".join(buf))
            quoted = True
            i = j + 1
            continue

        if c == "#" and not tok and not quoted:
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue

        if c == "\n":
            flush_seg()
            i += 1
            while heredocs:
                i = _skip_heredoc(command, i, heredocs.pop(0))
            continue

        if command.startswith("<<", i) and not command.startswith("<<<", i):
            i += 2
            if i < n and command[i] == "-":
                i += 1
            while i < n and command[i] in " \t":
                i += 1
            if i < n and command[i] in "'\"":
                q = command[i]
                j = command.find(q, i + 1)
                j = n if j < 0 else j
                delim = command[i + 1 : j]
                i = j + 1
            else:
                j = i
                while j < n and (command[j].isalnum() or command[j] in "_-."):
                    j += 1
                delim, i = command[i:j], j
            if delim:
                heredocs.append(delim)
            flush_tok()
            continue

        if command.startswith("&&", i) or command.startswith("||", i):
            flush_seg()
            i += 2
            continue

        if command.startswith("$(", i):
            flush_seg()
            i += 2
            continue

        if c in "|;`()":
            flush_seg()
            i += 1
            continue

        if c in " \t\r":
            flush_tok()
            i += 1
            continue

        tok.append(c)
        i += 1

    flush_seg()
    return segments


def normalise(token: str) -> str:
    """Reduce a command token to a bare executable name."""
    name = token.replace("\\", "/").rstrip("/")
    name = name.rsplit("/", 1)[-1]
    lowered = name.lower()
    for suffix in WINDOWS_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _skip_flags(tokens: list[str], i: int, value_flags: frozenset[str]) -> int:
    while i < len(tokens):
        t = tokens[i]
        if not t.startswith("-") or t == "-":
            return i
        if "=" in t:
            i += 1
            continue
        i += 2 if t in value_flags else 1
    return i


def resolve(tokens: list[str], depth: int = 0) -> list[str]:
    """Resolve one segment's tokens to the chain of executables it invokes.

    ``uv run ruff check`` yields ["uv", "ruff"]; ``uv run python -m pytest``
    yields ["uv", "python", "pytest"]; ``npx widget`` yields ["npx", "widget"].
    """
    if depth > 6:
        return []
    i = 0
    # Leading environment assignments and redirections are not the command.
    while i < len(tokens):
        t = tokens[i]
        if POSIX_ASSIGN.match(t):
            i += 1
            continue
        if t.startswith("$"):
            # A PowerShell assignment or a bare variable expansion.
            return []
        if REDIRECT.match(t):
            i += 2
            continue
        if ATTACHED_REDIRECT.match(t):
            i += 1
            continue
        break
    if i >= len(tokens):
        return []

    head = tokens[i]
    if head in DEAD_SEGMENT:
        return []
    if head in TRANSPARENT:
        return resolve(tokens[i + 1 :], depth + 1)
    if head == "env":
        return resolve(tokens[i + 1 :], depth + 1)

    name = normalise(head)
    if not name or name in DEAD_SEGMENT:
        return []
    if name in TRANSPARENT:
        return resolve(tokens[i + 1 :], depth + 1)

    rest = tokens[i + 1 :]
    lowered = name.lower()

    if lowered in RUNNERS:
        j = 0
        verbs = RUNNER_VERB.get(lowered)
        if verbs is not None:
            j = _skip_flags(rest, j, RUNNERS[lowered])
            if j >= len(rest) or rest[j] not in verbs:
                return [name]
            # "uv tool run x" needs one more verb consumed.
            if lowered == "uv" and rest[j] == "tool":
                j += 1
                if j >= len(rest) or rest[j] != "run":
                    return [name]
            j += 1
        # Look for -m before skipping flags: "uv run -m pytest" delegates to a
        # module, and a generic flag-skipper would step straight over it.
        value_flags = RUNNERS[lowered]
        while j < len(rest):
            t = rest[j]
            if t in ("-m", "--module"):
                if j + 1 < len(rest):
                    return [name, "python", normalise(rest[j + 1])]
                return [name, "python"]
            if not t.startswith("-") or t == "-":
                break
            j += 1 if "=" in t else (2 if t in value_flags else 1)
        return [name, *resolve(rest[j:], depth + 1)] if j < len(rest) else [name]

    if PYTHONS.match(lowered):
        j = 0
        while j < len(rest):
            t = rest[j]
            if t in ("-m", "--module"):
                if j + 1 < len(rest):
                    return ["python", normalise(rest[j + 1])]
                return ["python"]
            if t in ("-c", "--command", "-"):
                return ["python"]
            if t.startswith("-"):
                j += 1
                continue
            break
        return ["python"]

    # "uv run analyse.py" and "./script.py" are python invocations.
    if lowered.endswith(".py"):
        return ["python"]

    return [name]


def executables(command: str) -> tuple[list[str], list[str]]:
    """Return (resolved chain, raw segment heads) for a command line."""
    chain: list[str] = []
    raw: list[str] = []
    for tokens in tokenize(command):
        if not tokens:
            continue
        raw.append(tokens[0])
        for name in resolve(tokens):
            if name not in chain:
                chain.append(name)
    return chain, raw
