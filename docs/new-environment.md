# Bringing a catalogue up on a new machine

You have a catalogue in git and a machine that has never run it — a second workstation, a
laptop, a locked-down machine. This is the walk-through. The steps a command can do are
kept apart from the four decisions no command can make for you.

Nothing about the machine is assumed. A committed environment profile ships with paths that
are true of the machine that wrote it and may be false of this one, and **a wrong path that
looks plausible is worse than a missing one** — so the first step discovers rather than
trusts.

## Before you start

You need `git`, Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and the engine:

```
uv tool install git+https://github.com/grimaldost/stack-radar@v0.2.1
```

Then get the catalogue onto the machine:

```
git clone <your-catalogue-url> my-radar
cd my-radar
```

Air-gapped instead? Carry a bundle and `git clone my-radar.bundle my-radar`. Feedback reports
are append-only and datestamped, so merging two machines' bundles is a union rather than a
conflict.

The catalogue must be a git repository — a clone is — because approvals of the commands it
declares are kept in its git directory, and a catalogue outside git runs none of them (see
*Step 4*).

A clone does not arm the catalogue's commit lane; arm it the way the first machine did:

```
git config core.hooksPath .githooks
```

That points git at the tracked `.githooks/`, so read `.githooks/`,
`.pre-commit-config.yaml`, `uv.lock`, `pyproject.toml`, `uv.toml` and `.python-version`
first: from then on they decide what runs on every commit, a branch's changes to them
included. The hooks run through `uv run --frozen`, which installs what `uv.lock` lists and
nothing else, and the first commit fetches the commit-message hook, so it needs uv and
network access once; the lane then refuses a commit whose subject is not a Conventional
Commit (`feat: ...`, `fix: ...`, `chore: ...`). A machine that cannot reach the network
leaves the lane unarmed, and the catalogue's CI runs the same checks on every push.

Everything below runs from inside the catalogue. Pick a short name for this environment —
`laptop`, or `<ENV>` in what follows — and use it consistently: it is the key for the
profile, the convergence scope, and every report. A name is letters, digits, `.`, `_` and
`-`, starting with a letter or digit, and not a device name Windows reserves (`CON`, `NUL`,
`COM1` and the like); every verb refuses any other `--env` with exit 2 before it reads a
file. A catalogue made by `radar init` already has a profile called `default`.
`radar field`, `radar reconcile`, `radar versions` and `radar sync-feedback` fall back to
`default_environment`, a top-level key of `radar.toml`, when `--env` is left out, or to
`default` when the marker names none. `radar apply`, `radar bootstrap` and
`radar render-feedback-targets` always need `--env`.

## Step 1 — discover the machine (read-only)

```
radar bootstrap --env <ENV>
```

It reports the platform, the real paths, which commands exist, which plugins are already
installed, which MCP servers are already mounted, and how the committed profile's declared
paths differ from this machine's actual ones. **It writes nothing without `--write`.**

What `--write` writes depends on what the catalogue already has:

- **No profile for `<ENV>` yet.** `radar bootstrap --env <ENV>` prints a starter profile;
  `radar bootstrap --env <ENV> --write` writes it as `environments/<ENV>.toml`, with the home
  directory written as `~`. That file is **tracked**: review it, then commit it.
- **A committed profile whose paths disagree with this machine.**
  `radar bootstrap --env <ENV> --write` leaves the profile alone and writes the differing
  paths to `environments/<ENV>.local.toml`, which is **gitignored**. Machine-specific paths
  are true of one workstation and false of every other, so they belong in the overlay: a
  committed profile that carried them would break the next machine that pulled it. The
  comparison is with the profile as every other verb reads it, the overlay layered on, and
  the overlay's paths are marked `(overlay)`, so a stale overlay shows as a difference too.
  When the overlay exists already, `--write` changes only its `[paths]` — adding what this
  machine disagrees on, dropping what the committed profile already gets right — and keeps
  every other key it sets, though not its comments.
