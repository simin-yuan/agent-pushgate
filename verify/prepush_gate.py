#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prepush_gate.py -- the gate on the manual push path (the third entrance to the pre-publish
privacy gate)

Usage
    python verify/prepush_gate.py [--repo R] [--upstream REF] [--terms FILE]
                                  [--git GIT] [--quiet] [--no-log]

Exit code PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3 (FAIL takes priority over UNDECIDABLE).

Why it exists
  There are two publishing paths and the gate used to cover only one:
    1. the automatic export job -- the export step calls the three gates in-process at the exit
       (fail-closed);
    2. **manual** `git commit && git push` -- which had no gate at all. The measured leak came
       in through exactly a manual commit.
  This file is the gate body for (2), called by .git/hooks/pre-push (installed via
  tools/install_git_hooks.py). Its checks **share the very same functions** as (1) (leakscan /
  pushscope_gate / history_gate) rather than being written a second time -- one thing, two
  entrances, not two standards.

Scope -- what this gate does **not** cover (without this section the checks get treated as
something they cannot support)
  - It only takes effect on the local clones where the hook is installed. `git push --no-verify`
    bypasses it; the self-test (verify/selftest_prepush_hook.py) has a case that **measures this
    hole on purpose**, rather than pretending it does not exist. Other machines / CI have no
    such gate.
  - It covers only the git exit. Other exits (chat / X / email / web forms) are not here.
  - If the base cannot be taken (first push of a new branch, no local remote-tracking ref) ->
    UNDECIDABLE=2 -> refuse, and it is not guessed as clean. That is the price of fail-closed,
    stated in the open.
  - It does not judge information embedded in binaries and does not judge semantic re-spellings
    (the same blind spot as leakscan).

Can the checks produce a negative result?
  Yes. The self-test runs 9 real pushes (clean / dirty tree / dirty message / missing term list /
  no PATH / no PATH and no sidecar / --no-verify bypass / tampered hook / redirected
  core.hooksPath), and for each it only accepts two pieces of hard evidence: did the remote ref
  move, and what is in the trace log. verify/falsify_prepush_hook.py then breaks each of the
  three parts once, and the self-test must go red with it.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3
NAME = {PASS: "PASS", FAIL: "FAIL", UNDECIDABLE: "UNDECIDABLE", ERROR: "ERROR"}
CST = timezone(timedelta(hours=8))


def run(repo, upstream=None, terms_path=None, gitp=None):
    """Call the three gates in-process (cron has no PATH, so one less silently-failing link)
    -> (rc, lines)"""
    repo = os.path.abspath(repo)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import leakscan
    import pushscope_gate
    import history_gate

    lines = ["prepush_gate repo=...%s base=%s (= the range this push actually sends)"
             % (os.path.basename(repo), upstream or "(auto: @{u} / origin/main)")]
    trc, tl = leakscan.scan(repo, terms_path=terms_path)
    mrc, ml = pushscope_gate.scan_messages(repo, upstream=upstream, terms_path=terms_path, gitp=gitp)
    hrc, hl = history_gate.scan_history(repo, upstream=upstream, terms_path=terms_path, gitp=gitp)
    for ln in list(tl) + list(ml) + list(hl):
        lines.append("  " + ln)
    rcs = (trc, mrc, hrc)
    if FAIL in rcs:
        out = FAIL
    elif UNDECIDABLE in rcs:
        out = UNDECIDABLE
    elif any(r != PASS for r in rcs):
        out = ERROR
    else:
        out = PASS
    lines.append("PREPUSH %s  tree=%d msg=%d hist=%d -> rc=%d"
                 % (NAME.get(out, "?"), trc, mrc, hrc, out))
    return out, lines


def write_log(repo, rc, base):
    """Trace (.git/prepush_gate.log): did the gate run, and what was the verdict.

    Writes only the verdict and the first 12 chars of the base -- hit samples are publishing-
    surface material and do not go in here. This line is the **external trace** that "the gate
    really ran on this push": without it, a `--no-verify` bypass and "the gate let it through"
    look exactly alike.
    """
    p = os.path.join(repo, ".git", "prepush_gate.log")
    if not os.path.isdir(os.path.dirname(p)):
        return None
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("%s rc=%s base=%s\n" % (datetime.now(CST).isoformat(timespec="seconds"),
                                         rc, (base or "auto")[:12]))
    return p


def main(argv=None):
    # This tool prints text taken from the repository (paths, messages, samples). A Windows
    # console defaults to a legacy code page: one character it cannot encode would abort the
    # check with UnicodeEncodeError instead of printing a verdict. Escape what it cannot carry.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:                       # stdout replaced (StringIO) or already wrapped
        pass
    ap = argparse.ArgumentParser(description="Privacy gate on the manual push path (tree + commit messages + objects about to be pushed)")
    ap.add_argument("--repo", default=REPO, help="the repo to scan (default = the repo this script lives in)")
    ap.add_argument("--upstream", default=None,
                    help="the base of this push (the remote sha given on pre-push stdin). Default = auto: @{u} / origin/main")
    ap.add_argument("--terms", default=None, help="term list path (default <repo>/_local/privterms.txt)")
    ap.add_argument("--git", default=None, help="git executable (cron has no PATH; the hook passes it from the sidecar)")
    ap.add_argument("--quiet", action="store_true", help="print only the final verdict line")
    ap.add_argument("--no-log", action="store_true", help="do not write .git/prepush_gate.log")
    a = ap.parse_args(argv)
    try:
        rc, lines = run(a.repo, upstream=a.upstream, terms_path=a.terms, gitp=a.git)
    except Exception as e:                                        # noqa: BLE001
        print("PREPUSH ERROR %s: %s" % (type(e).__name__, e))
        print("PREPUSH ERROR rc=3")
        if not a.no_log:
            try:
                write_log(a.repo, ERROR, a.upstream)
            except OSError:
                pass
        return ERROR
    for ln in (lines[-1:] if a.quiet else lines):
        print(ln)
    if not a.no_log:
        try:
            write_log(a.repo, rc, a.upstream)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
