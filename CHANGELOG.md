# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release heading is `## [X.Y.Z] - YYYY-MM-DD`. That grammar is not cosmetic: the newest
released heading is one of the version sites `radar check-version-sites` compares with
`pyproject.toml`, so a heading written some other way is a version site that has stopped
existing. Changes marked **(consumer-affecting)** alter what a reader of the entry schema,
the field report or the gate's exit code sees.

## [Unreleased]

### Changed

- The development lock takes ty 0.0.90, and the `setup-uv` pin moves from v10.3.0 to v10.3.1 in
  CI and in the workflow `radar init` writes.

## [0.4.1] - 2026-11-01

Released as a patch rather than a minor version, because it adds nothing a reader has to learn:
no verb, no key and no format, only one directory the gate stops reading.

### Fixed

- **The invariant scan of `radar gate` leaves `<claude_home>/plugins/data` out.** It is the
  directory Claude Code gives each plugin for its runtime state, and a hook's per-session file
  there can name the catalogue for an ordinary reason. Such a file failed the gate on every new
  session, and its name carries the session id, so an `[invariant.exempt]` row could not name it.
  The directory is now a declared omission, printed with its reason on every run that scans, and
  its files no longer count among the eligible files of the `plugins` root. Only that directory
  is out: a directory named `data` anywhere else is still read, and so is a path under it declared
  in `[invariant].claude_home_paths`. (consumer-affecting)

## [0.4.0] - 2026-11-01

### Added

