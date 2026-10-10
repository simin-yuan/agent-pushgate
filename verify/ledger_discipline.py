#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ledger_discipline.py -- a check that can fail, for ledger and pre-registration discipline

What it judges
  R1 LEDGER_COVERAGE  : every task in the task queue that is `- [x]` and carries `-> done:`
                        must have at least one of the **artifact references** in its done
                        section (a commit-shaped token: 7-12 hex chars containing at least one
                        a-f letter; or an absolute path: drive-letter path) appear in the
                        ledger.
                        `- [x]` without `-> done:` also counts as a violation. **Only absolute
                        paths are accepted**: a relative path (such as `verify/gate_stat.py`)
                        can hit the ledger inside an antonymic context such as "not landed",
                        which measured produced a false green (see self-test case K), so it is
                        not accepted.
  R2 PRE_REG_PRECEDES : every settle file (`RUN_*_SETTLE.md` / `SETTLE_*.md`) must match a
                        candidate pre-registration (`PRE_REG_*.md`, filename containing the same
                        numeric segment), and at least one of those pre-registrations must have
                        mtime <= the settle file's mtime. Missing pre-registration or reversed
                        order -> FAIL.
  R3 LEDGER_CONSISTENCY: every ledger line must be valid JSON; and `last_touch` must not be
                        earlier than `created` (when both parse).

Scope -- what this tool does **not** cover (without this section the checks get treated as
something they cannot support)
  - R1 only proves "this commit-id/path string appeared somewhere in the ledger". It does
    **not** prove the artifact really exists, does not prove it hangs under the right work
    item, and does not check whether someone else wrote it. Whether the artifact is really on
    disk is out of scope: read the disk or the version control yourself; this tool does not
    build a second copy of a file-existence check.
    Matching is **loose**: a string appearing in any one ledger record counts as covered; it
    does no "which work item should own which artifact" attribution.
  - R1 **cannot tell a reference from an antonym**: "this path was delivered" and "this path was
    not landed" are the same string layer. For absolute paths the mis-match probability is low
    but not zero -- so R1 is a coverage clue, not proof of delivery (proof of delivery means
    reading the disk or the VCS).
  - R2 only compares mtime, it **does not read file content**: mtime can be rewritten by copy /
    checkout / sync. So R2's PASS only means "the order is right by file timestamp", **not**
    "the prediction really was written before publication". A stronger check would need a
    prediction ledger with embedded timestamps, or git commit history (this project has neither
    yet -- recorded as a known gap).
  - R2's candidate matching picks by "filename numeric segment intersects", which **may be too
    wide** (NNN from different sources offer each other as candidates).
  - R3 only checks in-line consistency and line parse-ability; it **does not check** cross-line
    order or id uniqueness.

Can the checks produce a negative result?
  Yes. `selftest_ledger_discipline.py` throws one tampered sample at each of the three rules
  (drop the commit id / delete the pre-registration / pre-registration later than settle /
  last_touch earlier than created), and all four measured go red; plus one positive sample that
  must be all green. A check you cannot make fail is decoration -- so the test set must contain
  negative samples, and that rule is written into the test file.

Exit codes
  PASS=0 / FAIL=1 / UNDECIDABLE=2 (file missing or unreadable, no guessing) / ERROR=3 (own
  exception) FAIL takes priority over UNDECIDABLE. When the checks are insufficient it says
  "cannot decide" rather than returning a number that looks like a conclusion.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# The machine landing points **do not enter the public repo**: see .gitignore's `_local/`.
# Resolution order: explicit `--plan/--ledger/--prereg-dir` > `_local/wiring.json`.
# All missing -> fall back to a **clearly non-existent placeholder**, so the tool finishes as
# UNDECIDABLE (rc=2) instead of quietly running against the wrong target and reporting green.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WIRING = os.path.normpath(os.path.join(_HERE, os.pardir, "_local", "wiring.json"))


def _default_path(key, placeholder):
    try:
        with open(_WIRING, "r", encoding="utf-8") as fh:
            val = json.load(fh).get(key)
        if val:
            return val
    except Exception:
        pass
    return placeholder


