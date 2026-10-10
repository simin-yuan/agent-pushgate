#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_pushscope_gate.py -- prove the "commit messages about to be pushed" gate can say
yes and can say no.

How: build a **local bare repo** in temp as the remote (no network, does not read the live
repo, does not depend on _local/), stage seven states with real git, and drive each one through
pushscope_gate's **real path** (argv -> resolve range -> decide -> print -> exit code), no
mocks.

  A clean unpushed messages           -> rc 0 / commits=1 (it ran, not silence)
  B drive path in a message           -> rc 1, names the sha and the rule; the output must
    **not** contain the sample's literal text (masked)
  F same state + --show-samples       -> rc 1, the sample literal **must** appear (both halves
    of masking; a half is a dead rule)
  C term-list word in a message       -> rc 1 / L2-term
  D a bad message already upstream (pushed) -> rc 0 (range = to be pushed; what is already
    pushed is not this push's risk)
  E upstream unresolvable             -> rc 2 UNDECIDABLE (must not be read as pass)
  G term list unreadable              -> rc 2 (not checked is not clean); the output must not
    contain a **machine-absolute path**

Bad samples are **built by concatenation, not written literally in the source**: this file is
itself inside the tree that leakscan scans, so writing them literally would make the
neighbouring gate refuse to publish. `no_literal_sample_in_source()` turns that into a check
that can fail. (History: the first version of `pushscope_gate.py` wrote the git candidate as a
literal install path -- drive letter + backslash -- and was immediately failed by that tree
gate; writing that lesson into a comment failed it a second time. So even the sample's shape is
not reproduced here. Case G guards the output side.)

Usage
    python verify/selftest_pushscope_gate.py [--target PATH] [--git PATH]
Exit code 0 = everything as expected / 1 = a case did not match / 3 = environment shortfall
(cannot run, not counted as pass)
"""
import argparse
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

FAKE_TERM = "zz" + "probe" + "term"
DRIVE_BAD = "Q:" + "/probe/leak/" + "x.txt"


def load(target):
    spec = importlib.util.spec_from_file_location("pushscope_gate_target", target)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load target module: %s" % target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(gitp, cwd, args, check=True):
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    r = subprocess.run([gitp, "-c", "user.name=selftest",
                        "-c", "user.email=selftest@example.invalid",
                        "-c", "commit.gpgsign=false"] + args,
                       cwd=cwd, capture_output=True, text=True, env=env, timeout=120,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError("git %s rc=%d %s %s" % (args[:2], r.returncode,
                                                   (r.stdout or "")[-200:], (r.stderr or "")[-300:]))
    return r


def run_check(mod, work, gitp, extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--repo", work, "--git", gitp] + list(extra))
    return rc, buf.getvalue()


def no_literal_sample_in_source():
    """Turn "the fixture must not be written literally in the source" into a check that can
    fail, instead of relying on memory."""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    bad = [s for s in (FAKE_TERM, DRIVE_BAD) if s in src]
    if bad:
        print("  [MISS] the self-test source contains the literal bad sample %r -- leakscan would refuse to publish this file" % (bad,))
        return False
    print("  [PASS] the self-test source contains no literal bad sample (built by concatenation)")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.path.join(HERE, "pushscope_gate.py"))
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    import pushscope_gate as ref                                # noqa: E402
    gitp = a.git or ref.find_git()
    if not gitp:
        print("RUNNER NOT READY   no git found -- in this situation a pass must not be reported")
        print("SUMMARY cases=0 pass=0 fail=0 rc=3")
        return 3

    mod = load(a.target)
    root = tempfile.mkdtemp(prefix="pushscope_selftest_")
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    cases, failures = [], []
    ok_src = [no_literal_sample_in_source()]

    def expect(name, got_rc, want_rc, out, must_contain=(), must_absent=()):
        miss = [s for s in must_contain if s not in out]
        there = [s for s in must_absent if s in out]
        ok = (got_rc == want_rc) and not miss and not there
        cases.append((name, got_rc, want_rc, ok))
        if not ok:
            failures.append("%s: rc=%s(want %s) missing=%r unexpected=%r\n%s"
                            % (name, got_rc, want_rc, miss, there, out[-500:]))
        return ok

    def commit_file(txt, msg):
        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", msg])
        return git(gitp, work, ["rev-parse", "HEAD"]).stdout.strip()

    try:
        git(gitp, root, ["init", "-q", "--bare", "-b", "main", origin])
        git(gitp, root, ["init", "-q", "-b", "main", work])
        # A **relative path** as the remote URL: an absolute path gets mangled by
        # file:// / drive-letter / MSYS translation
        git(gitp, work, ["remote", "add", "origin",
                         os.path.relpath(origin, work).replace("\\", "/")])
        os.makedirs(os.path.join(work, "_local"), exist_ok=True)
        with open(os.path.join(work, "_local", "privterms.txt"), "w", encoding="utf-8") as fh:
            fh.write("# synthetic term list (self-test only, not live)\n" + FAKE_TERM + "\n")
        commit_file("1", "base one")
        git(gitp, work, ["push", "-q", "-u", "origin", "main"])

        # ---- A clean unpushed messages: must be green, and must report how many it scanned ----
        commit_file("2", "chore: selftest clean commit")
        rc, out = run_check(mod, work, gitp)
        expect("A clean unpushed messages", rc, 0, out, ["upstream=origin/main", "commits=1", "rc=0"])

        # ---- B drive path in a message: must go red, name the sha and rule, and not echo the literal ----
        sha_bad = commit_file("3", "chore: leak at " + DRIVE_BAD)
        rc, out = run_check(mod, work, gitp)
        expect("B drive path in a message (should go red)", rc, 1, out,
               ["S1-drive-path", sha_bad[:12], "commits=2", "rc=1"],
               must_absent=[DRIVE_BAD])
        expect("B masking is stated (not just silence)", rc, 1, out, ["masked len="])

        # ---- F the other half of masking: with --show-samples the literal must appear ----
        rc, out = run_check(mod, work, gitp, ["--show-samples"])
        expect("F --show-samples echoes the literal", rc, 1, out, [DRIVE_BAD, "rc=1"])

        # ---- C term-list word in a message: must go red ----
        commit_file("4", "chore: approved by " + FAKE_TERM)
        rc, out = run_check(mod, work, gitp)
        expect("C term-list word in a message (should go red)", rc, 1, out, ["L2-term", "rc=1"])

        # ---- D after pushing: the bad message is upstream -> out of range ----
        git(gitp, work, ["push", "-q", "origin", "main"])
        rc, out = run_check(mod, work, gitp)
        expect("D bad message already upstream (out of range, expect green)", rc, 0, out, ["commits=0", "rc=0"])

        # ---- E upstream unresolvable: must be UNDECIDABLE, must not be read as pass ----
        rc, out = run_check(mod, work, gitp, ["--upstream", "refs/remotes/origin/nope"])
        expect("E upstream unresolvable (expect 2)", rc, 2, out, ["UNDECIDABLE", "rc=2"])
        if rc == 0:
            failures.append("E: an unknown was read as a pass (rc=0)")

        # ---- G term list unreadable: not checked is not clean ----
        commit_file("5", "chore: selftest clean again")
        rc, out = run_check(mod, work, gitp,
                            ["--terms", os.path.join(root, "absent-terms.txt")])
        expect("G missing term list = UNDECIDABLE (must not PASS)", rc, 2, out,
               ["terms unreadable", "rc=2", "absent-terms.txt"],
               must_absent=[root, root.replace("\\", "/")])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = len(cases) - len(failures)
    print("\n  %-34s %s" % ("self-test source has no literal bad sample", "OK" if ok_src[0] else "**MISMATCH**"))
    for name, got, want, ok in cases:
        print("  %-34s rc=%-6s want %-6s %s" % (name, got, want, "OK" if ok else "**MISMATCH**"))
    for f in failures:
        print("  FAIL %s" % f)
    total_fail = len(failures) + (0 if ok_src[0] else 1)
    print("SUMMARY cases=%d pass=%d fail=%d target=%s rc=%d"
          % (len(cases) + 1, len(cases) + 1 - total_fail, total_fail,
             os.path.basename(a.target), 1 if total_fail else 0))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
