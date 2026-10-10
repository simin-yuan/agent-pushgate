#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""falsify_prepush_hook.py -- falsify the check first, then trust it (the self-test itself must
be able to go red)

Apply one "break it" mutation to each of the three parts, then run the **corresponding** cases
against the mutated copy: the mutant must go red on the relevant cases, and the control
(unmutated) must be green. A check you cannot make fail is decoration.

  F1 tools/git-hooks/pre-push     the refusal branch returns 0 even when not letting through -> case_b/c/d must go red
  F2 verify/prepush_gate.py       UNDECIDABLE folded into PASS                          -> case_d must go red
  F3 tools/install_git_hooks.py   the content-drift check short-circuited                -> case_h must go red
  C  control: change nothing                                                            -> case_a must be green

Usage: python verify/falsify_prepush_hook.py  -> last line N/N, rc=0 means every mutation was
caught.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, os.pardir))
MUTATIONS = [
    ("F1 hook never refuses and never lets through",
     os.path.join("tools", "git-hooks", "pre-push"),
     '[ "$rc" -eq 0 ] || refuse "privacy gate returned non-zero',
     '[ "$rc" -eq 0 ] || true #'),
    ("F2 UNDECIDABLE folded into PASS",
     os.path.join("verify", "prepush_gate.py"),
     "    elif UNDECIDABLE in rcs:\n        out = UNDECIDABLE",
     "    elif UNDECIDABLE in rcs:\n        out = PASS"),
    ("F3 drift check short-circuited",
     os.path.join("tools", "install_git_hooks.py"),
     "    if isha != tsha:",
     "    if False:"),
]
CASES = {"F1": ["case_b", "case_c", "case_d"],
         "F2": ["case_d"],
         "F3": ["case_h"]}


def make_copy(tag):
    dst = os.path.join(tempfile.mkdtemp(prefix="falsify-%s-" % tag.lower()), "copy")
    os.makedirs(os.path.join(dst, "tools", "git-hooks"))
    shutil.copytree(os.path.join(SRC, "verify"), os.path.join(dst, "verify"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(os.path.join(SRC, "tools", "install_git_hooks.py"),
                    os.path.join(dst, "tools", "install_git_hooks.py"))
    shutil.copyfile(os.path.join(SRC, "tools", "git-hooks", "pre-push"),
                    os.path.join(dst, "tools", "git-hooks", "pre-push"))
    return dst


def load_mod(dst, tag):
    spec = importlib.util.spec_from_file_location(
        "st_%s" % tag, os.path.join(dst, "verify", "selftest_prepush_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cases(mod, names):
    root = tempfile.mkdtemp(prefix="falsify-run-")
    res = mod.Res()
    try:
        for n in names:
            try:
                getattr(mod, n)(root, res)
            except Exception as e:                                 # noqa: BLE001
                res.ok(False, "%s raised an exception" % n, "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return res


def main():
    print("control (change nothing)")
    cdir = make_copy("C")
    cmod = load_mod(cdir, "C")
    cres = run_cases(cmod, ["case_a"])
    ok_control = not cres.fails
    print("  %s  control case_a (%d assertions)" % ("PASS" if ok_control else "FAIL", cres.n))

    total, caught = 1, 1 if ok_control else 0
    for label, path, a, b in MUTATIONS:
        tag = label.split()[0]
        dst = make_copy(tag)
        target = os.path.join(dst, path)
        with open(target, encoding="utf-8") as fh:
            src = fh.read()
        if a not in src:
            print("  UNDECIDABLE  %s: the anchor to mutate was not found (the check itself drifted)" % label)
            total += 1
            continue
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace(a, b, 1))
        mod = load_mod(dst, tag)
        res = run_cases(mod, CASES[tag])
        total += 1
        if res.fails:
            caught += 1
            print("  PASS  %s -> the mutant was caught by %s (red in %d of %d assertions)"
                  % (label, "/".join(CASES[tag]), res.n, len(res.fails)))
        else:
            print("  FAIL  %s -> the mutant was not caught (this self-test is decoration)" % label)
        shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
    shutil.rmtree(os.path.dirname(cdir), ignore_errors=True)
    print("\nSUMMARY %d/%d" % (caught, total))
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