- **A committed profile that matches.** It says `no overlay needed` and writes nothing, or,
  when an overlay sets paths that match too, names the paths it sets.

Every verb that reads the profile — `radar apply`, `radar reconcile`, `radar field`,
`radar versions`, `radar sync-feedback`, `radar render-feedback-targets` and the gate's
invariant scan — layers the overlay over it, because the profile names it:
`overlay = "<ENV>.local.toml"` under `[flags]`, which every profile `radar init` and
`radar bootstrap` write already carries. A `claude_home` or `feedback_root` fixed in the
overlay is therefore the one all of them use. If the profile names no overlay,
`radar bootstrap` says so when it proposes one, and no verb reads the file until the profile
names it. `radar apply` and `radar reconcile` list the files they read, the overlay marked
`(overlay)`, and an overlay that is not valid TOML stops each verb with a `[FAIL]` naming it.
A path under the home directory can be written with a leading `~` (`~/.claude`) in either
file; every reader expands it at run time, so the profile says where the path is without
naming the account.

## Step 2 — settle the policy

This is the part no command can do. Open `environments/<ENV>.toml` and decide four things.
Do not guess on the flags: both failure directions are real. A wrongly permissive flag can
push data or install software where policy forbids it; a wrongly restrictive one only costs
you a manual step.

1. **`rings`** — which rings this environment converges. A new environment starts
   `["own", "adopt"]`. `pilot` is deliberately absent: a pilot is an experiment, so add it
   only on a machine you are prepared to see broken by one.
2. **`flags.allow_external_mcp`** — may `radar apply` converge MCP servers here, including
   ones that reach outside the network boundary? Default `false`: MCP entries are skipped.
3. **`flags.allow_network_install`** — may `radar apply` install from the network? Default
   `false`: it prints each install command for you to run by hand instead of running it.
4. **`flags.sync`** — how this machine receives catalogue updates: `"git-remote"` if it can
   reach the catalogue's remote, `"git-bundle"` for hand-carry. It sits under `[flags]`, so
   it is written `flags.sync`; it is a record for the people maintaining the machine, and
   the engine does not read it.

For both flags, a missing `[flags]` table or a missing key counts as `false`; only a TOML
`true` turns one on. The header `radar apply` prints shows the effective values.

`exclude` lists entry names this environment does not converge; give each a reason in a
comment. **Never put a secret in any file here.** `require_env` holds the *names* of required
environment variables and nothing else; `radar apply` checks that they are set and never
reads or prints their values.

### Unsigned executables

One machine constraint worth discovering now rather than later: an operating system that
enforces binary reputation can refuse to run an unsigned executable. Two consequences
follow.

- An `artifact = "cli"` entry can be unusable here even with
  `allow_network_install = true`. The flags govern policy; this governs what the machine
  will run. Record it in the profile as a comment when you meet it, because the symptom — a
  probe that reports UNKNOWN — looks like a broken probe.
- The console-script shim a Python package installs (`pre-commit.exe`, for example) is such
  an executable, so a Python tool may run only as `python -m <module>`. This is why the
  engine and a catalogue's git hooks call `uv run --frozen python -m pre_commit` rather than
  `pre-commit install`, whose hooks call the shim and, where it is refused, leave
  `.git/hooks/` looking armed while nothing runs. It is also why `radar field` counts the
  `python -m` form under the entry's distribution name.

## Step 3 — declare this environment's own tools, if it has any

An environment often has tools that exist only in it: a site-specific library or server.
Two mechanisms, and the choice is yours per tool, because the costs differ:

- `tools/<name>.toml` with a top-level `environments = ["<ENV>"]` — travels in git, so it is
  written once and appears on every machine in that environment. **Cost: the tool's name
  lives in the repository.** Such an entry must be `visibility = "private"`.