- **`radar discovery`, a report of what the sessions in a window were shown and what they
  reached for.** Every other verb measures use; this one measures exposure: the skill listing
  and the entries it rendered name-only (grouped by plugin prefix, a prefix marked own when an
  `own` entry's `match_skill` holds `<prefix>:*`), the deferred tools announced, ToolSearch
  calls by kind on the main thread and in subagents with the names selected, the skills a
  prompt-time hook suggested (a router) and how many were followed by a load, the lines in
  which the agent named the skills it chose (dispatch lines), the skills loaded and the
  compactions. A session counts when its rows in the window sum to at least one user
  prompt and one assistant message, and every figure is written as "X of Y" with its
  denominator named. It reads one source, a table in *discovery table format, version 1*,
  which `docs/concepts.md` now specifies, at a new profile key, `[paths].discovery_index`,
  and checks `derive.json`'s `discovery_format` before reading a row. A missing key, table,
  `derive.json`, format or `records_end`, and a window with no rows, each print one `[WARN]`
  naming which and exit 1; days after the table's records end or before its first row print
  as unmeasured, never as zero, and the records-end day is flagged partial. Markdown by
  default, one `--json` document with `schema_version` 1 otherwise. It writes nothing, names
  no project, slug or path, and prints every name through the redaction `radar field` writes
  with, a name a `tools.local/` entry's matcher claims printed as its `redact_as`; a claim on
  one skill, or a `match` glob that begins `mcp__` and names one tool, covers its whole
  plugin prefix or server, and a plugin is one
  namespace across its skills and the tools of its MCP servers (named
  `plugin_<prefix>_<server>`), so a claim on either side hides both. Released as
  part of a minor version and not a patch, because it adds a verb and a profile key a reader
  can see and use; nothing existing is changed or removed, so no major is called for. A new
  module: `radar field` and its report document, the entry schema and the gate's exit code are
  untouched, so the entry is not consumer-affecting.

### Changed

- **Development dependencies and the setup-uv action move to releases that follow the 7-day
  cooldown.** The dev lock takes `pre-commit` 4.7.0, `ty` 0.0.89, `ruff` 0.17.1, `virtualenv`
  21.14.13 and `filelock` 4.2.4, and the engine's workflow pins `astral-sh/setup-uv` at v10.3.0.
  Each but `virtualenv` is the newest release published at least seven days before the change,
  as `.github/dependabot.yml` requires, so `ty` and `ruff` stay behind their newest releases.
  `virtualenv` 21.14.13 is taken inside the cooldown on purpose: it is the first release that
  fixes GHSA-cx6f-vjhg-g3cc, an unescaped `--prompt` value in the Bash activator that affects
  21.14.9 up to 21.14.12. The `ci.yml` template that `radar init` writes takes the same
  setup-uv pin, because a test holds it equal to the engine's own, so a catalogue created from
  now on starts on v10.3.0. No code changes.

## [0.3.0] - 2026-10-24

Released as a minor version rather than a patch, because it adds what a reader can see:
`radar economics`, a new verb, and a way for `radar field` to identify a script a POSIX
shell ran, which can move a count up. The two fixes are corrections, but the second one adds a
`was_pilot` boolean to the field report document. Nothing is removed or renamed and the
gate's exit code does not move, so no major version is called for, and a minor is where a
feature arrives. One operational cost follows from choosing a minor: `radar init` writes
`requires_framework` as the range `>=X.Y.Z,<X.(Y+1).0`, so a catalogue created by 0.2.x
carries the ceiling `<0.3.0` and refuses this engine until its marker has that ceiling
raised.

### Added

- **`radar field` identifies a script run by a POSIX shell.** A `match_command` glob now
  matches the file name of the script operand of `bash`, `sh`, `zsh`, `dash` or `ksh`, as it
  already did for a `.py` file run by the python interpreter: `bash /opt/tools/runner -- uv run
  pytest` counts as `runner`, so a CLI shipped as an extensionless shell script can be measured
  instead of declared `telemetry_absent`. Options before the script are skipped; `-c` and `-s`
  leave the body opaque, and the arguments after the script are not read. A count can only move
  up, and only for an entry whose glob names a script that a shell ran. (consumer-affecting)
- **`radar economics`, a descriptive view of the token economics of the sessions in a
  window.** It reads the `message.usage` block of every assistant message in the window's
  transcripts, which no other verb read, and prints totals by class (output, thinking,
  uncached input, cache read), the main session against sidechains, the split per model
  (attributed per message), cache creation by time to live (5 minutes, 1 hour,
  unattributed), cache re-creation events (a turn whose cache creation exceeds half the
  previous turn's context) with their gap to the previous turn, and per transcript the
  turns, cold-start context, final context and growth per turn. Streamed duplicates are
  merged by `message.id`; a field a record does not carry is counted as absent, not zero.
  Markdown by default, one `--json` document with `schema_version` 1 otherwise. It writes
  nothing, names no project, path or message content, and a window with no usage at all
  prints a `[WARN]` line and exits 1. Descriptive, not causal; tokens, not prices. A new
  module: `radar field` and its report document are unchanged.

### Fixed

- **`ty` 0.0.85 passes.** `trust.scratch_dir`, a `@contextmanager` function, is annotated
  `-> Generator[Path, None, None]` instead of `-> Iterator[Path]`, the form `ty` 0.0.85 reports
  as a deprecated overload. The annotation is the whole change: no behaviour moves. The dev
  dependency lock takes `ty` 0.0.85 with it.
- **`radar field` no longer calls a `[pilot_exit]` block "retained from an earlier pilot" on an
  entry that never was a pilot.** The schema allows the block at `ring = "own"` (authorship is
  not an exemption from evidence), and the report's finding and Markdown section described it
  as the leftover of an experiment that ended. They now say it is recorded and not evaluated
  at that ring, and are evaluated only at `ring = "pilot"`; an entry whose `[[history]]` shows
  a stay at `pilot` keeps the old wording. The report document gains a `was_pilot` boolean
  beside `pilot_exit_evaluated`. Nothing is evaluated differently: the same blocks are
  evaluated and the same checks run. (consumer-affecting)

## [0.2.1] - 2026-10-07

Versions before 0.2.1 were developed privately and their history is not published.

### Added

- **A catalogue format.** One TOML entry per tool in `tools/`, machine-local entries in the
  gitignored `tools.local/`, and a `radar.toml` marker whose `requires_framework` range
  every verb that reads the catalogue checks. A `name` is its file's name
  (`tools/<name>.toml`): lowercase letters, digits, `.`, `_` and single hyphens, claimed by
  no other entry and no device name Windows reserves, and a machine-local entry's
  `redact_as` placeholder is a plain name of the same characters that contains none of the
  names it replaces; an `axis` is one of the marker's `[entries].axes`, or of the default
  set; a `registry` is `pypi:<name>`, `npm:<name>` or `github:<owner>/<repo>`. `radar init`
  creates a catalogue with its commit lane, `.gitattributes`, CI workflow and Dependabot
  configuration, in a directory that holds none of those files unless `--force` is given,
  and `radar add` writes one entry, validated before it lands, to a file named as the entry
  is. (consumer-affecting)
- **The evidence rule.** Every `[[history]]` block needs a `YYYY-MM-DD` `date` and a
  `reason`, and a block that moves an entry to `adopt` or `pilot` also needs `evidence`.
  Blocks run oldest first, and the last one's `ring` is the entry's. (consumer-affecting)
- **The gate.** `radar gate` exits 1 on any FAIL and 2 when it finds no catalogue or its
  `radar.toml` does not parse. It checks the schema and history rules, environment scoping,
  `[pilot_exit]` on every pilot, telemetry matchers (each glob non-empty), licence
  call-outs, upstream health from the newest snapshot, the astroturf signature and
  version-site agreement, and names every check it could not run. (consumer-affecting)
- **The invariant scan** FAILs a file under a profile's `claude_home`, or in an `own`
  entry's checkout, that names the catalogue by a declared needle or its absolute path. **The
  publication check**, opt-in through `[publish].framework_worktree`, reads a declared
  checkout's tracked files for the catalogue's names and paths.
- **Environment profiles and overlays.** `environments/<ENV>.toml` declares a machine's
  rings, flags and `[paths]`, and every verb that reads it layers the gitignored overlay it
  names, which may set `description`, `rings`, `exclude`, `require_env`, `[paths]` and
  `[flags]` and nothing else. `radar bootstrap` compares the profile, overlay layered on,
  with the machine, names a declared path that does not exist here rather than reporting a
  match, and writes a starter profile or the overlay's `[paths]`, keeping every other key an
  existing overlay sets.
- **Convergence and comparison.** `radar apply` plans, installs only under `--apply --yes`,
  never removes anything, and exits 0 once every install it ran succeeded and nothing else
  needs action. `radar reconcile` compares rings with what the machine shows, not counting a
  catalogue's own `pyproject.toml` as a project, and lists what is installed without an
  entry. `radar versions` compares installed versions with PyPI, npm or git tags.
- **Telemetry verbs.** `radar field` counts an entry's use and its opportunities from Claude
  Code session transcripts and writes a JSON and a Markdown report; for a machine-local entry
  the report directory carries the entry's `redact_as` placeholder, not its name, so the
  tracked path names nobody. `radar snapshot` records upstream licence, activity and download
  figures. (consumer-affecting)
- **The other verbs.** `radar render` writes the catalogue's README and HTML page, and with
  `--public` the public projection; `radar sync-feedback` and `radar render-feedback-targets`
  keep a feedback archive and its registry; `radar redact-backfill` maps machine-local names,
  in every component of a path as well as in content, and the home directory out of tracked
  files; `radar check-version-sites` and `radar changelog-gate` judge the repository they are
  run in.

### Security

- A catalogue command (`[install].check`, `[install].apply`, `[feedback].index_builder`)
  runs only once the operator approves it. An approval covers its filled text, every file it
  names inside the repository with the directory that holds it, and the links on the way,
  and is kept in `<git common dir>/stack-radar/trusted-commands.json`, which git never
  tracks. A pending command is listed as `[UNTRUSTED]` with a short id; `--trust-commands`
  approves only a command whose whole digest a run of the same verb listed, and each run of
  a verb, with the flag or without it, replaces that verb's listing. A path written inside a
  longer argument is not hashed; the listing notes an argument that holds both a space and a
  path separator, and a script that sits directly in the data root. An approved command runs
  in an empty scratch directory with `PYTHONSAFEPATH=1` and `PYTHONDONTWRITEBYTECODE=1`, and
  on Windows one whose program is a `.bat` or `.cmd` file runs only when neither its
  arguments nor the program's path hold a character cmd.exe reads as syntax. `radar gate`
  runs no catalogue command.
- `git`, `gh` and `uv` run by the absolute path found on `PATH`, never from the working
  directory, and a `.bat`/`.cmd` one is held to the same no-cmd-syntax rule as an approved
  command. Every `git` the engine runs on a tree a catalogue, profile or data root chooses
  runs with `safe.bareRepository=explicit`, an empty `core.fsmonitor` and `core.sshCommand`,
  and hooks pointed at an empty path, so a bare repository committed in a catalogue is never
  adopted and no fsmonitor or hook program runs. A checkout an entry or the marker names is
  enumerated only when it holds its own `.git`, and a relative `repo`, `[feedback].worktree`
  or `[publish].framework_worktree` is refused rather than enumerated; the data root may sit
  anywhere inside a worktree. No file is written or moved into the data root through a
  symbolic link, and `radar sync-feedback` refuses a tool whose archive directory is one.
  `radar render-feedback-targets` writes no file but one named `feedback-targets.toml`.
- The CI workflow `radar init` writes pins the engine to the commit it was installed from,
  naming the release tag beside each step only when that install asked for the tag. Without
  such a commit it pins the release tag, with a comment saying how to pin the commit. The
  engine's build backend is pinned (its own transitive dependencies resolve from the index
  unpinned), each `setup-uv` step pins the `uv` version it installs, and the generated
  Dependabot configuration proposes updates to the pinned actions and to `uv.lock`.
- The commit-lane hooks `radar init` writes, and this repository's own, run
  `uv run --frozen`, which installs the project's dependencies from `uv.lock` alone: a
  dependency that `pyproject.toml` declares and `uv.lock` does not carry is never resolved
  or installed at commit time. A project that is itself built, as this repository is, is
  still rebuilt when `pyproject.toml` changes, and the rebuild installs the
  `[build-system]` requirements and runs the build backend that `pyproject.toml` names;
  the catalogue `radar init` writes sets `package = false` and is never built. The files to
  review before checking a branch out are `.githooks/`, `.pre-commit-config.yaml`,
  `uv.lock`, `pyproject.toml`, `uv.toml` and `.python-version`.
- `radar render` escapes entry names, notes and the catalogue title in the HTML page and the
  Markdown tables, link and image syntax included, and links a `repo` only when it is an
  http(s) URL. `radar snapshot` never writes a `tools.local/` entry into `snapshots/`.
