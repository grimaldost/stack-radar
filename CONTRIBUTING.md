# Contributing

This document is mostly about what the checks hold a change to, and why each one is where
it is. Most of what follows is enforced by a check. The rest is held by review: the
commit-body conventions under *Commits* (no AI-attribution trailers, red proofs, a verifier
not edited in the change it judges) and most of *What a change should not do*. None of it is
a matter of taste.

## Set up

```
git clone https://github.com/grimaldost/stack-radar && cd stack-radar
uv sync
uv run radar --help
```

The engine has **no runtime dependencies** — standard library only, Python 3.11 and up — and
that is a constraint rather than an accident. The commands are meant to run where
installing anything may need approval, so a runtime dependency would be a new
precondition for every command. It is also what makes the fallback work: where a wheel
cannot be installed at all, `PYTHONPATH=<checkout>/src python -m stack_radar.cli` is a
complete installation.

The `dev` dependency group carries `pytest`, `ruff`, `ty` and `pre-commit`. Those are for
developing the engine and are never runtime requirements. Adding a runtime dependency is a
decision to argue for, not a convenience to reach for.

## The checks, in this order

Every change has to pass this list. Run it locally; continuous integration runs the same
list on Linux and on Windows, in the `checks` job, on every push and pull request.

```
uv lock --check
uv sync
uv run ruff format --check .
uv run ruff check .
uv run ty check src
uv run python -m pytest -q
```

`uv lock --check` goes **first** and cannot be moved. No test can guard lock staleness,
because `uv run` re-locks before pytest could read the file — by the time anything else runs,
the thing being checked has already been rewritten.

Two more judge this repository rather than a catalogue, and need no data root. CI runs both:

```
uv run radar check-version-sites
uv run radar changelog-gate origin/main
```

- `radar check-version-sites` runs at the end of the `checks` job, on both operating
  systems, after the suite.
- `radar changelog-gate` runs in the `changelog-gate` job, which runs for pull requests
  only, because only a pull request has a base to compare against. It judges every commit
  between the merge base and the head.

The suite runs most engine commands in the test process, which keeps it fast.
`RADAR_TEST_SUBPROCESS=1` starts every command as a real process instead, which is how the
suite shows the in-process runner hides nothing a process start would: the `checks` job
runs it on Linux after the default run, and the `real-process-windows` job runs it on
Windows weekly and on demand (`workflow_dispatch`), because starting a process costs much
more there. Before a release, run it locally on Windows too:

```
RADAR_TEST_SUBPROCESS=1 uv run python -m pytest -q
```

The other jobs exercise what the suite cannot reach from inside the checkout:
`stranger-path` builds the wheel, installs it into a clean environment and runs `radar init`,
`radar add`, `radar gate` and `radar render` in a directory that never held a catalogue;
`no-root` checks that `radar gate` outside any catalogue exits 2 and names `radar.toml`; and
`zizmor` audits this repository's workflows and the ones `radar init` writes.

The suite is hermetic: the tests that exercise commands end to end build a miniature
catalogue in `tmp_path` and run against that. Nothing in it reads the machine's real
`~/.claude` or writes outside the temporary directory.

### The commit lane

`.githooks/` carries a tracked `pre-commit` and `commit-msg` pair, and
`.pre-commit-config.yaml` says what they run. After `uv sync`, arm them once per clone:

```
git config core.hooksPath .githooks
```

That points git at the tracked `.githooks/`, so a branch that changes it, or
`.pre-commit-config.yaml`, `uv.lock`, `pyproject.toml`, `uv.toml` or `.python-version`,
changes what runs on your next checkout or commit. Read those files in a pull request
before checking it out. Every `uv run` the lane makes is `uv run --frozen`, which installs
the project's dependencies from `uv.lock` alone: a dependency that `pyproject.toml`
declares and `uv.lock` does not carry is never resolved or installed. This project is
built, though, and `--frozen` does not stop uv rebuilding it when `pyproject.toml` changes.
The rebuild installs the `[build-system]` requirements and runs the build backend that
`pyproject.toml` names, so a change there runs at your next commit; that is why
`pyproject.toml` is on the list.

At `pre-commit`, the lane runs `ruff format --check` and `ruff check` on the staged Python
files, through `uv run --frozen`, so the ruff that runs is the one `uv.lock` pins. At
`commit-msg`, it runs `compilerla/conventional-pre-commit`, pinned to a commit, which
refuses a subject that is not a Conventional Commit. The first commit after arming fetches
that hook into pre-commit's cache, which needs network access once. Run the lane by hand
with `uv run --frozen python -m pre_commit run --all-files`, and skip it for one commit with
`git commit --no-verify` when you mean to; CI runs the full list regardless.

The hooks are tracked files rather than generated ones so that a fresh clone arms the lane
with one command. They deliberately do **not** go through `pre-commit install`, whose hooks
call the `pre-commit` console-script shim, which some machines refuse to run (see
*Unsigned executables* in [docs/new-environment.md](docs/new-environment.md)); a hook that
silently never runs is worse than none. The tracked scripts invoke
`uv run --frozen python -m pre_commit` instead: the project's own pinned interpreter, no
shim, and a hook a reviewer can read.

