# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release heading is `## [X.Y.Z] - YYYY-MM-DD`. That grammar is not cosmetic: the newest
released heading is one of the version sites `radar check-version-sites` compares with
`pyproject.toml`, so a heading written some other way is a version site that has stopped
existing. Changes marked **(consumer-affecting)** alter what a reader of the entry schema,
the field report or the gate's exit code sees.

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