- `tools.local/<name>.toml` — gitignored, never travels. **Cost: written again on each
  machine.** Give it a `redact_as` placeholder (`redact_as = "local-lib"`), the word that
  stands in for its name in anything the catalogue archives and in the field report's
  directory. It follows the rule a `name` does, since it becomes part of file names, and
  it may not contain the name or a `redact_also` spelling it stands in for.

If a name itself must not travel, the second is the only correct answer, and `radar gate`
enforces the consequence: no tracked file may contain the name of a `tools.local/` entry.

Two similar fields that are **not** the same thing: the top-level `environments` says which
environments a tool *exists* in; `[install].environments` says where `radar apply` should
*converge* it. The second must be a subset of the first.

Write entries with `radar add`, which validates before the file lands.

### Homes that are not this machine's

`radar redact-backfill` and `radar sync-feedback` map this machine's home directory to `~`
so no tracked file names the account. A report written on a *different* machine and synced
in carries that machine's home, which this one cannot discover. Declare those in
`redact.local.toml` at the data root:

```toml
# Homes belonging to other machines whose reports reach this catalogue.
homes = ["C:/Users/<account>", "/home/<account>"]
```

Gitignored, for the same reason `tools.local/` is: the account name is precisely what the
redaction exists to withhold, so a tracked declaration would publish it in order to say it
must not be published. A path is read in the flavour it is written in, so a Windows home
declared on a POSIX machine still matches both of its spellings. No file, or no entries, and
nothing changes.

### Working from a linked worktree

A catalogue can be checked out in a second git worktree — a branch under review, say. A
linked worktree with no `tools.local/` or `redact.local.toml` of its own reads the main
worktree's copy, and prints a `[NOTE]` on stderr naming each file it took that way. The
environment overlay is not shared: each worktree reads its own
`environments/<ENV>.local.toml`, so write one in the linked worktree too if the machine
needs it there. Approvals of catalogue commands are kept in the git directory every worktree
shares, so an approval recorded in one worktree holds in the others.

## Step 4 — converge

```
radar apply --env <ENV> --plan
```

The plan runs each entry's **approved** check probe and writes nothing else. On a machine
that has never run the catalogue nothing is approved yet, so the first plan lists every probe
as

```
[UNTRUSTED] <tool>: [install].check not run - new or changed since it was last approved: <command>
    listed as id <id>: --trust-commands on a later run approves it as it stands now
```

and exits 1. Read the commands — they come from the catalogue and run with your account —
then approve and run them:

```
radar apply --env <ENV> --plan --trust-commands
```

`--trust-commands` approves what an earlier run of the same verb listed, by id. A command
added or changed since — a pull landed, a branch was switched — is listed again rather than
run. Each run, with the flag or without it, replaces what the same verb listed before, and
the flag approves only from that verb's listing, so it never approves a command that verb's
latest run stopped showing.
Approvals live in `<git common dir>/stack-radar/trusted-commands.json`, outside anything git
tracks. A probe whose command, or a file it names or sits beside, changes later is pending
again and has to be approved again. Read the plan before applying it. Then:

```
radar apply --env <ENV> --apply
radar apply --env <ENV> --apply --yes --trust-commands
```

The first run shows what would execute and marks each install command that is not approved
yet with its id and what its approval covers, and lists apart, with the reason, an install
this machine will not run at all; the second runs the installs and approves the install
commands the first listed. It exits 0 when every install it ran succeeded and nothing else
needs action; a fresh shell may still be needed before the next `--check` sees new `PATH`
entries.

Two properties to preserve rather than work around:

- `radar apply` never executes a **removal**. It proposes them and leaves them to a human.
- A probe it cannot run reports **UNKNOWN** rather than "absent", so nothing is installed or
  demoted on the strength of a broken check. If a probe comes back UNKNOWN, investigate the
  probe; do not force the outcome.

Entries with `install.kind = "manual"` print instructions instead of running. That is a
design decision, not a failure to fix.

## Step 5 — verify, and read the result honestly

