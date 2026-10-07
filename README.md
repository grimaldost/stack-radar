# stack-radar

A tech radar that governs a Claude Code stack — which plugins, skills, MCP servers and
command-line tools you have adopted, on what evidence, and what would make you drop one —
plus a gate that refuses the entries that only claim to be true.

This repository ships the `stack-radar` package, which installs one command, `radar`.

It is a **control plane**. It reads the stack, decides, converges it, and then gets out of
the way. The stack itself — `~/.claude`, its plugins and skills, the MCP servers you have
mounted, your own repositories — never learns this tool exists. Delete the catalogue and
nothing breaks: every install stays installed, and you lose the record of how it got there,
not the thing itself.

## Is this for you

**Yes, if** you run Claude Code seriously enough that the question "why is this plugin
installed, and what would make me remove it?" has stopped having an obvious answer; if you
work across more than one machine and they have drifted; or if you want a tool decision to
have to cite something.

**Partly, if** you want a tech radar for a stack that has nothing to do with Claude Code.
The catalogue format, `radar render` and `radar snapshot` are indifferent to what a tool is,
and so is the gate but for one rule: an `own` or `adopt` entry whose `artifact` is `cli`,
`mcp-server`, `cc-plugin` or `corpus` must say how a Claude Code session transcript shows
its use, or record `telemetry_absent = "<why>"`. A catalogue of CLIs and Python libraries
that is not about Claude Code records that absence on each adopted CLI, and otherwise works
as described. The default set of axes an entry files under is a small general one, and a
catalogue replaces it with its own in `radar.toml` (see *Axes* in docs/concepts.md). But the
verbs that look at the machine read or write under the profile's `claude_home`, which is
`~/.claude` on a machine `radar init` or `radar bootstrap` surveyed: `radar field` reads its
session transcripts, `radar reconcile` its plugin and skill records, `radar versions` its
plugin record, `radar bootstrap` surveys it, the gate's invariant scan reads the files a
session loads from it, and the two feedback verbs keep their registry and inbox under it by
default. Much of what makes this more than a spreadsheet is shaped around Claude Code.

**No, if** you want a dashboard. There is no server, no web app and no database: the
catalogue is a directory of TOML files in git, and every artefact is regenerated from them.

## What you get

- **A catalogue.** One TOML file per tool, in git, with a dated history of its ring
  changes. The gate fails a history block with no date or no reason, and a move to adopt
  or pilot with no evidence.
- **A gate.** Schema and history rules, licence call-outs (copyleft and
  source-available terms), upstream health from the latest snapshot (archived, stale,
  missing or not yet measured), pre-registered exit criteria for experiments, telemetry
  matchers, version-site agreement, and a scan showing that the governed stack still does
  not reference the catalogue.
- **Field telemetry with no instrumentation.** Claude Code's transcripts already record
  every tool call. `radar field` mines them after the fact, so a tool you started watching
  today still has a history: its window starts at the entry's `[telemetry].since`, which
  `radar add` sets to today, and `radar field <tool> --since <an earlier date>` reads the
  use before it.
- **Convergence.** `radar apply` plans first, installs only on request, and never executes
  a removal — it proposes them and leaves them to you.

See [docs/concepts.md](docs/concepts.md) for the vocabulary: rings, axes, artifact kinds,
the gate, and why the engine and the catalogue are separate things.

## Install

