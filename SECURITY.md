# Security

## Supported versions

| version | supported |
|---|---|
| 0.2.x | yes |
| older | no |

Fixes land in the newest 0.2.x release. There are no backports to older lines.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting on this repository:
the **Security** tab, then **Report a vulnerability**. Please do not open a public issue
for it. If that button is not offered, open an issue that asks for a private contact and
says nothing about the problem itself.

Answers are best effort, and there is no promised response time.

## Trust model

- **A catalogue is trusted input.** It is a git repository you maintain, and the engine
  reads it as data: entries, profiles and the marker.
- **The commands a catalogue declares run only after you approve them.** Three fields are
  commands: `[install].check` and `[install].apply` in a tool entry, and
  `[feedback].index_builder`. `radar apply`, `radar versions`, `radar reconcile` and
  `radar sync-feedback` run one only once the operator has approved that exact command;
  `radar versions` needs a check probe only when no installer's record gives the
  installed version. A pending command is printed as `[UNTRUSTED]` with a short id, is
  not run, and makes the command exit non-zero.
- **An approval covers what the command runs from the repository.** It covers the command
  text after the profile's `[paths]` placeholders are filled; the content of every file
  and directory its arguments name inside the git repository the data root sits in,
  including a path such as `../scripts/probe.js` from a data root in a subdirectory; the
  whole tree of the directory that holds each named file, so a helper a script loads from
  beside itself is covered; and every symbolic link inside the repository on the way to
  those paths or inside those trees, by its target text and, when it resolves inside the
  repository, by what it leads to. The way to a path is walked as the operating system
  resolves it, one link at a time, so a link that a later `..` steps back out of
  (`lnk/../probe.py`) is covered too, and so is a link reached only through another
  link's target. A change to any of these makes the command pending again. A link
  outside the repository belongs to the machine and is not covered.
- **`--trust-commands` approves only what a run of the same verb listed.** It approves a
  pending command only when an earlier run of that verb printed it as `[UNTRUSTED]` (or
  `radar apply --apply` listed it as not yet approved) with the same sha256 of everything
  the approval covers. The listing records that digest whole; the id it prints is its first
  twelve characters, for reading, and is never what the flag compares. A command that
  arrives or changes between the listing and the approving run - a `git pull`, a branch
  switch - is listed again, not run. Each verb keeps its own listing, every run of that verb
  that asks about a command replaces it with what the run showed, with the flag or without
  it, and the flag approves only from it, so a command the verb no longer shows - a branch
  reviewed and dropped - is not approved later, once that verb has run again. A listing does
  not otherwise expire: a command listed and decided against stays approvable until the
  next run of that verb.
- **Approvals live in the catalogue's git directory**, in
  `<git common dir>/stack-radar/trusted-commands.json`, with the digests listed and not
  yet approved. Git never tracks that directory, so an approval cannot arrive with a pull
  request. A data root outside a git repository has nowhere to keep approvals, and runs
  no catalogue command.
- **An approved command runs in an empty scratch directory**, not in the data root, with
  `PYTHONSAFEPATH=1` and `PYTHONDONTWRITEBYTECODE=1`, and the program is looked up on
  `PATH` only, so a file added to the catalogue cannot stand in for the program an approved
  command names.
- **No shell runs a command, except cmd.exe for a batch file.** On Windows a program that is
  a `.bat` or `.cmd` file, as npm and npx are, is run by cmd.exe, which reads `&`, `|`, `<`,
  `>`, `^`, `%`, `!` and `"` inside an argument as its own syntax whatever the quoting, and
  reads the program's own path too. The file name is judged as Windows reads it, without
  trailing dots or spaces, so `probe.cmd.` is a batch file too. Such a command runs only
  when every argument is made of letters, digits, spaces and ``-_.,:;/\@+=~#*?[]{}'$` ``,
  and the program's path is held to the same set less `,;=` or, when it holds a space and is
  passed in quotes, holds no `%` or `!`. Any other is refused with a `[FAIL]`, approved or
  not, and the listing names a program that is a batch file.
