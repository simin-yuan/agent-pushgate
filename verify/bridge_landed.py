#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bridge_landed.py -- did the export **landing leg** actually land?

One-line background: the export job commits locally once, then pushes. **Local HEAD moving
forward is not the same as the remote receiving it.** Measured once: locally one extra commit
appeared, the export step's stdout was empty, the record still said ok, and remote main did not
move at all -- from "on this machine" everything looked fine, from "outside" this push had
already stopped.

This check answers one thing only: among the commits by which local is ahead of
`origin/<branch>`, **how long has the oldest one been waiting**.

Usage
    python verify/bridge_landed.py [--repo R] [--remote origin] [--branch main]
                                   [--max-age-h 2.0] [--git PATH]

Exit codes
    0 PASS        landed (HEAD == remote), or committed just now within the tolerance window (PENDING)
    1 FAIL        a commit has been stuck locally longer than --max-age-h (the output leg did not land)
    2 UNDECIDABLE the remote could not be read (network / tool missing / remote object not local)
                  -- **must not be read as 0**
    3 ERROR       any other exception

Can the checks produce a negative result?
    Yes. `verify/selftest_bridge_landed.py` builds four states with a **local bare repo** and
    runs each once (landed 0 / just committed 0 / stuck 1 / remote unreachable 2); all four
    states go through real git calls, no mocks. `--max-age-h` default 2.0 is a **choice**, not a
    measurement: the export cadence is 12h and a successful push takes seconds, so anything
    still not landed after two hours can only mean the leg is broken. Changing this number =
    changing the check.

Scope -- what this tool does **not** cover (without this section the checks get treated as
something they cannot support)
    - It compares shas only; it does not judge whether remote content matches local content.
    - It does not judge the cause of failure (credentials / network / permissions), it reads
      **no credentials** and performs no network write.
    - It does not judge whether any other gate ran: it only compares this repository's
      local HEAD with its remote-tracking ref.
    - The age is taken from the committer timestamp `%cI`, and it **depends on the local clock**;
      if the local clock is wrong, the age is wrong.
    - The remote sha comes from `git ls-remote` (authoritative), **not** from the local
      `origin/*` tracking ref -- the latter is only updated by this machine's push/fetch, so a
      broken leg would not know (it was still sitting on the old value).
    - Being able to compute "how old the oldest unlanded commit is" is not the same as "how long
      it has waited to be pushed": the latter cannot be measured locally; this is only an
      approximation of "time since the last successful landing".
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

DEFAULT_MAX_AGE_H = 2.0

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def find_git(explicit=None):
    """The cron service has no PATH, so try candidates in turn instead of relying on which."""
    cands = [explicit, os.environ.get("BRIDGE_GIT")]
    home = os.environ.get("HERMES_HOME")
    if home:
        cands.append(os.path.join(home, "git", "mingw64", "bin", "git.exe"))
    pf = os.environ.get("ProgramFiles")
    if pf:
        cands.append(os.path.join(pf, "Git", "cmd", "git.exe"))
    cands.append(shutil.which("git"))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    for c in cands:
        if c and not os.path.isabs(c) and shutil.which(c):
            return c
    return None


def git_run(git, repo, args, timeout=90):
    return subprocess.run([git] + args, cwd=repo, capture_output=True, text=True,
                          timeout=timeout)


def local_head(git, repo):
    r = git_run(git, repo, ["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def remote_head(git, repo, remote, branch):
    r = git_run(git, repo, ["ls-remote", remote, "refs/heads/%s" % branch], timeout=120)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out.split()[0] if out else None


def unpushed(git, repo, remote_sha):
    """-> [(sha, iso_committer_date)] for remote_sha..HEAD. Remote object not local -> None."""
    r = git_run(git, repo, ["log", "--format=%H|%cI", "%s..HEAD" % remote_sha])
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if "|" in line:
            sha, iso = line.split("|", 1)
            rows.append((sha, iso))
    return rows


def oldest_age_h(rows, now=None):
    """How many hours since the oldest unlanded commit. Returns None if it cannot be parsed
    (does not guess 0)."""
    stamps = []
    for _sha, iso in rows or []:
        try:
            stamps.append(datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc))
        except ValueError:
            return None
    if not stamps:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - min(stamps)).total_seconds() / 3600.0


def decide(local_sha, remote_sha, age_h, max_age_h):
    """Pure decision: anything that cannot be obtained is UNDECIDABLE, never silently degraded
    to PASS."""
    if not local_sha or not remote_sha:
        return UNDECIDABLE, "UNDECIDABLE", [
            "could not get the local or remote sha (the remote did not answer / tool missing) -- this is not 'landed'"]
    if local_sha == remote_sha:
        return PASS, "LANDED", ["local HEAD == remote %s" % local_sha[:10]]
    if age_h is None:
        return UNDECIDABLE, "UNDECIDABLE", [
            "local is ahead of remote, but the age of the lagging commits cannot be computed (timestamp parse failed / the remote object is not local)"]
    if age_h > max_age_h:
        return FAIL, "STALE", [
            "local is ahead of remote, oldest unlanded commit is %.2fh old (> %.2fh) -- the output leg did not land"
            % (age_h, max_age_h)]
    return PASS, "PENDING", [
        "local is ahead of remote, oldest unlanded commit is %.2fh old, still within the %.2fh tolerance window"
        % (age_h, max_age_h)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Has the local commit actually been pushed (local HEAD vs remote branch)")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H)
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    try:
        git = find_git(a.git)
        if not git:
            print("R0 INTERNAL ERROR   no git found (cron has no PATH; pass --git explicitly)")
            print("SUMMARY rc=%d" % ERROR)
            return ERROR
        lsha = local_head(git, a.repo)
        rsha = remote_head(git, a.repo, a.remote, a.branch)
        rows = unpushed(git, a.repo, rsha) if (lsha and rsha and lsha != rsha) else []
        age = oldest_age_h(rows)
        rc, status, notes = decide(lsha, rsha, age, a.max_age_h)
    except Exception as e:                                       # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=%d" % ERROR)
        return ERROR

    print("bridge_landed repo=%s remote=%s/%s max_age=%.2fh" % (a.repo, a.remote, a.branch,
                                                               a.max_age_h))
    print("  local  HEAD   %s" % (lsha or "(unavailable)"))
    print("  remote %-6s %s" % (a.branch, rsha or "(unavailable)"))
    if rows:
        print("  unlanded commits: %d, oldest %.2fh: %s" % (len(rows), age if age else -1,
                                                            rows[-1][0][:10]))
    for n in notes:
        print("  %s" % n)
    print("SUMMARY status=%s rc=%d" % (status, rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