`tests/test_commit_lane.py` checks what arming relies on: each hook is executable in the
index (git on Linux and macOS skips one that is not, with only a hint that is easy to
miss), runs a config this repository tracks and that declares a hook for its stage, and
`pre-commit` is in the `dev` group. It also checks that every `uv run` the lane makes is
frozen, and runs each hook's `uv run` against a `pyproject.toml` that declares a dependency
`uv.lock` does not carry, to show that nothing is built or installed. A last test runs them
on a built project whose `pyproject.toml` names a different build backend, to show that the
rebuild does run it.

## Commits

Conventional Commits: `feat fix docs style refactor perf test chore build ci release`, an
imperative subject, one concern per commit.

**No AI-attribution trailers.** Not `Co-Authored-By` lines naming a model, not "generated
with" footers, not anywhere in the message.

**Red proofs go in the body.** Every check that ships carries recorded proof that it can
fail: the new test run against the pre-change code, with the failing assertion pasted under a
`Red proof:` heading. A verifier that has never been red has not been tested.

The suite is built for that. `RADAR_ENGINE_SRC` points the tests at a different `src/` tree,
so checking out an earlier commit into a temporary worktree and running the same tests
against it is the whole procedure:

```
git worktree add --detach <tmp> <base-commit>
RADAR_ENGINE_SRC=<tmp>/src uv run python -m pytest -q tests/<the-new-test>.py
```

**The verifier is not edited in the change it judges.** A fixture or an oracle moves in its
own commit, or in the commit that changes its source when that is unavoidable — with the
reason in the message either way.

## Recording a change

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with release
headings exactly `## [X.Y.Z] - YYYY-MM-DD` and section headings from its vocabulary: Added,
Changed, Deprecated, Removed, Fixed, Security. The heading grammar is not cosmetic: the
newest released heading is one of the version sites `radar check-version-sites` compares, so
a heading written some other way is a version site that has stopped existing.

`src/` is the watched surface. `radar changelog-gate` judges **every commit in the range on
its own**: a commit whose own diff touches `src/` must either be recorded by the range's
`CHANGELOG.md` diff or carry a `Changelog: none (<reason>)` trailer in its *own* message. A
trailer on a neighbouring commit does not cover it, and a bare `Changelog: none` does not
satisfy it — the reason is the reviewable part.

A record is an **added line** whose whitespace-collapsed form the same diff does not also
remove. Touching the file, reflowing it, reordering bullets, converting line endings or
deleting it outright all record nothing.

Three files are **contract surfaces** — `src/stack_radar/radar_lib.py` (the entry schema and
the closed `[telemetry]` key set), `src/stack_radar/field_report.py` (the report document
another reader parses) and `src/stack_radar/gate.py` (the exit code a CI step branches on). A
change to any of them that alters what a consumer sees is marked `(consumer-affecting)` in
its changelog entry; the changelog gate warns when a diff touching one never says it.

Argue the bump class **in prose**, inside the entry ("minor and not patch, because…"),
for every release after the first. Nothing judges major, minor or patch mechanically; the
gate checks presence and coherence, and the judgement stays with the author.

## Releasing

The version is written in several places, and `radar check-version-sites` (and the test
suite) compares every one of them with the anchor, `pyproject.toml [project].version`:

- `CHANGELOG.md`'s newest released heading;
- `__version__` in `src/stack_radar/__init__.py`;
- `uv.lock`'s entry for the project itself;
- every install line in `README.md` and `docs/**/*.md` that installs this project from a
  tagged git source, `git+https://github.com/<owner>/stack-radar@vX.Y.Z`.

It also checks that the release headings are in descending version order.

A release is a metadata-only `release: X.Y.Z` commit — the changelog roll plus the version
bump in every site above, never a feature, so `git bisect` keeps working. Bump
`[project].version`, then run `uv lock` so the lock records it, and commit the result with
the rest. Then lay an **annotated tag on that commit**:

```
git tag -a vX.Y.Z <release-commit> -m "stack-radar X.Y.Z"
```

Annotated, not lightweight: a lightweight tag records no tagger date, so a tag laid at the
wrong commit cannot say when it was laid. A published tag is never re-pointed; a tag error is
corrected forward, with a new release.

The first commit of the public history is a single root commit carrying the first published
release. It is the one exception to the release-commit rule, and to the Conventional Commit
subject the commit-msg hook asks for: there is no earlier commit for it to roll forward from,
and its subject is `stack-radar X.Y.Z`, the same text as the annotated tag laid on it.
Every later commit follows both rules.

## What a change should not do

- **Name a particular catalogue, or the tools in one.** One engine serves any number of
  catalogues and knows none of them by name. A name that belongs to somebody's stack belongs
  in their `radar.toml`, their entries or their profiles — never in this repository, in code
  or in prose.
- **Write a real machine path.** No `C:/Users/<an-account>` in an example, ever. Write the
  placeholder a profile fills, which is what the example actually means.
  `tests/test_docs.py` checks the documents for this, and the gate's publication check
  applies the same rule to a whole tree.
- **Find the home directory outside `bootstrap.py`.** Exactly one module may do that,
  because that is its job. Everywhere else the machine path comes from the environment
  profile's `[paths]`, which is what lets one entry work on several machines.
- **Make a check quieter.** A check that cannot run says so and names itself; it never counts
  as a pass. "0 findings" out of a scan that read nothing is the exact wording of a clean
  scan, and that collision is the failure mode most of this code is shaped around.
