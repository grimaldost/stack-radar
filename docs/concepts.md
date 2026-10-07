# Concepts

The vocabulary, and the reasoning behind each word. Nothing here assumes you have used a
tech radar before, or that you have anything installed yet.

Two examples run through the whole document, both boring on purpose:
[`ruff`](https://github.com/astral-sh/ruff), a Python linter and formatter, and
[`just`](https://github.com/casey/just), a command runner. One you have probably already
committed to; the other you have probably been meaning to try. Those are the two states most
of a catalogue is in.

## The engine and the catalogue are two different things

`stack-radar` is the **engine**: a package that installs one command, `radar`, and holds no
data.

Your **catalogue** is a separate directory — a git repository of your own — holding one TOML
file per tool, your environment profiles, and the artefacts the engine renders from them. A
directory becomes a catalogue by carrying a `radar.toml` at its top. That file is called the
marker, and the engine finds a catalogue by walking up from the working directory until it
meets one, the way `git` finds `.git`. The engine's documentation and its messages also call
a catalogue a **data root**.

The split has three consequences, and each one is the reason for a design decision further
down:

- **The engine knows no catalogue by name.** One installed engine serves as many catalogues
  as you have. Nothing in the engine names your tools, your machines or your paths; the
  vocabularies it fixes, such as the rings, are the same for every catalogue, and the one
  a catalogue is most likely to need its own of - the axis set below - has a default the
  catalogue can replace.
- **Your catalogue is yours.** It holds the names of your tools, your machines' paths, and
  your reasons for dropping things. None of that is in the engine, so upgrading the engine
  cannot leak it and publishing the engine cannot publish it.
- **A version range is checkable.** `radar.toml` declares `requires_framework`, the range of
  engine versions the catalogue can be read by (in the marker's key names, *framework*
  means the engine). An old engine reading a new catalogue fails loudly instead of
  silently ignoring a field it does not know about.

`radar init` creates a catalogue. Two verbs are the exception to all of this —
`radar changelog-gate` and `radar check-version-sites` judge whatever repository they are run
from and need no catalogue at all.

## The control plane, and the invariant that makes it reversible

Your catalogue is a **control plane**. The thing it governs — `~/.claude`, its plugins and
skills, the MCP servers you have mounted, the repositories on your machine — is the **data
plane**.

Knowledge flows one way. The catalogue reads and writes the stack; **no file in the stack may
ever reference the catalogue.** Not a path in `settings.json`, not an import, not a hook.

That is one rule, and it buys one property: if the catalogue directory were deleted right
now, nothing would break. Every install stays installed. Every plugin still loads. You would
lose the record of how the stack got to be the way it is, and not the stack. Adopting a
radar is therefore reversible.

A rule this easy to break by accident is worth checking mechanically, so `radar gate` scans
the data plane for references to the catalogue. The details are under *The invariant scan*
below.

## Rings — how committed you are

Five rings. The order is not a ladder anything climbs automatically.

| ring | what it claims |
|---|---|
| `own` | you wrote it — you can land a fix |
| `adopt` | the default choice for its problem; present and in use |
| `pilot` | a running experiment with a deadline |
| `observe` | interesting, not committed to; costs nothing and claims nothing |
| `discard` | evaluated and rejected, or dropped after use |

`ruff` is `adopt`: it is the linter in the Python projects on the machine, and that is a
fact about the machine, not a plan. `just` is `observe` until it has actually been tried,
and moving it to `pilot` is a decision with a date on it.

**A ring is a claim about observed state, not about intent.** `adopt` and `pilot` require
presence — an install probe that passes, an installer's own record, a real dependency in a
project, a mounted server. If a tool is not actually there, the honest ring is `observe`.
This is the rule that stops a radar from decaying into a wishlist.

`radar reconcile` compares the rings against what the machine shows — an approved install
probe that passes, an installer's own record where the probe gave no answer, a real project
dependency, a mounted server — and proposes demoting an `adopt` or `pilot` entry with none
of them. The projects are the directories under `--projects-root` (by default, the one that
holds the data root) with a `pyproject.toml`; a directory carrying `radar.toml` is a
catalogue, so the catalogue's own `pyproject.toml` and its worktrees' are not counted. It
never proposes that on a guess: an entry whose probe is not approved or UNKNOWN, or whose
approved probe says absent while an installer's record says installed, is a NOTE it could
not judge, and an entry the profile excludes or does not want, or whose `install.kind` is
`none`, is reported as not judged here. It also lists the uv tools, Claude Code plugins and
user skills installed on the machine that match no entry, or whose entry is at `observe` or
`discard`; that listing is a report only. The gate requires `own` and `adopt` entries whose
use a session transcript could see to declare how that use is recognised (see *Telemetry*
below).

**Every ring change is a `[[history]]` block**, oldest first, and the newest block's `ring`
must equal the entry's `ring`:

```toml
[[history]]
date = "2026-01-15"
ring = "pilot"
reason = "Makefiles used only as task runners keep appearing in projects that compile nothing"
evidence = "a trial run of `just --list` and `just test` in one of those projects"
```

The gate enforces the rule on every block, not only the newest:

- every block needs a `date` in `YYYY-MM-DD` form (a quoted string, or a TOML date) and a
  non-empty `reason`;
- a block that moves the entry to `adopt` or `pilot` also needs a non-empty `evidence` —
  a link, a run, a report, something a reader can check;
- no block is dated before the block above it, so the last block is the newest (two blocks
  on one day keep the order they are written in).

Each missing field, and each block out of order, is a schema error, so the gate FAILs.
`observe`, `discard` and `own` state a position or an authorship rather than a result, so
they need a reason and no evidence.

Two habits the history exists to make possible:

- Promoting an experiment needs a **measurement or evidence of real use**, never an
  impression. Demoting one needs the fact that caused it.
- An experiment states, **before** any data exists, what would end it. That is the
  `[pilot_exit]` block: how many days until review, what would adopt it, what would decline
  it. An entry at `pilot` without one is a FAIL, because whoever reads the numbers after
  choosing what to look for will find what they wanted. A demotion does not erase the block
  — letting an experiment come back with newer, slacker criteria would defeat writing them
  first.

## Axes — which problem it solves

`axis` is the one-word answer to "what is this for". The set is closed, because a free-text
field here becomes fifty near-synonyms and nothing can be grouped. The engine's default set
is small and general:

`build` · `lint-format` · `testing` · `agent-plugins` · `mcp-servers` · `other`

A catalogue that wants axes of its own declares its set in `radar.toml`, and that list
replaces the default wherever an axis is judged or shown — the gate's schema check,
`radar add --axis` and its `--help`, and the axes of the rendered radar page:

```toml
[entries]
axes = ["build", "lint-format", "testing", "agent-plugins", "mcp-servers", "docs", "other"]
```

An axis is a slug: lowercase letters and digits, in words joined by single hyphens. A
malformed list is a gate FAIL, and `radar add` refuses to write until it is fixed. Changing
the set is a change to the catalogue, so the gate then fails every entry whose axis left it.

An entry that fits none exactly takes the nearest, and `other` is there for one that fits
nothing. Under the default set `ruff` is `lint-format` and `just`, a command runner, is
`build`. An axis groups things that compete: two entries on the same axis at `adopt` is a
question worth answering.

## Artifact — what you install, and where it runs

`artifact` is the dimension an axis cannot carry, and the one that most often decides whether
you can have a tool at all.

| artifact | what it commits you to |
|---|---|
| `cc-plugin` | loads into the agent: skills, agents, commands, hooks |
| `mcp-server` | a tool surface **inside the agent's context** — it costs tokens every session |
| `cc-lsp` | a language server driven natively by the harness; no tool surface |
| `cli` | a command in a shell or in CI; no context cost, easy to audit |
| `python-lib` | imported by project code, so it becomes a runtime dependency |
| `service` | a server and a UI somebody has to operate, authenticate and patch |
| `standard` | nothing to install — a format to target |
| `corpus` | authored knowledge, not executable |
| `unknown` | never established; rejected on evidence before its nature mattered |

`ruff` and `just` are both `cli`, and that is most of why they are cheap: a CLI costs nothing
in an agent's context window and can be read by anyone auditing the machine. An `mcp-server`
on the same axis would be a different commitment entirely — same problem solved, tokens spent
on every session whether it is used or not, and a locked-down environment may simply refuse
it.

`unknown` is allowed only at `observe` and `discard`. It means: we did not establish what
this is, because it failed on evidence before that mattered.

## An entry, whole

Eight fields are required — `name`, `repo`, `axis`, `ring`, `license`, `visibility`, `note`,
`artifact` — plus at least one `[[history]]` block. The rest are opt-in.

```toml
name = "just"
repo = "https://github.com/casey/just"
axis = "build"
artifact = "cli"
ring = "observe"
license = "CC0-1.0"
visibility = "public"
note = "Language-agnostic command runner; a Make alternative that is only a task runner"
registry = "github:casey/just"

[[history]]
date = "2026-01-15"
ring = "observe"
reason = "Makefiles used only as task runners keep appearing in projects that compile nothing"
```

`name` is the name of the entry's file: an entry lives at `tools/<name>.toml` (or
`tools.local/<name>.toml`). So it is written as a file name is — lowercase letters, digits,
`.`, `_` and single hyphens, with no hyphen at either end (`MyTool` is entered as `mytool`,
`x--y` as `x-y`) — and no two entries claim one name, compared ignoring case. `radar add`
refuses a name in any other form, and `radar gate` FAILs an entry whose file is named
differently from it, or whose name another entry claims. The name is also written into
rendered pages, so it cannot carry a separator or markup, and it cannot be a device name
Windows reserves — `CON`, `PRN`, `AUX`, `NUL`, `COM0`–`COM9`, `LPT0`–`LPT9`, in any case and
with or without an extension (`aux.txt`) — because a file with that name cannot be checked
out on Windows.

`visibility` decides whether an entry appears in the projection `radar render --public`
writes — which is how a private catalogue can share its opinions about third-party tools
without publishing anything about itself. A public entry must give `repo` as a URL, because
the projection writes it as a link; a local path there would publish that path.

`registry` says where an entry's releases are read, and it is taken from the tool's own
repository metadata, never found by matching a name. Three kinds are accepted, and anything
else is a schema error:

| registry | releases | downloads |
|---|---|---|
| `pypi:<name>` | the newest release on PyPI | a monthly download count |
| `npm:<name>` (scoped names included) | the `latest` tag on npm | a monthly download count |
| `github:<owner>/<repo>` | the highest `X.Y.Z` or `vX.Y.Z` tag, pre-releases ignored, read with `git ls-remote` | none |

A `github:` registry publishes no download count, so the gate's download and astroturf
checks treat it as declaring no download registry rather than as a lookup that failed.
`radar versions --offline` skips the tag lookup.

`eval_status` records how an entry's value was established: `unmeasured` by default,
`eval-pending` while a measurement is scheduled but not yet made, or
`measured:<what was measured>`. It exists so that "we like it" and "we measured it" cannot be
written the same way.

`[install]` says how to converge the tool, and its `kind` — `uv-tool`, `npm-tool`,
`claude-plugin`, `mcp-plugin`, `mcp-server`, `pip`, `repo`, `manual` or `none` — decides what
`radar apply` may do. Everything except `manual` and `none` needs a `check` probe that is
idempotent and exits 0 when the tool is present, and everything except those two and `repo`
needs an `apply` command. `manual` prints its `instruction` and executes nothing, which is
the right answer for anything an environment's policy has to approve first. `check` and
`apply` are commands the catalogue asks the engine to run, so they run only once approved —
see *Commands from the catalogue* below.

`[version]` sets what "current enough" means for this entry: `latest` (warn when behind the
registry; needs `registry`), `pin` (fail on any deviation from `pinned` — for a tool whose
behaviour you measured at one version), `floor` (fail below `min`, silent above), or `any`
(never compared).

Two scopes are easy to confuse. The top-level `environments` says which environments a tool
*exists* in; `[install].environments` says where `radar apply` should *converge* it, and must
be a subset of the first. An entry scoped with `environments` must be
`visibility = "private"`.

## Environments — one profile per machine, and an overlay for what only it has

An environment profile, `environments/<ENV>.toml`, says what one kind of machine converges:
its `rings`, its `exclude` list, the `require_env` names it checks, its `[flags]`, and the
`[paths]` the verbs use — `claude_home`, `documents`, `feedback_root` and the rest. The
profile is tracked, so every machine that pulls the catalogue reads it, and a path under the
home directory is written with a leading `~` that every reader expands.

A path that is true of this workstation only belongs in the profile's **overlay**,
`environments/<ENV>.local.toml`, which is gitignored. The profile names it —
`overlay = "<ENV>.local.toml"` under `[flags]`, which every profile `radar init` and
`radar bootstrap` write already carries — and every verb that reads the profile layers the
overlay over it when the file exists, the gate's invariant scan and `radar bootstrap`
included.

An overlay may set six keys, and each is layered one way: `[paths]` and `[flags]` merge key
by key, `exclude` and `require_env` gain the overlay's items, and `description` and `rings`
replace the profile's. Its `name` is ignored. Any other key, and `overlay` itself (which
file is the overlay is the committed profile's to say), stops the verb with a `[FAIL]`
naming the file. `radar bootstrap` compares the profile with its overlay layered on against
the machine, and marks each path the overlay sets; `--write` changes only the overlay's
`[paths]`, adding what the machine disagrees on and dropping what the committed profile
already gets right, and keeps every other key (not its comments). A `.local.toml` the
profile does not name is not read. An overlay that is not valid TOML, that gives `[paths]`
or `[flags]` as something other than a table, a path as something other than a string,
`rings`, `exclude` or `require_env` as something other than a list of strings, or a key the
profile also sets as a value of another type, stops the verb with a `[FAIL]` naming the
file. A committed profile of the wrong shape stops it the same way, naming
`environments/<ENV>.toml`. `radar apply` and `radar reconcile` print the files they read,
the overlay marked `(overlay)`. A CI runner has no overlay, so the gate there reads the
committed profiles alone.

`--env` names the profile. Every verb that takes it accepts letters, digits, `.`, `_` and
`-`, starting with a letter or digit and not a reserved device name, the same rule as an
entry's `name`, and refuses anything else with exit 2 before it reads a file. `radar field`,
`radar reconcile`, `radar versions` and `radar sync-feedback` fall back to `radar.toml`'s
`default_environment` when `--env` is left out, or to `default` when the marker names none;
`radar apply`, `radar bootstrap` and `radar render-feedback-targets` always need it.

## Telemetry — evidence that costs nothing to collect

A ring claims observed state, so something has to be able to check the claim. Claude Code's
session transcripts already record every tool call, so `radar field` mines them **after the
fact**. There is no hook to install, no wrapper, no prompt: a tool you start watching today
already has a history, and watching one costs it nothing. The history is read on request:
a report's window starts at `[telemetry].since` — which `radar add` sets to the day it writes
the entry, unless `--telemetry-since` gives an earlier one — so `radar field <tool> --since
<an earlier date>` is what reads the use before it. A zero from a window that began today
says nothing about the months before.

An entry declares how its use is recognised:

```toml
[telemetry]
match_command = ["ruff"]      # globs matched against each command's resolved chain
since = "2026-01-15"
```

There are three matcher shapes — `match` for MCP tool names, `match_skill` for skill ids,
`match_command` for a CLI — and a parallel set (`opportunity_match`, and so on) pointing at a
different question: not "was it used" but "was there an occasion where it would have
applied". Without a denominator narrower than "every session on this machine", a usage count
means very little.

A `match_command` glob is matched against the **resolved chain** of each shell command, not
only its first word: `uv run ruff check .` resolves to `uv` and `ruff`, and
`python -m pre_commit run` to `python` and `pre_commit`. A literal hyphenated glob is also
matched in its module spelling, so `pre-commit` matches `pre_commit`. A matcher that matches
`python` itself is scanned in full, with a `[WARN]`: a chain can resolve through `python`
without the word appearing in the command line, so the fast byte prescan cannot be trusted
for it.

The `[telemetry]` key set is **closed**. A mistyped matcher name is not a TOML error; it is a
silent zero, and a zero reads as evidence. So the gate reports an unrecognised key as a WARN
rather than ignoring it.

Some artifacts leave no trace a session can see — a `python-lib`, a `cc-lsp`, a `standard`.
The gate asks for a matcher only on `own` and `adopt` entries whose artifact is `cli`,
`mcp-server`, `cc-plugin` or `corpus`, and even there `telemetry_absent = "<why>"` records a
deliberate absence, which the gate prints on every run.

### Cross-checking against an invocation table

`radar field --reconcile` compares this run's counts with a second table of the same calls,
derived independently from the same transcripts, and warns when the two differ by more than
2% on a matcher kind. Two parsers of one fact drift apart silently, and each looks right on
its own; the comparison is what shows it. Neither side is the source of truth: the report
prints both counts.

The table is read in *invocation table format, version 1*, specified below. It is an input
format, not a particular program: anything that writes these two files can be
cross-checked, and the profile names the directory that holds them as
`[paths].transcript_index`:

- `derived/invocations.jsonl` — one JSON object per line, one line per tool call:
  - `timestamp` — the call's time in ISO 8601, read as UTC when it carries no zone;
  - `tool` — the tool name, counted against the entry's `match` globs;
  - `exe` — a list of the executables the call's command ran, counted against
    `match_command`;
  - `skill` — the skill id of a Skill call, counted against `match_skill`;
  - `first_seen` — true on the first copy of a call. A row without it is a replayed copy
    and is skipped, because this run counts each call once.

  A line that is not JSON, or has no string `timestamp`, is skipped.
- `derived/derive.json` — `{"generated_at": "<ISO 8601>"}`, the time the table was derived.
  A call made after it cannot be in the table, so both sides are counted only up to that
  time, and the report prints the table's age. Without the file the whole window is
  compared. It may also carry `"format": "invocation-table/1"`, naming the version it was
  written in; the engine reads version 1, accepts the field without acting on it, and
  ignores any other key.

A table that records only the interpreter for `python tool.py` reads as drift on an entry
matched by its script name. That is a real disagreement, and the report keeps it.

## The gate

`radar gate` is what makes the rest of it more than a document. It exits 1 on any FAIL and 2
when there is no catalogue to judge. WARN, FLAG and NOTE lines are informational.

What it answers from the repository alone:

- **Schema** — every entry passes the schema above: required fields, the closed sets, the
  name rule, the registry form, the history rules, and the shape of every optional block,
  `[telemetry].since` a real day included. Any error is a FAIL.
- **Environments** — an entry scoped to an environment no profile defines is a FAIL, because
  it would be invisible everywhere.
- **Exit criteria** — an entry at `pilot` with no `[pilot_exit]` is a FAIL.
- **Telemetry** — an `own` or `adopt` entry of a measurable artifact kind with no matcher and
  no `telemetry_absent` is a FAIL; an unknown `[telemetry]` key is a WARN.
- **Licences** — copyleft and source-available terms in an entry's `license` are called out
  as NOTEs rather than buried.
- **Upstream health**, read from the newest file `radar snapshot` wrote under `snapshots/`:
  an archived repository is a FAIL at `own`, `adopt` or `pilot` and a NOTE elsewhere; a
  last push more than 180 days ago is a WARN at `adopt` or `pilot`. A repository lookup
  that answered 404 is a WARN at `own`, `adopt` or `pilot`, and any other failed lookup is
  a NOTE. When a lookup fails, the snapshot keeps the facts measured earlier, and the
  archived, last-push and lookup messages say when a fact was carried and from which date.
- **Unmeasured entries** — an entry with a github.com `repo` or a `pypi:` or `npm:`
  registry and no facts in the snapshot is a NOTE saying it is unmeasured, rather than
  silence that would read like a healthy upstream. With no snapshot at all, one NOTE counts
  the tracked entries and another the machine-local ones, which a snapshot never holds.
- **An astroturf signature** — more than 5,000 stars and monthly downloads below 5% of the
  star count is a FLAG, and so is a high star count with no download figure at all. Not proof
  of anything; a reason to look. Only an entry with a `pypi:` or `npm:` registry is judged.
- **Version sites** — the places the catalogue writes its own version agree (see
  `radar check-version-sites`). Any disagreement is a FAIL.

What needs a machine:

- **The scope-leak scan** — no file git tracks in the catalogue may carry, in its content or
  its name, the name of an entry kept in the gitignored `tools.local/`. The data root may be
  the top of a git worktree or a directory inside one.
- **The invariant scan** — the data plane does not reference the catalogue.
- **The publication check**, when the catalogue opts in.

Two habits run through all of it. The gate **names what it could not check** instead of
counting a skipped check as a pass: a scan that read nothing prints the same "0 references"
as a scan that read everything, so the distinction has to be in the output. And it reports
the size of what it read, so a green line can be argued with.

`radar gate --no-data-plane` answers the way a CI runner does — no `~/.claude`, no governed
worktree, no `tools.local/` — and says which checks that cost it. `radar gate --fast` caps
the invariant scan at 6,000 files per root and marks every root it cut with
`TRUNCATED (--fast)`.

## The invariant scan

What it reads:

- Under each profile's `claude_home`, an allow-list rather than the whole directory, because
  `~/.claude` also holds transcripts, caches and shell snapshots, which are logs rather than
  bindings. The engine always reads the generic Claude Code entries — `settings.json`,
  `CLAUDE.md`, `skills/`, `plugins/` — and the `feedback-targets.toml` that
  `radar render-feedback-targets` writes there.
- Anything else your stack keeps under `claude_home` and loads into a session, declared in
  `radar.toml`:

  ```toml
  [invariant]
  needles = ["my-radar"]
  claude_home_paths = ["agents", "hooks/pre-tool.sh"]
  ```

  Each entry is a sub-path relative to `claude_home`. An absolute path, or one that climbs
  out with `..`, is ignored with a NOTE. A declared path that does not exist on a machine is
  skipped there without a word, which is what lets you declare it before it exists.
- The local checkout of every `own` entry — its `[feedback].worktree` when it declares one,
  otherwise its `repo` — enumerated with `git ls-files`, so the checkout's own `.gitignore`
  decides what is out of scope. The checkout is named by an absolute path or a profile
  placeholder, and only the top level of a worktree, a directory that holds a `.git`, is
  enumerated. An entry whose only location is a URL, or a relative path, is listed as having
  no local tree to scan; a directory with no `.git` of its own, or one git cannot enumerate,
  is a FAIL that says which, not a skip.

Directories are walked for text files (`.md`, `.toml`, `.json`, `.py`, `.js`, `.ts`, `.sh`,
`.ps1`, `.txt`, `.yaml`, `.yml`), skipping `.git`, `.venv`, `node_modules`, `__pycache__`
and Claude Code's own log directories. The feedback inbox under `claude_home` is left out by
declaration, and the gate prints why on every run.

What it looks for:

- **`[invariant].needles`** — the names the catalogue goes by, matched as plain substrings.
  An empty list is legal and leaves only the path below, and the gate says so, because a
  scan on the path alone misses every file that refers to the catalogue by name.
- **The catalogue's own absolute path** — the data root and, when the data root is a linked
  git worktree, the main worktree — in each spelling a file can use: with forward slashes,
  in the platform's own spelling, and in that spelling as a JSON or TOML string escapes it,
  every backslash doubled, as `settings.json` writes a Windows path. On Windows the match
  ignores case, as the file system does. A path matches only where it ends at a path
  boundary, so a sibling directory that shares the prefix (`<path>-other`, `<path>.bak`) is
  not a reference.
- **The engine's own name**, only when the catalogue declares `[publish].framework_worktree`
  and an `own` entry's `repo` resolves to it, and only in `settings.json`, any
  `hooks/hooks.json` and `CLAUDE.md`: naming the engine elsewhere in the data plane is
  ordinary, while a hook bound to it would make a session depend on it.

A file that lists a needle among terms it *forbids* is asserting the catalogue's absence, not
depending on its presence, and the scan cannot tell the two apart. `[invariant.exempt]` names
such files one at a time, as a path suffix with a reason, and the gate prints each exemption
on every run.

When `claude_home` exists but none of the scanned entries do, the gate says
`none of the scanned files exist under <claude_home>` and lists what it looked for.

## The publication check

An opt-in check, for a catalogue whose operator also maintains a checkout meant for
publication — a published tool, a fork of the engine. Until `radar.toml` declares
`[publish].framework_worktree` the gate prints one line and nothing else:

```
[NOTE] publication check: off ([publish].framework_worktree is not declared)
```

The key follows the rule an `own` entry's checkout does: an absolute path or a placeholder,
naming the top level of a worktree. When it names a checkout (the profile's `[paths]`
placeholders are filled in), the gate lists that checkout's tracked files with
`git ls-files` and reads every one of them, names and content, against the list of what
must not travel:

- the catalogue's `[invariant].needles`;
- the catalogue's absolute path, in both spellings on Windows, and the main worktree's path
  when the catalogue is a linked worktree;
- the name of every entry in `tools.local/`;
- the name of every `own` entry, except the engine's own entry — the one whose `repo`
  resolves to the declared checkout;
- `[publish].forbid_extra`, the operator's own additions. `radar.toml` is tracked, so think
  before putting in it the very strings that most want to be forbidden.

It also applies two form rules to every tracked file:

1. **No home-finding outside `bootstrap.py`.** In a Python file, it flags `Path.home()`, and
   `Path.home` passed uncalled; `expanduser` on a `~` written in the source — a string that
   starts with `~`, an f-string or a `+` concatenation that starts with one, or a name bound
   to one in the same module, with or without an annotation; and reads of `HOME`,
   `USERPROFILE`, `HOMEDRIVE` or `HOMEPATH`, through `os.environ[...]`, `os.getenv` or
   `os.environ.get`, `setdefault` or `pop`. Import aliases are followed. `expanduser` on a
   path the operator typed is allowed. The rule reads one module's syntax, so a `getattr`
   lookup or a `~` that arrives from another module is left to review. A file that does not
   parse falls back to a text search for `Path.home()`.
2. **No `C:/Users/<account>` literal**, with either separator. The segment is accepted when
   it is a placeholder — `user`, `username`, `your-user`, `your-name`, `you`, `name`,
   `example`, or one of the forms `{...}`, `<...>`, `%VAR%` and `$VAR` — or one of Windows'
   own folders there, which no account is named after: `Public`, `Default`,
   `Default User` and `All Users`.

The list lives in the catalogue on purpose. A public repository that held the names it must
not publish would be publishing them, so the published checkout's own CI can only ever check
the forms, never the names.

## Linked worktrees

A catalogue can be checked out in more than one git worktree — a branch opened beside the
main checkout for review, say. Two gitignored files are machine data rather than branch
data, and a linked worktree does not have its own copy unless you make one: `tools.local/`
and `redact.local.toml`. When the data root is a linked worktree with no copy of its own,
the engine reads the main worktree's copy and prints one `[NOTE]` on stderr for each file it
took that way. A worktree of a bare repository has no main worktree, so nothing is read from
elsewhere. The environment overlays, `environments/<ENV>.local.toml`, are not shared this
way: every verb run in a worktree reads that worktree's own overlay, so a machine that needs
one there needs a copy in each worktree.

The gate treats the main worktree's path as the catalogue's own path too, both as an
invariant needle and as a forbidden term in the publication check. Approvals of catalogue
commands live in the git directory every worktree shares, so they are shared as well.

## Commands from the catalogue

A catalogue is a git repository that takes pull requests, and three of its fields are
commands: `[install].check` and `[install].apply` in a tool entry, and
`[feedback].index_builder`, a script the engine runs with its own interpreter. Whoever edits
one of those lines writes code that runs with the operator's account, so none of them runs
until the operator has approved that exact command.

- **Who asks.** `radar apply` runs check probes and, under `--apply --yes`, installs;
  `radar reconcile` runs check probes; `radar versions` runs an entry's check probe only
  when no installer's record gives the installed version, and does not ask for one at
  `[version]` policy `any`; `radar sync-feedback` runs the index builder. `radar gate` runs
  no catalogue command.
- **What an approval covers.** The entry, the field and the command text after the
  profile's `[paths]` placeholders are filled. The content of every file and directory its
  arguments name inside the git repository the data root sits in — a data root in a
  subdirectory of the repository included, so `../scripts/probe.js` is covered. The whole
  tree of the directory that holds each named file, because a program loads files from
  beside its script without being told to (`require('./helper')`, a shell `source` of a
  sibling). Every symbolic link inside the repository on the way to those paths or inside
  those trees, by its target text, and, when it resolves inside the repository, by what it
  leads to; a link cycle is walked once, and a link that a later `..` steps back out of
  (`lnk/../probe.py`) counts as on the way, since a POSIX system resolves `lnk/..` from the
  link's target. Links are followed one at a time, so a link reached only through another
  link's target counts too. An
  index builder's approval covers its path and content hash. A change to any of these makes
  the command pending again.
- **What it does not cover.** What a program reads on its own account from outside the
  repository — its configuration in the home directory, its installed modules. Inside the
  repository, what it finds by walking up from its script (a `node_modules` in an ancestor
  directory), a path it computes at run time, a path written inside a longer argument (a
  `sh -c` string, `-c` code) rather than as an argument of its own, and the files beside a
  script that sits directly in the data root, whose directory is the whole catalogue; the
  listing points out such a script, and an argument that holds both a space and a path
  separator. Put a script that loads helpers in a directory of its own, and name it by an
  argument of its own. The files beside an index builder are not covered either: they are
  off its import path, but one it loads by a path it computes is not part of its approval.
- **Where approvals live.** `<git common dir>/stack-radar/trusted-commands.json` — inside the
  catalogue's git directory, which git never tracks, so an approval cannot arrive with a pull
  request. Every worktree of the catalogue shares it. A data root outside git has nowhere to
  keep approvals and runs no catalogue command.
- **How to approve.** A pending command prints
  `[UNTRUSTED] <tool>: <field> not run - new or changed since it was last approved: <command>`
  and, beneath it, `listed as id <id>` with what the approval would cover. It is not run, is
  counted as unknown, and makes the command exit non-zero. Review what it would run, then
  re-run the same verb with `--trust-commands`: a command an earlier run of that verb
  listed, unchanged, is printed as `[TRUSTED] ...`, recorded, and run. The listing records
  the whole sha256 of everything the approval covers, and the flag compares that; the id is
  its first twelve characters, for reading. So a command no run has listed, or one that
  changed since its listing — a pull landed, a branch was switched — is listed again and
  not run, even with the flag.
  `radar apply --apply` without `--yes` lists the pending install commands the same way.
  Each verb keeps its own listing, and the flag approves only from it. A run of that verb
  that asks about any command, with the flag or without it, replaces the listing with what
  the run showed: a command an earlier run listed and the latest one did not show — a branch
  reviewed and dropped — is not approved later, even when another verb's listing still
  holds it.

**Where a command runs.** Not in the data root. Each catalogue command runs in a fresh,
empty scratch directory with `PYTHONSAFEPATH=1` and `PYTHONDONTWRITEBYTECODE=1` (so a script
that imports a helper from its own directory writes no `__pycache__` into what its approval
hashes), and its program is looked up on `PATH` only. Commands are split with POSIX
shell-word rules — quotes group an argument, a backslash escapes the next character, so
paths use forward slashes — and are run without a shell, so pipes, redirects and `&&` are
not available. The exception is a program that is a `.bat` or `.cmd` file on Windows (npm
and npx are), which Windows runs through cmd.exe: cmd.exe reads `&`, `|`, `%`, `"` and
similar characters inside an argument, or inside the program's own path, as its own syntax.
The file name is judged as Windows reads it, without trailing dots or spaces, so
`probe.cmd.` is a batch file too. Such a command runs only when every argument is made of
letters, digits, spaces and ``-_.,:;/\@+=~#*?[]{}'$` ``, and the program's path is held to
the same set less `,;=` or, when it holds a space and is passed in quotes, holds no `%` or
`!`; it is refused with a `[FAIL]` otherwise. The listing says when a program is a batch
file. A relative argument is passed as an absolute path when it names a file in the data
root, or is a relative path with a directory part that exists there; a bare name that is
only a directory there (such as `tools`) is left as it is.

In practice: write a probe as `python scripts/probe.py` or `just --justfile ./justfile check`.
Do not rely on `python -m <module-in-the-catalogue>`, on Python importing a module that sits
beside the script, or on a tool finding its configuration in the working directory: none of
those is reachable from the scratch directory.

## The feedback registry

An optional part, for catalogues whose `own` tools collect feedback reports — notes a
session writes about a tool you maintain. Nothing here is needed to use the rest of the
engine. Each verb first needs its path in the profile: `radar sync-feedback` needs
`[paths].feedback_root`, and `radar render-feedback-targets` needs
`[paths].feedback_targets` unless it prints with `--stdout`. Without it the verb prints a
`[FAIL]` naming the missing key and exits 1 having written nothing. A profile written on a
machine where discovery found no Claude Code home carries both keys commented out. With
the path set, on a catalogue where no entry declares `[feedback]`, `radar sync-feedback`
says so and does nothing, and `radar render-feedback-targets` says so and still writes a
registry that lists no tools, so a reader of that file finds an empty list rather than an
old one.

An `own` entry declares where its reports go:

```toml
name = "widget"
repo = "{documents}/widget"
axis = "agent-plugins"
artifact = "cc-plugin"
ring = "own"
license = "MIT"
visibility = "private"
note = "A plugin this catalogue's owner maintains"

[telemetry]
match_skill = ["widget:*"]
since = "2026-01-15"

[feedback]
dir = "feedback/widget"
format_doc = "docs/feedback-format.md"

[[history]]
date = "2026-01-15"
ring = "own"
reason = "Written and maintained here"
```

The `[telemetry]` block is not part of the registry. It is there because the gate FAILs a
measurable `own` or `adopt` entry with no matcher, and this example is a whole entry.

`[feedback]` is allowed only at `own`, because a report is only worth writing to someone who
can land the fix. `dir` is the archive directory inside the catalogue. `worktree` names the
local checkout, and is required when `repo` is a URL; `format_doc`, `triage_template` and
`extras` are passed through to whatever writes the reports; `index_builder` names a script
that regenerates the archive's `INDEX.md`.

- **`radar render-feedback-targets --env <ENV>`** writes a flat TOML file, the path in the
  profile's `[paths].feedback_targets`, listing each such tool with its checkout, its inbox
  directory under `[paths].feedback_root`, and the pass-through fields. The file is plain,
  absolute-pathed data with no timestamp and no path into the catalogue, so it keeps working
  if the catalogue disappears. It writes only to a destination named
  `feedback-targets.toml` inside the profile's `claude_home` or the data root; inside the
  data root the file must also sit outside `tools/`, `environments/`, `feedback/`,
  `tools.local/` and `.git/`; and an existing file there must start with the header this
  command writes. It creates each tool's inbox directory under `[paths].feedback_root`.
- **`radar sync-feedback --env <ENV>`** moves reports from the inbox into the catalogue's
  `feedback/<tool>/` archive, mapping the names of `tools.local/` entries and home
  directories out of them on the way in, mirrors the merged set back to the inbox, and runs
  the approved index builder whenever `INDEX.md` is missing or older than a report it lists.
  A report extended after it was ingested is an amendment, and the archive takes the longer
  copy; two different reports under one name are a conflict it refuses. A tool name that
  is not one plain path component, and a mapped report name that would land outside the
  archive, are refused with a `[FAIL]` and counted as refused, apart from the conflicts.

What reads `feedback-targets.toml` is up to you: whatever tool your sessions use to write
reports. The engine only writes it, and the file is optional.

## Where to go next

- [new-environment.md](new-environment.md) — bringing a catalogue up on a machine that has
  never run it, including the decisions no script can make for you.
- `radar <verb> --help` — every verb documents its own options.