Requires Python 3.11 or newer, `git` and [uv](https://docs.astral.sh/uv/). The engine has
**no runtime dependencies** — everything it uses is in the standard library — so a command
you run inside a locked-down environment needs no installation policy decision first.

```
uv tool install git+https://github.com/grimaldost/stack-radar@v0.2.1
radar --version
```

The package is not on PyPI. It installs from this repository at a release tag, and the CI
workflow `radar init` generates installs it the same way (see *Continuous integration*
below). Where you may not install anything at all, a checkout is already a complete
installation:

```
PYTHONPATH=<checkout>/src python -m stack_radar.cli --help
```

## Quickstart

These are the steps `radar init` prints, in the order it prints them, with a real entry in
place of its placeholder: eleven commands, from nothing to a catalogue that renders. Run
them somewhere new — the catalogue is its own directory, separate from the engine and from
anything it governs.

```
radar init my-radar
cd "my-radar"
uv lock
git init
git add -A
git update-index --chmod=+x .githooks/pre-commit .githooks/commit-msg
git commit -m "chore: create the catalogue"
git config core.hooksPath .githooks
radar add ruff --repo https://github.com/astral-sh/ruff --axis lint-format --ring adopt --artifact cli --license MIT --visibility public --registry pypi:ruff --note "Linter and formatter for Python" --reason "Chosen as the linter and formatter for Python code" --evidence "ruff check and ruff format --check run in each project's CI" --telemetry-match-command ruff
radar gate
radar render
```

Each step is one command on one line, so the block runs as written in sh, PowerShell and
cmd. What the steps are for:

- `radar init` writes the marker (`radar.toml`); an environment profile,
  `environments/default.toml`, carrying the paths it **discovered on this machine** with
  the home directory written as `~`; a `tools/` holding only its README; a `CHANGELOG.md`
  and a `pyproject.toml`, which give the new catalogue its own version sites; the commit lane
  (`.githooks/` and `.pre-commit-config.yaml`); a `.gitattributes` that keeps line endings
  LF on every platform, so git runs the hooks as written; a CI workflow; and
  `.github/dependabot.yml`. It refuses a directory that already holds any of those files,
  names them and writes nothing; `--force` replaces them, but never an existing
  `radar.toml`.
- `uv lock` runs before the first commit because the generated CI checks `uv.lock` first.
- `git update-index --chmod=+x` runs before the commit too: git on Windows records the
  hooks without the executable bit, and git on Linux and macOS skips a hook that lacks it,
  printing only a hint that is easy to miss.
- `git config core.hooksPath .githooks` arms the commit lane once the catalogue exists.
  It points git at the tracked `.githooks/`, so those scripts run on every commit, and a
  hook that a checked-out branch adds there runs too. A pull request that changes
  `.githooks/`, `.pre-commit-config.yaml`, `uv.lock`, `pyproject.toml`, `uv.toml` or
  `.python-version` is a code change: review it like a change to an `[install]` command,
  before checking the branch out. The hooks run `uv run --frozen`, so a dependency that
  `pyproject.toml` declares and `uv.lock` does not carry is never installed. Once armed,
  the lane refuses a commit whose subject is not a Conventional Commit (`feat: ...`,
  `fix: ...`, `chore: ...`), which is why the first commit above is written that way.
- `radar add` validates the entry **before** the file lands. It refuses what the schema
  rejects and what the gate would fail on the next command — an adopt or pilot entry with
  no `--evidence`, an experiment with no pre-registered exit criteria, an adopted tool whose
  use a session could see and which declares no telemetry matcher.
- `radar gate` should report 0 FAIL, and `radar render` writes `README.md` and
  `docs/radar/index.html` into the catalogue.

`radar gate` reads the governed stack, so its run time grows with the size of `~/.claude`
and of the worktrees it scans. `radar gate --no-data-plane` answers the way a CI runner
does and skips that half — naming what it skipped, rather than reporting a scan of nothing
as a pass.

## The evidence rule

Every entry carries at least one `[[history]]` block, oldest first, and the newest block's
`ring` must equal the entry's `ring`. The gate checks every block, not only the newest:

- every block needs a `date` in `YYYY-MM-DD` form (a quoted string, or a TOML date) and a
  non-empty `reason`;
- a block that moves the entry to `adopt` or `pilot` also needs a non-empty `evidence` — a
  link, a run, a report, something a reader can check;
- no block is dated before the block above it, so the last block is the newest.

Each missing field, and each block out of order, is a schema error, so the gate FAILs and
exits 1. `observe`, `discard` and `own` need a reason and no evidence: they state a position
or an authorship, not a result.

## Commands from the catalogue run only once you approve them

A few catalogue fields are commands: `[install].check` and `[install].apply` in a tool
entry, and `[feedback].index_builder`. A catalogue takes pull requests, so the engine runs
none of them until the operator has approved that exact command. When `radar apply`,
`radar versions`, `radar reconcile` or `radar sync-feedback` needs a command that is not
approved yet, it prints it as `[UNTRUSTED]`, does not run it, counts what it would have
observed as unknown, and exits non-zero. `radar versions` needs an entry's check probe only
when no installer's record gives the installed version, and does not ask for one at all for
an entry at `[version]` policy `any`, whose version it never compares: it shows the version
an installer's record or an already approved probe gives, and otherwise `?`, with the probe
marked as not needed. Review the commands, then re-run the same verb with
`--trust-commands` to approve, record and run
them. Each `[UNTRUSTED]` command is listed with a short id, and the flag approves only a
command that verb listed with the same id, so one that arrives or changes between the two
runs is listed again rather than run. Each verb keeps its own listing: a run without the
flag replaces what the same verb listed before, and the flag approves only from that verb's
listing, so it never approves a command that verb's latest run stopped showing, whatever
another verb listed.

Approvals live in `<git common dir>/stack-radar/trusted-commands.json` — inside the
catalogue's git directory, which git never tracks and which every worktree of the catalogue
shares. An approval covers the command text after the profile's `[paths]` placeholders are
filled, the content of every file its arguments name inside the repository, and the whole
directory that holds each such file, so editing any of them makes the command pending again.
`radar gate` runs no catalogue command. See [SECURITY.md](SECURITY.md) for the whole trust
model, including what an approval does not cover.

## The verbs

`radar <verb> --help` prints a verb's own options.

| verb | what it does |
|---|---|
| `radar init` | create a catalogue: the marker, a profile, the commit lane and CI |
| `radar add` | write one validated entry, with its first dated history block |
| `radar gate` | judge the catalogue and the governed stack; exit 1 on any FAIL |
| `radar render` | write the catalogue's artefacts from its entries |
| `radar apply` | converge a machine towards what an environment declares |
| `radar field` | field telemetry for an entry, from session transcripts |
| `radar reconcile` | compare what is installed against what the catalogue claims |
| `radar snapshot` | record today's upstream licence, activity and download figures |
| `radar versions` | compare installed versions against what upstream publishes |
| `radar sync-feedback` | move feedback reports from the inbox into the catalogue and index them |
| `radar render-feedback-targets` | write the feedback-target registry into the stack |
| `radar redact-backfill` | map private tool names and the home directory out of tracked content |
| `radar bootstrap` | survey a machine and propose an environment profile |
| `radar changelog-gate` | fail a change that moves a repository's code unrecorded |
| `radar check-version-sites` | check that the version sites of a repository agree |

The last two judge whatever repository they are run from rather than a catalogue; this
repository's own CI runs both on itself. Everything else needs a data root: the engine
finds one by walking up from the working directory until it meets a `radar.toml`, the way
`git` finds `.git`. `--data-root` and `RADAR_DATA_ROOT` name one explicitly, for CI.

The two feedback verbs are optional. They matter only to a catalogue whose own tools
declare a `[feedback]` block; see *The feedback registry* in
[docs/concepts.md](docs/concepts.md).

## Continuous integration

The workflow `radar init` writes runs `uv lock --check`, `uv sync`, ruff, `radar gate` and
`radar check-version-sites` on Linux and Windows, and audits the workflows with zizmor. It
runs the engine with
`uvx --from "git+https://github.com/grimaldost/stack-radar@<commit>" radar <verb>`, pinned
to the commit of the release that created the catalogue, with the release tag in a comment
beside each step: a tag can be moved to other code, a commit cannot. The engine knows that
commit when it was installed from this repository with git, as the install line above does.
An engine installed another way writes `@v<version>` instead, and the generated comment
says how to look the tag's commit up and pin it. To run another copy — a fork, a mirror, an
index you control — change the `--from` value on each of those steps, and keep it pinned to
a commit: a bare package name resolves to whatever is published under that name when the
job runs.

## Documents

- [docs/concepts.md](docs/concepts.md) — rings, axes, artifact kinds, the gate, the trust
  model and the split between engine and catalogue.
- [docs/new-environment.md](docs/new-environment.md) — bringing a catalogue up on a machine
  that has never run it.
- [CONTRIBUTING.md](CONTRIBUTING.md) — the checks a change is held to, and how a release is
  made.
- [SECURITY.md](SECURITY.md) — supported versions, reporting a vulnerability, and the trust
  model.
- [CHANGELOG.md](CHANGELOG.md) — what changed in each release.

## Licence

[MIT](LICENSE).
