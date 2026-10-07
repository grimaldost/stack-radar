"""Refresh metrics for all tools into snapshots/<today>.json.

Sources: gh api (stars/watchers/pushed/archived/license) for GitHub repos, and
the registry named in each tool's `registry` field for downloads (npm:X or
pypi:X — per the identity rule, the registry field is set from repo metadata,
never name-matching; github:owner/repo declares releases as git tags, with no
download count to fetch). Network-slow by design (pypistats rate limits); run
monthly or before a radar review, then run `radar gate` against the fresh snapshot.

Machine-local entries (tools.local/) are never measured or written here: snapshots/
is tracked.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor

from .paths import UnsafeWrite, snap_dir, write_inside
from .radar_lib import (
    begin_command,
    github_repo,
    github_slug,
    latest_snapshot,
    load_tools,
    registry_errors,
)
from .trust import run_program

HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
# The repository facts one lookup produces, and the ones a failed lookup carries forward.
REPO_FACTS = ("stars", "watchers", "pushed", "archived", "license")
# Lookups run on a small pool: each is a separate network round trip with nothing to
# share, so a catalogue of many entries waited on them one at a time for no reason.
# pypistats gets a pool of its own with one worker, which keeps its requests spaced
# exactly as the sequential loop did.
LOOKUP_WORKERS = 8


def gh_repo(slug: str) -> dict:
    """The repository's facts, or `{"repo_status": ...}` when they could not be read.

    A failure is never written in place of the facts. `repo_status` is the HTTP status
    when gh reports one (404: the repository is gone or no longer visible), else
    "error"; `repo_error` is gh's own one-line reason.
    """
    # What `gh api repos/<slug>` is handed. The slug is cut out of a free-text `repo`
    # field, so it is checked against GitHub's own owner/name alphabet - the rule a
    # `github:` registry is held to - before it reaches a command.
    if github_slug(slug) != slug:
        return {"repo_status": "invalid-slug", "repo_error": f"not an owner/name slug: {slug!r}"}
    try:
        # The filter holds no quote or parenthesis, so a `gh` that is a batch file runs it
        # too (trust.run_program); a missing licence is filled in below instead.
        proc = run_program(
            [
                "gh",
                "api",
                f"repos/{slug}",
                "--jq",
                "{stars: .stargazers_count, watchers: .subscribers_count, "
                "pushed: .pushed_at[0:10], archived: .archived, license: .license.spdx_id}",
            ],
            capture_output=True,
            # gh writes UTF-8; the locale codec (cp1252 on Windows) would raise on some of
            # it, and one entry's undecodable output must not abort the whole refresh.
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"repo_status": "error", "repo_error": type(e).__name__}
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip().splitlines()
        m = HTTP_STATUS_RE.search(proc.stderr or "")
        return {
            "repo_status": int(m.group(1)) if m else "error",
            "repo_error": (reason[0] if reason else f"gh exited {proc.returncode}")[:120],
        }
    try:
        facts = json.loads(proc.stdout)
    except ValueError:
        return {"repo_status": "error", "repo_error": "gh printed no JSON"}
    if not isinstance(facts, dict):
        return {"repo_status": "error", "repo_error": "gh printed no JSON object"}
    if not facts.get("license"):
        facts["license"] = "none"
    return facts


def repo_entry(got: dict, old: dict, prev_date: str | None, slug: str) -> tuple[dict, bool]:
    """(the entry's repository fields, whether they were carried forward).

    `got` is what gh_repo returned for this entry on this pass, looking up `slug`.

    A failed lookup keeps the last good facts from the previous snapshot, marked
    `repo_stale` with the date they were measured, and records why this pass could not
    repeat them. Overwriting them with an error string would erase what is known about
    exactly the repositories that most need a look - one that has just disappeared.

    Facts are kept only while they describe the repository looked up now. `repo_slug`
    records the repository they were measured under, so an entry whose `repo` was
    corrected to another repository does not inherit the old one's stars, licence and
    push date when the new lookup fails. Facts recorded with no slug have nothing to
    compare against, and carry forward.
    """
    if "repo_status" not in got:
        return got | {"repo_slug": slug}, False
    out = {k: got[k] for k in ("repo_status", "repo_error") if k in got}
    measured_under = old.get("repo_slug")
    if measured_under is not None and measured_under != slug:
        return out, False
    kept = {k: old[k] for k in (*REPO_FACTS, "repo_slug") if k in old}
    if not any(k in kept for k in REPO_FACTS):
        return out, False
    out |= kept
    out["repo_stale"] = True
    out["repo_as_of"] = old.get("repo_as_of") or prev_date or "earlier"
    return out, True


def fetch_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def registry_kind(registry: str | None) -> str | None:
    """`pypi`, `npm` or `github` from a `registry` value, or None when none is declared."""
    if not registry:
        return None
    return str(registry).partition(":")[0]


def downloads(registry: str) -> tuple[int | None, str | None]:
    # Checked before the name is put into a URL, by the rule the gate applies to the
    # entry: a snapshot does not run the gate, so an entry it would refuse gets here.
    if registry_errors(registry):
        return None, None
    kind, _, pkg = registry.partition(":")
    if kind == "npm":
        d = fetch_json(f"https://api.npmjs.org/downloads/point/last-month/{pkg}")
        return (d or {}).get("downloads"), registry
    if kind == "pypi":
        d = fetch_json(f"https://pypistats.org/api/packages/{pkg}/recent")
        time.sleep(8)  # pypistats rate limit
        return ((d or {}).get("data") or {}).get("last_month"), registry
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar snapshot", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--data-root",
        help="the radar data root, instead of searching upwards for radar.toml. For CI "
        "and nothing else; it still has to name a directory that carries the "
        "marker",
    )
    args = ap.parse_args()
    root = begin_command(args.data_root)
    # A REFRESH MUST NOT LOSE EVIDENCE.
    # Downloads come from a rate-limited third party, so a lookup fails sometimes.
    # Omitting the field on failure would silently ERASE a known number - and the
    # astroturf detector, which needs downloads to fire, would go quiet: the gate's flag
    # count would fall while it knew strictly less. A monitor that improves its own report
    # by forgetting is worse than no monitor.
    # So a failed lookup carries the previous value forward, marked with the date it was
    # actually measured. Stale-but-labelled beats absent-and-silent. Repository facts
    # follow the same rule (see repo_entry).
    prev_name, prev = latest_snapshot()
    prev_date = prev_name[: -len(".json")] if prev_name else None
    snap = {}
    carried = 0
    dropped = 0
    repo_carried: list[str] = []
    today = datetime.date.today().isoformat()
    # include_local=False, for the reason render.py gives: snapshots/ is TRACKED, and a
    # tools.local/ entry written into it would carry its real name and repository into a
    # pushed file, which is what keeping it in a gitignored directory was meant to prevent.
    # Omitted rather than written under its `redact_as` placeholder: stars, licence and
    # push date identify a repository as surely as its name does. An entry with no
    # snapshot facts gives the gate nothing to read as fresh - no push date to call
    # recent, no download figure to clear it - so leaving it out cannot flatter it.
    tools = load_tools(include_local=False)
    local_n = len(load_tools()) - len(tools)
    # Every lookup is submitted first and read back in catalogue order below, so the
    # output and the file are the same whichever lookup finishes first.
    repo_jobs: dict[str, Future] = {}
    slugs: dict[str, str] = {}
    download_jobs: dict[str, Future] = {}
    with (
        ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool,
        ThreadPoolExecutor(max_workers=1) as pypi_pool,
    ):
        for t in tools:
            slug = github_repo(t.get("repo"))
            if slug:
                slugs[t["name"]] = slug
                repo_jobs[t["name"]] = pool.submit(gh_repo, slug)
            kind = registry_kind(t.get("registry"))
            # A `github:` registry says releases are the repository's git tags, which
            # carry no download count. There is nothing to look up, so it is not a
            # failed lookup either.
            if kind and kind != "github":
                lane = pypi_pool if kind == "pypi" else pool
                download_jobs[t["name"]] = lane.submit(downloads, t["registry"])
        repo_results = {n: f.result() for n, f in repo_jobs.items()}
        download_results = {n: f.result() for n, f in download_jobs.items()}
    for t in tools:
        name = t["name"]
        entry: dict = {}
        old = prev.get(name) or {}
        if name in repo_results:
            facts, stale = repo_entry(repo_results[name], old, prev_date, slugs[name])
            entry |= facts
            if stale:
                repo_carried.append(name)
            if "repo_status" in facts:
                print(
                    f"[NOTE] {name}: repository lookup failed ({facts['repo_status']}: "
                    f"{facts.get('repo_error', '')})"
                    + (
                        f" - kept the facts measured {facts['repo_as_of']}, marked repo_stale"
                        if stale
                        else ""
                    )
                )
        kind = registry_kind(t.get("registry"))
        if name in download_results:
            dl, src = download_results[name]
            if dl is not None:
                entry["downloads_month"] = dl
                entry["downloads_source"] = src
        # Carry forward whenever THIS pass produced no figure, not only when a lookup
        # failed. Guarding only the failed-lookup path would leave this one open, and the
        # same entries would lose their numbers on the next refresh.
        #
        # The gap is structural rather than accidental: such entries declare NO
        # `registry` at all - any figure they carry is entered by hand, because the
        # astroturf case is "huge star count, and the package is not really distributed
        # anywhere". So the entries that most need a download number are the ones least
        # able to refresh it, and gating carry-forward on `registry` dropped exactly them.
        if kind == "github":
            # No download figure is recorded for a git-tag registry, carried or fresh: the
            # gate treats it as declaring no download registry, so a number here could
            # only be read as evidence about an identity the entry does not claim.
            if old.get("downloads_month") is not None:
                dropped += 1
                print(
                    f"[NOTE] {name}: dropped a download figure - the entry declares "
                    f"{t.get('registry')!r}, whose releases are git tags with no download count"
                )
        elif "downloads_month" not in entry:
            old_source = old.get("downloads_source")
            # A figure is only evidence about THIS entry while the registry it was
            # measured under still applies. `downloads_source` records that registry
            # (set beside `downloads_month` above), so a mismatch against the entry's
            # current `registry` - removed, or pointed somewhere else - means the old
            # number belongs to an identity this entry no longer claims, and carrying
            # it forward would be carrying forward someone else's evidence - which is
            # what happens when a wrong `registry` is corrected away after a figure was
            # measured under it, unless the source is compared.
            #
            # An entry that has NEVER declared a registry records no `downloads_source`
            # at all (`old_source` is None) - there is nothing to compare, so that is
            # not a mismatch, and a hand-measured figure keeps carrying forward.
            registry_changed = old_source is not None and old_source != t.get("registry")
            if old.get("downloads_month") is not None and not registry_changed:
                entry["downloads_month"] = old["downloads_month"]
                entry["downloads_source"] = old_source
                entry["downloads_as_of"] = old.get("downloads_as_of") or "earlier"
                entry["downloads_stale"] = True
                carried += 1
            elif registry_changed:
                dropped += 1
                now = t.get("registry")
                where = f"declares {now!r}" if now else "declares no `registry` at all"
                print(
                    f"[NOTE] {name}: dropped a stale download figure measured under "
                    f"{old_source!r} - the entry now {where}; a figure from a "
                    "different registry is not evidence about this one"
                )
        if entry:
            if "downloads_month" in entry and not entry.get("downloads_stale"):
                entry["downloads_as_of"] = today
            snap[name] = entry
            print(f"{name}: {entry}")
    out = snap_dir(root) / f"{today}.json"
    # `radar init` creates no snapshots/, so the first snapshot creates it. write_inside
    # refuses a symbolic link on the way, and writes LF on every OS: snapshots/ is
    # tracked, and CRLF would read as a change on Windows.
    try:
        write_inside(root, out, json.dumps(snap, indent=1, sort_keys=True))
    except UnsafeWrite as exc:
        print(f"[FAIL] snapshot: {exc} - nothing written")
        raise SystemExit(1) from None
    print(f"\nwrote {out} ({len(snap)} entries)")
    if carried:
        print(
            f"[NOTE] {carried} entr{'y' if carried == 1 else 'ies'} kept an earlier "
            "download figure because the registry lookup failed (marked downloads_stale); "
            "a refresh never erases a measurement it could not repeat"
        )
    if dropped:
        print(
            f"[NOTE] {dropped} entr{'y' if dropped == 1 else 'ies'} dropped a download "
            "figure measured under a registry the entry no longer declares, or one with "
            "no download count - a stale number under the WRONG identity is worse than none"
        )
    if repo_carried:
        print(
            f"[NOTE] {len(repo_carried)} entr{'y' if len(repo_carried) == 1 else 'ies'} kept "
            "earlier repository facts because the lookup failed (marked repo_stale, with "
            "repo_status saying why): " + ", ".join(repo_carried)
        )
    if local_n:
        print(
            f"[NOTE] {local_n} machine-local entr{'y' if local_n == 1 else 'ies'} not "
            "measured: snapshots/ is tracked, so tools.local/ entries are never written "
            "into it"
        )


if __name__ == "__main__":
    main()
