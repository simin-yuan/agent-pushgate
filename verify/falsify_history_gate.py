#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""falsify_history_gate.py -- mutation test against history_gate: **the self-test must go red
with it**

Why: a green self-test for a gate does not prove the gate is actually looking at anything --
it may be unwired, or always return 0 (verify discipline: a check you cannot make fail is
decoration). Method: copy the gate into a temp dir, inject three mutations **known to disable
it**, then run its own self-test:

| mutation | expectation |
|---|---|
| disabling the failure verdict (`rc = FAIL` unreachable) | self-test red on `B dirty blob in a middle commit` |
| never echoing ( `mask()` returns the sample ) | self-test red on `B masking is stated` |
| scanning already-pushed objects too (range changed to `HEAD`) | self-test red on `D already-pushed is out of range` |

The control (unmutated copy) must be green; **a mutation string that does not match = that
mutation was not verified**, which also counts as a failure.

Red must be **on the right case** (look at the case name, not the exit code) -- counting rc
alone would let "the module crashed on load" pass as "the gate was caught" (having run a
verification is not the same as the verification being of that thing).

**Runs in a temp dir, does not touch this repo**: the gate imports sibling modules (leakscan /
pushscope_gate), so those copies are placed into the temp dir as well, letting `HERE=tmp`
import them -- that way no mutated file ever appears in the repo.

Usage: python verify/falsify_history_gate.py [--repo PATH]
Exit code 0 = control green + all three mutations caught / 1 = a mutation was not caught or
not applied / 3 = environment shortfall
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
NEED = ["history_gate.py", "selftest_history_gate.py", "leakscan.py",
        "pushscope_gate.py", "bridge_landed.py"]

#: (mutation name, original text, replacement, the self-test case name that should go red)
MUTANTS = [
    ("disabling the failure verdict (rc always 0)",
     "    if hits:\n        rc = FAIL",
     "    if False:\n        rc = FAIL",
     "B dirty blob in a middle commit (should go red)"),
    ("never echoing (mask returns the sample)",
     'return "<<masked len=%d>>" % len(sample or "")',
     'return sample or ""',
     "B masking is stated (not just silence)"),
    ("scanning already-pushed objects too",
     '"rev-list", "--objects", "%s..HEAD" % ref',
     '"rev-list", "--objects", "HEAD"',
     "D already-pushed is out of range (expect green)"),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args(argv)
    vdir = os.path.join(os.path.abspath(a.repo), "verify")
    missing = [f for f in NEED if not os.path.isfile(os.path.join(vdir, f))]
    if missing:
        print("RUNNER NOT READY   missing files: %s" % ", ".join(missing))
        return 3

    tmp = tempfile.mkdtemp(prefix="falsify_history_")
    for f in NEED:
        shutil.copy2(os.path.join(vdir, f), os.path.join(tmp, f))
    pristine = os.path.join(tmp, "history_gate.pristine.py")
    shutil.copy2(os.path.join(tmp, "history_gate.py"), pristine)

    def run():
        p = subprocess.run([sys.executable, os.path.join(tmp, "selftest_history_gate.py"),
                            "--target", os.path.join(tmp, "history_gate.py")],
                           cwd=tmp, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    control_rc, out = run()
    print("control (unmutated)  rc=%d  %s" % (control_rc, "OK" if control_rc == 0 else "**MISMATCH**"))
    if control_rc != 0:
        print(out[-1500:])
        print("RUNNER NOT READY   the unmutated copy's self-test is not green, so a mutation test means nothing")
        return 3

    ok, total = 0, len(MUTANTS)
    src_path = os.path.join(tmp, "history_gate.py")
    for name, old, new, case_label in MUTANTS:
        with open(pristine, encoding="utf-8") as f:
            body = f.read()
        if old not in body:
            print("  [MISS] %-26s mutation string did not match (original changed?) -- this mutation was not verified" % name)
            continue
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body.replace(old, new, 1))
        rc, out = run()
        caught = (rc == 1) and (case_label in out)
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        print("  [%s] %-26s rc=%d  expect red on: %s" % ("OK" if caught else "MISS", name, rc, case_label))
        if not caught:
            print("        " + " | ".join(l.strip() for l in out.splitlines()[-4:]))
        ok += 1 if caught else 0

    print("SUMMARY falsify %d/%d (control rc=%d)" % (ok, total, control_rc))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