DEF_PLAN = _default_path("plan", os.path.join("_local", "PLAN.md"))
DEF_LEDGER = _default_path("ledger", os.path.join("_local", "works_agenda.jsonl"))
DEF_PREREG = _default_path("prereg_dir", os.path.join("_local", "prereg"))

TASK_RE = re.compile(r"^- \[( |x)\]\s*\*\*(T\d+)")
TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{7,12}(?![0-9a-fA-F])")
PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s`\"'()\uff08\uff09;\uff1b,\uff0c]+")
NUM_RE = re.compile(r"\d{3,}")

PASS, FAIL, UNDECIDED = "PASS", "FAIL", "UNDECIDABLE"


def norm(s):
    """Path normalisation: fold \\ and / into /, so that a JSON-escaped path in the ledger can
    be matched against the bare path in the queue."""
    return re.sub(r"[\\/]+", "/", s or "")


def commit_tokens(text):
    """Commit shape: 7-12 hex chars containing at least one letter (excludes pure-number dates
    such as 20260928)."""
    out = []
    for m in TOKEN_RE.finditer(text or ""):
        t = m.group(0)
        if not re.search(r"[a-f]", t):
            continue
        if t not in out:
            out.append(t)
    return out


def product_refs(text):
    """Artifact references = commit ids union paths (after normalisation)."""
    out = list(commit_tokens(text))
    for m in PATH_RE.finditer(text or ""):
        p = norm(m.group(0)).rstrip(".,;:\u3001\uff09)")
        if len(p) > 8 and p not in out:
            out.append(p)
    return out


def ledger_haystack(path):
    """The ledger's comparable text: raw lines + parsed field values (the latter have JSON
    escaping removed), all normalised."""
    if not os.path.isfile(path):
        return ""
    chunks = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            chunks.append(line)
            try:
                j = json.loads(line)
            except Exception:
                continue
            for v in j.values():
                if isinstance(v, str):
                    chunks.append(v)
                elif isinstance(v, list):
                    chunks.extend(str(x) for x in v)
    return norm("\n".join(chunks))


def parse_tasks(plan_text):
    """-> [(tid, checked, block_text), ...]"""
    tasks, cur = [], None
    for line in (plan_text or "").splitlines():
        m = TASK_RE.match(line)
        if m:
            if cur:
                tasks.append(cur)
            cur = [m.group(2), m.group(1) == "x", line]
        elif cur is not None:
            cur[2] += "\n" + line
    if cur:
        tasks.append(cur)
    return [(t[0], t[1], t[2]) for t in tasks]


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def rule_r1(plan_path, hay):
    if not os.path.isfile(plan_path):
        return UNDECIDED, "missing task-queue file: %s" % plan_path, []
    tasks = parse_tasks(read_text(plan_path))
    done = [t for t in tasks if t[1]]
    marker = "\u2192 done:"
    missing, n_ok, no_marker, rows = [], 0, [], []
    for tid, _c, block in done:
        if marker not in block:
            no_marker.append(tid)
            rows.append("   -> %s has [x] but no done marker (needs U+2192 + 'done:')" % tid)
            continue
        refs = product_refs(block)
        hit = [t for t in refs if t in hay]
        rows.append("   -> %s refs=%d hit=%s" % (
            tid, len(refs), ",".join(hit) if hit else "-"))
        if hit:
            n_ok += 1
        else:
            shown = ",".join(refs[:3]) + ("..." if len(refs) > 3 else "")
            missing.append("%s:[%s]" % (tid, shown or "<no artifact ref>"))
    if missing or no_marker:
        return FAIL, "done=%d covered=%d missing=%d %s %s" % (
            len(done), n_ok, len(missing), " ".join(missing),
            ("| has [x] but no done marker (needs U+2192 + 'done:'): %s"
             % ",".join(no_marker)) if no_marker else ""), rows
    return PASS, "done=%d covered=%d" % (len(done), n_ok), rows


def rule_r2(prereg_dir):
    if not os.path.isdir(prereg_dir):
        return UNDECIDED, "missing pre-registration directory: %s" % prereg_dir
    names = sorted(os.listdir(prereg_dir))
    pre = [n for n in names if n.startswith("PRE_REG_") and n.endswith(".md")]
    settles = [n for n in names
               if (n.startswith("RUN_") and n.endswith("_SETTLE.md"))
               or (n.startswith("SETTLE_") and n.endswith(".md"))]
    if not settles:
        return UNDECIDED, "no settle file in the directory (%d pre-registrations)" % len(pre)
    bad, ok = [], 0
    for s in settles:
        segs = set(NUM_RE.findall(s))
        cands = [p for p in pre if segs & set(NUM_RE.findall(p))]
        if not cands:
            bad.append("%s:no pre-registration" % s)
            continue
        sm = os.path.getmtime(os.path.join(prereg_dir, s))
        good = [c for c in cands
                if os.path.getmtime(os.path.join(prereg_dir, c)) <= sm]
        if good:
            ok += 1
        else:
            bad.append("%s:pre-registration is later than settle (%s)" % (s, ",".join(cands)))
    if bad:
        return FAIL, "settles=%d order-ok=%d %s" % (len(settles), ok, " ".join(bad))
    return PASS, "settles=%d order-ok=%d" % (len(settles), ok)


def rule_r3(ledger_path):
    if not os.path.isfile(ledger_path):
        return UNDECIDED, "missing ledger file: %s" % ledger_path
    bad_json, bad_order, n = [], [], 0
    with open(ledger_path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                j = json.loads(line)
            except Exception as e:
                bad_json.append("L%d(%s)" % (i, type(e).__name__))
                continue
            c, t = parse_ts(j.get("created")), parse_ts(j.get("last_touch"))
            if c and t and t < c:
                bad_order.append("L%d(%s<%s)" % (i, j.get("last_touch"), j.get("created")))
    if bad_json or bad_order:
        return FAIL, "lines=%d bad-JSON=%s out-of-order=%s" % (n, ",".join(bad_json) or "-",
                                                               ",".join(bad_order) or "-")
    return PASS, "lines=%d" % n


def check(plan_path, ledger_path, prereg_dir, explain=False):
    try:
        hay = ledger_haystack(ledger_path)
    except Exception:
        hay = ""
    r1_v, r1_d, r1_rows = rule_r1(plan_path, hay)
    r2_v, r2_d = rule_r2(prereg_dir)
    r3_v, r3_d = rule_r3(ledger_path)
    results = [("R1 LEDGER_COVERAGE", r1_v, r1_d, r1_rows),
               ("R2 PRE_REG_PRECEDES", r2_v, r2_d, []),
               ("R3 LEDGER_CONSISTENCY", r3_v, r3_d, [])]
    lines = []
    for name, v, d, rows in results:
        lines.append("%s %s   %s" % (name, v, d))
        if explain:
            lines.extend(rows)
    verdicts = [v for _n, v, _d, _r in results]
    if FAIL in verdicts:
        rc = 1
    elif UNDECIDED in verdicts:
        rc = 2
    else:
        rc = 0
    lines.append("SUMMARY rules=%d PASS=%d FAIL=%d UNDECIDABLE=%d rc=%d" % (
        len(verdicts), verdicts.count(PASS), verdicts.count(FAIL),
        verdicts.count(UNDECIDED), rc))
    return rc, lines


def main():
    # This tool prints text taken from the repository (paths, messages, samples). A Windows
    # console defaults to a legacy code page: one character it cannot encode would abort the
    # check with UnicodeEncodeError instead of printing a verdict. Escape what it cannot carry.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:                       # stdout replaced (StringIO) or already wrapped
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=DEF_PLAN)
    ap.add_argument("--ledger", default=DEF_LEDGER)
    ap.add_argument("--prereg-dir", default=DEF_PREREG)
    ap.add_argument("--explain", action="store_true",
                    help="list each task's artifact references and the tokens that hit (to debug why R1 is red/green)")
    a = ap.parse_args()
    try:
        rc, lines = check(a.plan, a.ledger, a.prereg_dir, explain=a.explain)
    except Exception as e:
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rules=0 rc=3")
        return 3
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