- **What an approval does not cover.** What a program reads on its own account from
  outside the repository, such as its configuration in your home directory or the modules
  installed in its environment. Inside the repository: what a program finds by walking up
  from its script, such as a `node_modules` in an ancestor directory; a path it computes
  at run time; a path written inside a longer argument, such as a `sh -c` string or `-c`
  code, rather than as an argument of its own; and the files beside a script that sits
  directly in the data root. The listing points out such a script, and an argument that
  holds both a space and a path separator. A script that loads helpers belongs in a
  directory of its own, which the approval then hashes whole, and is named by an argument
  of its own. An index builder's approval covers its path and its content alone, not the
  files beside it: they are not on its import path, but one it loads by a path it
  computes can change without making it pending again.
- **Tracked hooks run what the repository holds.** `radar init` tells the operator to
  run `git config core.hooksPath .githooks`, which points git at a tracked directory. Its
  scripts run on every commit, through `uv run --frozen` and `.pre-commit-config.yaml`,
  and a hook that a checked-out branch adds there runs on the next checkout or commit - no
  approval is asked for. `--frozen` makes uv install the project's dependencies from
  `uv.lock` alone, so a dependency that `pyproject.toml` declares and `uv.lock` does not
  carry is never resolved or installed at commit time. uv still reads `pyproject.toml`,
  `uv.toml` and `.python-version`, and a project that is itself built is rebuilt when
  `pyproject.toml` changes: the rebuild installs the `[build-system]` requirements and runs
  the build backend that `pyproject.toml` names, neither of which `uv.lock` records. This
  repository is built; the catalogue `radar init` writes sets `package = false` and is
  not. So a pull request that changes `.githooks/`, `.pre-commit-config.yaml`, `uv.lock`,
  `pyproject.toml`, `uv.toml` or `.python-version` is a code change: review it as you
  would a change to an `[install]` command, before checking the branch out, or unset
  `core.hooksPath` while reviewing it.
  The same holds for this repository's own `.githooks/`.
- **`radar gate` runs no catalogue command, and the `git` it runs executes no
  configuration a catalogue carries.** The gate reads files, and runs `git ls-files` on the
  data root, on each `own` entry's checkout and on `[publish].framework_worktree`. A pull
  request can commit a directory laid out as a bare git repository, and a nested `.git` can
  exist on disk (an untracked clone, say); the `config` of either can name a program in a
  key git runs, such as `core.fsmonitor`, and git pointed at it would run that program. So
  every `git` the engine runs on a tree a catalogue, a profile or the data root chose
  carries one shared set of settings, placed before the subcommand where the repository's
  own configuration cannot override them: `safe.bareRepository=explicit` (a bare layout is
  not discovered as a repository), `core.fsmonitor=`, `core.hooksPath` pointed at an empty
  path (no hook runs), and `core.sshCommand=`; the changelog gate's one content diff adds
  `--no-ext-diff --no-textconv`. The `git mv` of `radar redact-backfill` can still run a
  clean filter the tree's `.gitattributes` selects, but only one that git configuration
  outside the catalogue defines. A checkout an entry or the marker names is enumerated only
  when it holds its own `.git`, and one written as a relative path is refused wherever it
  points: name it by an absolute path or a profile placeholder. The data root may be the
  top of a worktree or a directory inside one. This covers `radar gate`,
  `radar redact-backfill` and `radar changelog-gate`, the verbs run on an unreviewed
  branch, and the `git` of `radar versions`.
- **The programs the engine runs itself are found on `PATH` only.** `git`, `gh`, `uv` and
  the other programs the engine runs by name are looked up in the absolute directories on
  `PATH` and run by that absolute path, never from the working directory, so a `git.exe`
  committed to a catalogue does not run in git's place. One that resolves to a `.bat` or
  `.cmd` file, which Windows runs through cmd.exe, is run only when its path and arguments
  hold nothing cmd.exe reads as syntax - the rule an approved catalogue command is held to
  - so a directory name a catalogue chooses cannot inject a command through it.
