#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_ledger_discipline.py -- falsify the checker first, then trust it

Discipline: every rule must be made red once by a **tampered sample**. A check that cannot go
red is decoration -- it disguises "nobody verified" as "it was verified". This file throws two
or three samples at each of R1/R2/R3, plus one positive sample that must be all green, plus one
UNDECIDABLE (a missing piece may not be guessed as PASS).

No subprocess (cron has no PATH): everything calls verify/ledger_discipline.py's check()
in-process. No output = nothing to verify; so every case prints expected vs actual.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_discipline as LD  # noqa: E402

T0 = 1_700_000_000  # a fixed base time, so the test does not depend on the real clock


def w(path, text, mtime=None):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def plan_text(checked=True, with_marker=True, token="abc1234"):
    if checked:
        body = ("- [x] **T1 - item**\n"
                "  \u2192 done: commit `%s` @ 2026-01-01\n" % token) if with_marker else \
               "- [x] **T1 - item**\n"
    else:
        body = "- [ ] **T1 - item**\n"
    return "# PLAN\n\n## queue\n\n" + body + "\n- [ ] **T2 - not done yet**\n"


def ledger_line(wid="W-1", created="2026-01-01T00:00:00+08:00",
                touch="2026-01-01T00:00:00+08:00", note="commit abc1234"):
    return json.dumps({"id": wid, "kind": "works", "title": "t",
                       "status": "running", "created": created,
                       "last_touch": touch, "artifact": "", "note": note},
                      ensure_ascii=False)


def case(d, *, plan="", ledger="", preregs=(), settles=(), plan_missing=False,
         ledger_missing=False):
    """Build one fixture and run check(); returns (rc, output lines)"""
    pd = os.path.join(d, "plan.md")
    ld = os.path.join(d, "works_agenda.jsonl")
    rg = os.path.join(d, "prereg")
    os.makedirs(rg, exist_ok=True)
    if not plan_missing:
        w(pd, plan)
    if not ledger_missing:
        w(ld, ledger)
    for name, text, mt in preregs:
        w(os.path.join(rg, name), text, mt)
    for name, text, mt in settles:
        w(os.path.join(rg, name), text, mt)
    return LD.check(pd, ld, rg)


CASES = []


def expect(name, want_rc, want_substrings, **kw):
    CASES.append((name, want_rc, want_substrings, kw))


# A positive sample: done carries a commit id that is in the ledger; pre-registration before
# settle; ledger lines self-consistent -> 0 / all PASS
expect("A positive sample (expect all green)", 0,
       ["R1 LEDGER_COVERAGE PASS", "R2 PRE_REG_PRECEDES PASS", "R3 LEDGER_CONSISTENCY PASS"],
       plan=plan_text(), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# B R1 red: the ledger does not contain that commit id
expect("B R1 red: ledger missing the commit id", 1, ["R1 LEDGER_COVERAGE FAIL"],
       plan=plan_text(token="abc1234"), ledger=ledger_line(wid="W-2", note="another commit deadbee"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# C R1 red: checked [x] but no \u2192 done: marker
expect("C R1 red: checked without a done marker", 1, ["R1 LEDGER_COVERAGE FAIL", "no \u2192 done:"],
       plan=plan_text(with_marker=False), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# D R2 red: a settle exists but there is no pre-registration at all
expect("D R2 red: settle without pre-registration", 1, ["R2 PRE_REG_PRECEDES FAIL", "no pre-registration"],
       plan=plan_text(), ledger=ledger_line(),
       settles=[("RUN_009_SETTLE.md", "s", T0)])

# E R2 red: pre-registration later than settle
expect("E R2 red: pre-registration after settle", 1, ["R2 PRE_REG_PRECEDES FAIL", "pre-registration is later than settle"],
       plan=plan_text(), ledger=ledger_line(),
       preregs=[("PRE_REG_run_002.md", "p", T0 + 7200)],
       settles=[("RUN_002_SETTLE.md", "s", T0)])

# F R3 red: last_touch earlier than created
expect("F R3 red: last_touch goes backwards", 1, ["R3 LEDGER_CONSISTENCY FAIL", "out-of-order"],
       plan=plan_text(), ledger=ledger_line(created="2026-01-02T00:00:00+08:00",
                                            touch="2026-01-01T00:00:00+08:00"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# G R3 red: a bad JSON line
expect("G R3 red: bad JSON line", 1, ["R3 LEDGER_CONSISTENCY FAIL", "bad-JSON=L2"],
       plan=plan_text(), ledger=ledger_line() + "\n{not json\n",
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# H an undone task is not a violation: a queue of only - [ ] -> R1 must PASS (the check must not
# count "not done" as a violation)
expect("H undone tasks are not false-flagged", 0, ["R1 LEDGER_COVERAGE PASS", "done=0"],
       plan=plan_text(checked=False), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# I UNDECIDABLE: missing task-queue file -> rc=2, may not be guessed as PASS
expect("I missing piece = cannot decide", 2, ["R1 LEDGER_COVERAGE UNDECIDABLE", "rc=2"],
       plan_missing=True, ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


# J the artifact is a file (no commit id): a bare path written JSON-escaped in the ledger must
# still hit (tests normalisation)
PLAN_J = "# PLAN\n\n- [x] **T1 - item**\n  \u2192 done: artifact `E:/x/y/z.md`\n"
expect("J path reference (tests escape normalisation)", 0, ["R1 LEDGER_COVERAGE PASS"], plan=PLAN_J,
       ledger=ledger_line(wid="W-3", note="landed E:\\x\\y\\z.md 384 B"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


# K regression: **false-green sample** -- a relative path appeared in the ledger inside an
#   antonymic context such as "not landed" and must still be FAIL. This is a real incident
#   measured in practice (that line's referenced commit was not in the ledger, yet it was let
#   through because it hit another record that said "not landed").
PLAN_K = "# PLAN\n\n- [x] **T1 - item**\n  \u2192 done: artifact `verify/leakscan.py` @ x\n"
expect("K regression: antonymic context must not count as coverage", 1, ["R1 LEDGER_COVERAGE FAIL", "T1:"],
       plan=PLAN_K,
       ledger=ledger_line(wid="W-9", note="unresolved: verify/gate_stat.py not landed"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


def main():
    fails = []
    with tempfile.TemporaryDirectory(prefix="ldselftest_") as root:
        for i, (name, want_rc, subs, kw) in enumerate(CASES):
            d = os.path.join(root, "c%d" % i)
            os.makedirs(d, exist_ok=True)
            rc, lines = case(d, **kw)
            blob = "\n".join(lines)
            miss = [s for s in subs if s not in blob]
            ok = (rc == want_rc) and not miss
            print("[%s] %s  want_rc=%d got_rc=%d%s" % (
                "OK" if ok else "MISS", name, want_rc, rc,
                "" if not miss else "  missing=%s" % miss))
            for ln in lines:
                print("      | " + ln)
            if not ok:
                fails.append(name)
    print("\nSELFTEST %s  cases=%d failed=%d" % (
        "OK" if not fails else "FAILED", len(CASES), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