```
radar gate
radar apply --env <ENV> --check
radar reconcile --env <ENV>
radar versions --env <ENV>
```

Every one of these is safe to re-run. What to expect:

- `radar gate` reports 0 FAIL. Note what it says it **skipped**: on a fresh machine some
  checks have nothing to read yet, and the output names them individually rather than
  folding them into the pass.
- `radar apply --check` shows no pending action other than the deliberately manual entries.
- `radar reconcile` runs the same check probes as `radar apply`. A probe Step 4 already
  approved runs here without a flag; one not yet approved is listed as `[UNTRUSTED]`, and
  a later run of this verb with `--trust-commands` approves it. It will probably propose
  demotions, and you should not apply them yet. On a machine where the stack has only just
  been installed, "no observed evidence" mostly means "no sessions have happened yet" —
  this environment has not existed long enough to tell absence of evidence from evidence
  of absence. An entry it could not judge — a probe not approved, or an approved probe
  that says absent while an installer's record says installed — is a NOTE, and an entry
  this profile excludes or does not want is reported as not judged here. It also lists
  installed uv tools, plugins and skills the catalogue does not account for; that listing
  is a report only.
- `radar versions` reads installed versions from the installers' own records where it can
  (uv's tool directory, Claude Code's `installed_plugins.json`, a checkout's git tag) and
  falls back to the entry's check probe, which needs approval like any other. For an entry
  at `[version]` policy `any` it does not ask for that approval: the version is never
  compared, so it is shown when an installer's record or an approved probe gives it, and
  is `?` otherwise, with the probe marked as not needed. It also compares the plugin
  versions in `installed_plugins.json` with the copies the Claude desktop app serves to its
  sessions on Windows, where it can find them, and says nothing about the app where it
  cannot; `--no-plugins` skips that comparison.

If the catalogue's own tools declare `[feedback]`, two more commands apply; they are
optional, and the section *The feedback registry* in [concepts.md](concepts.md) explains
them:

```
radar render-feedback-targets --env <ENV>
radar sync-feedback --env <ENV> --check
```

## When it is done

The environment is up when `radar gate` reports 0 FAIL and `radar apply --check` shows no
pending action other than the deliberately manual entries. From then on the machine needs
nothing daily: feedback reports and field telemetry accumulate on their own, and you review
the catalogue whenever you choose to.

## Appendix — handing this to an agent

The walk-through above works as a hand-off to a fresh Claude Code session. Paste the block
below into a session running inside the freshly cloned catalogue, replacing `<ENV>`.

It is written for a session with **nothing**: no plugins, no skills, no `CLAUDE.md`, no
memory of anything before it. Everything it needs is either in the block, in a file of the
catalogue it is told to open, or in the engine's repository at an address the block gives.
It names no private tool, since the block may end up committed.

```
You are in a freshly cloned radar catalogue on a machine that has never run it. Your job: discover what this machine actually is, settle the policy questions a command cannot settle, converge the stack, and verify. The environment name for this machine is `<ENV>`. The user will direct next steps once you acknowledge this hand-off.

The catalogue is a CONTROL PLANE. It observes a tool stack, decides, applies, and then gets out of the way. The stack it governs - Claude Code plugins, skills, MCP servers, `settings.json`, `CLAUDE.md`, the user's own repositories - is the DATA PLANE. Knowledge flows ONE way: the catalogue reads and writes the stack; no file in the stack may reference the catalogue. If the catalogue were deleted, the stack must keep working in its current state. `radar gate` checks this by scanning the data plane. Never write a path to the catalogue into any file under the user's `.claude` directory or into any tool's working tree.

Nothing about this environment is assumed. A committed profile may carry paths that are true of another machine, and a wrong path that looks plausible is worse than a missing one.

Before acting, read the engine's concepts document, which is not in the catalogue or the installed package: https://github.com/grimaldost/stack-radar/blob/v<VERSION>/docs/concepts.md, with <VERSION> replaced by what `radar --version` prints. It covers rings, axes, artifact kinds, what the gate checks, and how catalogue commands are approved. Requirements on the machine: `git`, Python 3.11+, uv, and the `claude` CLI.

Step 1 - discover, read-only:

radar bootstrap --env <ENV>

It writes nothing without `--write`. If no profile exists for `<ENV>`, `--write` writes the tracked `environments/<ENV>.toml` with the home directory as `~`: review it with the user, then commit it. If a committed profile exists and its paths differ from this machine, `--write` writes the gitignored overlay `environments/<ENV>.local.toml` instead: machine-specific paths must never enter the committed profile. Every other radar verb that reads the profile layers that overlay over it.

Step 2 - settle the policy. This is the part no command can do. Open `environments/<ENV>.toml` and decide four things; ask the user for any you cannot determine from the machine or from local rules, and do NOT guess on the flags, because both failure directions are real.

1. `rings` - which rings this environment converges. A new environment starts ["own", "adopt"]. `pilot` is deliberately absent: a pilot is an experiment, so add it only on a machine the user is prepared to see broken by one.
2. `flags.allow_external_mcp` - may `radar apply` converge MCP servers here? Default false; a missing key counts as false.
3. `flags.allow_network_install` - may `radar apply` install from the network? Default false; when false it prints each install command to run by hand.
4. `flags.sync` - "git-remote" if this machine can reach the catalogue's remote, "git-bundle" for hand-carry. It sits under `[flags]`, so it is written `flags.sync`. A record for people; the engine does not read it.

Never put a secret in any file here. `require_env` holds the NAMES of required environment variables only.

Step 3 - declare this environment's own tools, if it has any. Two mechanisms, and the choice is the user's per tool: a tracked `tools/<name>.toml` with `environments = ["<ENV>"]` travels in git but puts the tool's name in the repository; a gitignored `tools.local/<name>.toml` never travels but is written again on each machine. Ask which applies; if the user has not raised it, do not invent entries. Top-level `environments` says which environments a tool EXISTS in, while `[install].environments` says where `radar apply` should CONVERGE it, and the second must be a subset of the first. Write entries with `radar add`, which validates before the file lands.

Step 4 - converge:

radar apply --env <ENV> --plan

This runs only approved check probes and writes nothing else. On the first run every probe is printed as [UNTRUSTED] and not run, and the command exits 1. Show the user each [UNTRUSTED] command - they come from the catalogue and run with the user's account - and re-run with `--trust-commands` only after the user approves them; the flag approves only the commands an earlier run listed, by id, and lists again anything that changed since. Read the plan out to the user and get approval before applying. Then run `radar apply --env <ENV> --apply`, which lists the install commands, show them to the user, and only with the user's approval run `radar apply --env <ENV> --apply --yes --trust-commands`. Two properties to preserve rather than work around: it never executes a REMOVAL, and a probe it cannot run reports UNKNOWN rather than "absent". If a probe comes back UNKNOWN, investigate the probe; do not force the outcome. Entries with `install.kind = "manual"` print instructions by design.

Step 5 - verify, and report honestly:

radar gate
radar apply --env <ENV> --check
radar reconcile --env <ENV>
radar versions --env <ENV>

All are safe to re-run. reconcile reports [UNTRUSTED] for any probe not yet approved, and versions for a probe it needs because no installer's record gives the installed version. `radar reconcile` will likely propose demotions - do NOT apply them. On a machine where the stack has only just been installed, "no observed evidence" mostly means "no sessions have happened yet". Report the proposals and leave them. A ring may only claim what is observably true: `adopt` and `pilot` require presence - a passing install probe, an installer's own record, a real project dependency, or a mounted server. If something is not present, the honest ring is `observe`, which costs nothing and claims nothing.

Suggested opening: run step 1 and show the user the discovery report before changing anything. Then walk through the four policy decisions in step 2, one at a time.
```
