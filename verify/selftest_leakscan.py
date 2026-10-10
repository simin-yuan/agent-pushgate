#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_leakscan.py -- falsify this gate before trusting it

Rule one: the control group must be **hand-made**, never a copy of the live tree -- a control
that sinks together with the thing under test is not a control. So every case below builds a
small tree in temp, and **never reads this repo's real tree**.
Rule two: a check that cannot go red is decoration. S1/S2/S3/L2 each get one bad sample that
must turn red, plus one "must not go red" false-positive control (a URL scheme separator is
not a drive path).
Rule three: the checks must be able to yield a negative result -- the `--shape-only` case
asserts that the output contains **no** L2 hit, declared with want_absent rather than just
looking at rc.

No subprocess (cron has no PATH): everything calls `leakscan.scan()` in-process.
This file is used by `the mutation (falsify) test` to turn the gate itself red (swap LS for an
always-green mutant -> this self-test must report FAILED).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leakscan as LS  # noqa: E402

# Bad samples. Their presence in this file is **deliberate**: unless they live in the
# self-test, there is no way to prove the gate turns red.
UNC_SAMPLE = "mounted at \\\\fileserver\\share\\notes today\n"
DRIVE_SAMPLE = "the artifact lands at E:/build/out/report.json\n"
APPDATA_SAMPLE = "config lives under AppData/Local/Temp/x\n"
TERM_SAMPLE = "this page is published after ZZPRIV approval\n"
CLEAN = "# Notes\n\nnothing operator-specific in here.\n"
URL_ONLY = "see https://github.com/example/example for the mirror\n"
PLACEHOLDER = "synthetic case path E:/x/y/z.md, used only to explain the check\n"


def tree(d, files):
    for rel, text in files.items():
        p = os.path.join(d, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


def write_terms(d, terms):
    p = os.path.join(d, "_local", "privterms.txt")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# synthetic term list\n" + "\n".join(terms) + "\n")
    return p


def run_case(d, files, terms=("zzpriv",), terms_missing=False, shape_only=False,
             allow=None):
    """Build a tree in d and scan it once. terms_missing=True points at a list that is not
    there."""
    tree(d, files)
    if terms_missing:
        tp = os.path.join(d, "_local", "absent.txt")
    else:
        tp = write_terms(d, terms)
    return LS.scan(d, terms_path=tp, terms_required=not shape_only, allow=allow)


CASES = []


def expect(name, want_rc, subs=(), absent=(), **kw):
    CASES.append((name, want_rc, tuple(subs), tuple(absent), kw))


# A hand-made clean control: must be all green (and zero exemptions -- allow={} proves a clean
# tree does not need an allowlist to pass)
expect("A hand-made clean control (expect all green)", 0,
       ["L1 shape layer", "hits=0  PASS", "L2 literal layer", "rc=0"], files={"README.md": CLEAN},
       allow={})

# B false-positive control: a URL scheme separator is not a drive path, **must not go red**
expect("B URL must not false-positive", 0, ["hits=0  PASS", "rc=0"],
       files={"README.md": CLEAN, "docs/x.md": URL_ONLY}, allow={})

# C S1 drive path -> red
expect("C S1 drive path (should go red)", 1, ["S1-drive-path", "LEAK", "L1 shape layer", "FAIL"],
       files={"README.md": CLEAN, "docs/x.md": DRIVE_SAMPLE}, allow={})

# D S2 UNC -> red
expect("D S2 UNC path (should go red)", 1, ["S2-unc-path", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": UNC_SAMPLE}, allow={})

# E S3 profile-internal -> red
expect("E S3 profile-internal (should go red)", 1, ["S3-profile-internal", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": APPDATA_SAMPLE}, allow={})

# F L2 literal term (prose in uppercase, list in lowercase -> proves case-insensitivity) -> red
expect("F L2 literal term (should go red)", 1, ["L2-term", "L2 literal layer  terms=1 hits=1  FAIL", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": TERM_SAMPLE}, allow={})

# G term list unreadable = UNDECIDABLE, must not be guessed as PASS
expect("G missing term list = UNDECIDABLE (must not PASS)", 2,
       ["terms unreadable", "UNDECIDABLE", "rc=2"],
       files={"README.md": CLEAN}, terms_missing=True, allow={})

# H the exemption mechanism is not a blanket pass: same placeholder path, red without an
# exemption, green with one
expect("H1 same placeholder path - no exemption (should go red)", 1, ["S1-drive-path", "rc=1"],
       files={"doc.md": PLACEHOLDER}, allow={})
expect("H2 same placeholder path - with exemption (should stay green)", 0, ["allowlisted=1", "rc=0"],
       files={"doc.md": PLACEHOLDER},
       allow={"doc.md": {"e:/x/y/z.md": "synthetic placeholder, not a real machine path"}})

# I --shape-only: the literal layer did not run -> rc=0 but the output must contain **no** L2
# hit, and must state "not run"
expect("I shape-only does not prove the literal layer", 0,
       ["**NOT RUN**", "does **not prove**", "rc=0"], absent=["L2-term"],
       files={"docs/x.md": TERM_SAMPLE}, shape_only=True, allow={})


def no_abs_root_in_output():
    """Turn "this gate's output must not contain a machine-absolute path" into a check that can
    fail.

    Why it exists: this gate's stdout is a **published surface** -- the export step folds it
    into the next commit message (measured: this gate itself once printed root's absolute path
    into its own output).
    A gate that prints an absolute path as a summary line is a leak source itself. This check
    can go red: put `_rel` back to a bare path and it fires.
    """
    with tempfile.TemporaryDirectory(prefix="ow-leakscan-absroot-") as d:
        with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(CLEAN)
        tp = write_terms(d, ("zzpriv",))
        _rc, lines = LS.scan(d, terms_path=tp)
        blob = "\n".join(lines)
        leaked = [s for s in (d, d.replace("\\", "/"), tp, tp.replace("\\", "/"))
                  if s in blob]
        if leaked:
            print("  [MISS] the gate's output contains an absolute path: %r" % leaked)
            return False
        if os.path.basename(d) not in blob:
            print("  [MISS] the gate's output does not even contain the last segment: the check has degraded into 'saying nothing'")
            return False
    print("  [PASS] the gate's output contains no absolute root path / absolute term-list path (last segment and relative paths only)")
    return True


def main():
    fails = []
    extra = [no_abs_root_in_output()]
    with tempfile.TemporaryDirectory(prefix="ow-leakscan-") as root:
        for i, (name, want_rc, subs, absent, kw) in enumerate(CASES):
            d = os.path.join(root, "c%d" % i)
            os.makedirs(d, exist_ok=True)
            rc, lines = run_case(d, **kw)
            blob = "\n".join(lines)
            miss = [s for s in subs if s not in blob]
            unexpected = [s for s in absent if s in blob]
            ok = (rc == want_rc) and not miss and not unexpected
            print("[%s] %s  want_rc=%d got_rc=%d%s%s" % (
                "OK" if ok else "MISS", name, want_rc, rc,
                "" if not miss else "  missing=%s" % miss,
                "" if not unexpected else "  unexpected=%s" % unexpected))
            for ln in lines:
                print("      | " + ln)
            if not ok:
                fails.append(name)
    bad = len(fails) + (0 if extra[0] else 1)
    print("\nSELFTEST %s  cases=%d failed=%d" % (
        "OK" if not bad else "FAILED", len(CASES) + 1, bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