- **No file is written through a symbolic link inside the data root.** Every file the
  engine writes there - `README.md`, `PUBLIC.md`, `docs/radar/*`, `snapshots/*`, field
  reports, archived feedback, profiles, and the files `radar init` and `radar add` create -
  is refused with a `[FAIL]` when it is a symbolic link (or a Windows junction), when a
  directory between the data root and it is one, or when it resolves outside the data
  root, and the run writes nothing. A write replaces the file rather than writing through
  it. `radar sync-feedback` does not mirror an archived report that is a link or resolves
  outside the archive, reads and indexes nothing for a tool whose `feedback/` or
  `feedback/<tool>` directory is a link, and `radar redact-backfill` neither reads nor
  rewrites a tracked link, nor renames a file to a path that a link lies on.
- **`radar render-feedback-targets` writes only where it is allowed to.** The destination
  must be named `feedback-targets.toml` and sit inside the profile's `claude_home` or the
  data root; inside the data root it must also sit outside `tools/`, `environments/`,
  `feedback/`, `tools.local/` and `.git/`; and an existing file there must start with the
  header the command writes. It also creates one inbox directory per tool under the
  profile's `[paths].feedback_root`, each named after its tool and refused unless the name
  is one plain path component that stays under `feedback_root`. Each rule is applied both
  to the path as written and to where it resolves, a destination that is a symbolic link
  is refused, and one written inside the data root follows the rule above. `claude_home`
  is profile data, so that bound catches mistakes rather than a hostile profile; the name
  and header rules are what stop it writing any other file.
- **Archived feedback reaches what sessions read.** `radar sync-feedback` copies each
  report in the tracked archive, `feedback/<tool>/`, that this machine's inbox under
  `[paths].feedback_root` lacks, or whose inbox copy the archived one extends, into that
  inbox, and copies the archive's `INDEX.md` there once it is regenerated. The inbox is
  where sessions write reports and read earlier ones. A pull request can add a report to
  the archive or extend one already there, and once the branch is merged, or the verb runs
  on a checkout of it, that text is in the inbox for an agent to read. So an archived
  report is untrusted input: review it like any other catalogue change, as text an agent
  will act on. `radar render-feedback-targets` likewise writes each
  `[feedback].extras` string, quoted but otherwise as it stands, into the registry it
  renders, which the tooling that writes reports reads.
- **The rendered pages escape catalogue text.** `radar render` escapes entry names, notes
  and the catalogue title in the HTML page and in the Markdown tables: HTML, table syntax
  and the brackets of Markdown link and image syntax, so catalogue text cannot inject
  markup, script, a link or an image. A bare `http(s)` URL in a note is left as text, and
  a Markdown renderer may still turn it into a link. An entry's `repo` becomes a link only
  when it is an `http` or `https` URL; any other value is written as text beside the name.
  `[render].boundary_note` is inserted as authored Markdown and is not escaped.
- **The generated CI installs the engine from a pinned git source.** The workflow
  `radar init` writes runs the engine with
  `uvx --from "git+https://github.com/grimaldost/stack-radar@<commit>"`, rather than by
  package name from an index. The commit is the one the running engine was installed from,
  read from its installation record: a tag can be moved to other code, a commit cannot.
  When that install asked for the release tag, the tag is named in a comment beside each
  step; when it asked for a branch or another commit, the header says the install did not
  ask for the release tag, so the commit may differ from the release's, and how to look
  the release's up. An engine that was not installed from this repository with git does
  not know the commit, writes the release tag (`@v<version>`) instead, and says in the
  workflow's header comment how to look the commit up and pin it. Building the engine from
  source fetches its build backend, `hatchling`, from the package index the runner
  reaches, at the one version this repository's `pyproject.toml` pins; hatchling's own
  dependencies are resolved from that index unpinned, so the pin covers the backend itself
  and not its transitive dependencies. The `setup-uv` step pins the `uv` version it
  installs, so the toolchain does not drift between runs without a commit. The Dependabot
  configuration `radar init` writes proposes updates to the pinned actions and to
  `uv.lock`; the commit-message hook's pinned commit, and the pinned `uv` version, are
  moved by hand.
