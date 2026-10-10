#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_history_gate.py -- prove the "content-of-the-range-about-to-be-pushed" gate can
say yes and can say no.

How: build a **local bare repo** in temp as the remote (no network, does not read the live
repo, does not depend on _local/), stage real states with real git, and drive each one through
history_gate's **real path** (argv -> resolve range -> decide -> print -> exit code), no mocks.

  A clean range                                  -> rc 0 / commits=1 (it ran, not silence)
  B **a middle commit introduces a dirty blob, a later commit deletes it** -> rc 1, names S1
    and the committing shas
      - on the same fixture `leakscan.scan()` (the tree gate) **must be rc 0** -- this is the
        reason this gate exists: tree clean + messages clean, yet that blob is still sent by
        this push.
      - the output must not contain the sample's literal text (masked)
  F same state + --show-samples                  -> rc 1, the sample literal **must** appear
    (both halves of masking)
  C a term-list word in a middle blob            -> rc 1 / L2-term
  H a binary blob (contains NUL)                 -> skipped and counted (this tool does not
    inspect data embedded in binaries)
  D after pushing (already upstream)             -> rc 0 / commits=0 (range = to be pushed;
    what is already pushed is not this push's risk)
  E upstream unresolvable                        -> rc 2 UNDECIDABLE (must not be read as pass)
  G term list unreadable                         -> rc 2 (not checked is not clean); the output
    must not contain a **machine-absolute path**

Bad samples are **built by concatenation, not written literally in the source**: this file is
itself inside the tree that leakscan scans, so writing them literally would make the
neighbouring gate refuse to publish. `no_literal_sample_in_source()` turns that into a check
that can fail.

Usage
    python verify/selftest_history_gate.py [--target PATH] [--git PATH]
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
    spec = importlib.util.spec_from_file_location("history_gate_target", target)
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
    ap.add_argument("--target", default=os.path.join(HERE, "history_gate.py"))
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    tdir = os.path.dirname(os.path.abspath(a.target))
    sys.path.insert(0, tdir)
    import pushscope_gate as pg                                  # noqa: E402
    import leakscan as ls                                        # noqa: E402
    gitp = a.git or pg.find_git()
    if not gitp:
        print("RUNNER NOT READY   no git found -- in this situation a pass must not be reported")
        print("SUMMARY cases=0 pass=0 fail=0 rc=3")
        return 3

    mod = load(a.target)
    root = tempfile.mkdtemp(prefix="history_selftest_")
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    cases, failures = [], []
    ok_src = [no_literal_sample_in_source()]

    def check(name, got_rc, want_rc, out, must_contain=(), must_absent=()):
        miss = [s for s in must_contain if s not in out]
        there = [s for s in must_absent if s in out]
        ok = (got_rc == want_rc) and not miss and not there
        cases.append((name, got_rc, want_rc, ok))
        if not ok:
            failures.append("%s: rc=%s(want %s) missing=%r unexpected=%r\n%s"
                            % (name, got_rc, want_rc, miss, there, out[-600:]))
        return ok

    def write(rel, txt, mode="w"):
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if mode == "wb":
            with open(p, "wb") as fh:
                fh.write(txt)
        else:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
        return p

    def commit(msg):
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", msg])
        return git(gitp, work, ["rev-parse", "HEAD"]).stdout.strip()

    try:
        git(gitp, root, ["init", "-q", "--bare", "-b", "main", origin])
        git(gitp, root, ["init", "-q", "-b", "main", work])
        # A **relative path** as the remote URL: an absolute path gets mangled by
        # file:// / drive-letter / MSYS translation.
        git(gitp, work, ["remote", "add", "origin",
                         os.path.relpath(origin, work).replace("\\", "/")])
        write("_local/privterms.txt", "# synthetic term list (self-test only, not live)\n" + FAKE_TERM + "\n")
        write("f.txt", "base one\n")
        commit("base one")
        git(gitp, work, ["push", "-q", "-u", "origin", "main"])

        # ---- A clean range: must be green, and must report what it scanned (ran != silence) ----
        write("f.txt", "clean two\n")
        commit("chore: selftest clean commit")
        rc, out = run_check(mod, work, gitp)
        check("A clean range (expect green)", rc, 0, out,
              ["upstream=origin/main", "commits=1", "blobs=", "rc=0"])

        # ---- B core: reference and deletion in two separate commits -> tree clean, this gate must go red ----
        p = write("notes.md", "scratch: " + DRIVE_BAD + "\n")
        sha_add = commit("chore: add scratch note")
        os.remove(p)
        commit("chore: drop scratch note")
        trc, _tlines = ls.scan(work)                    # tree gate: must be green on the same fixture
        check("B tree gate must stay green on the same fixture (the divide)", trc, 0, "leakscan rc=%d\n" % trc)
        rc, out = run_check(mod, work, gitp)
        check("B dirty blob in a middle commit (should go red)", rc, 1, out,
              ["S1-drive-path", "notes.md", "only in a middle commit", "touched by=", "commits=3", "rc=1"],
              must_absent=[DRIVE_BAD])
        check("B masking is stated (not just silence)", rc, 1, out, ["masked len="])

        # ---- F the other half of masking: with --show-samples the literal must appear ----
        rc, out = run_check(mod, work, gitp, ["--show-samples"])
        check("F --show-samples echoes the literal", rc, 1, out, [DRIVE_BAD, "rc=1"])

        # ---- H binary blob: skipped and counted, must not be treated as "scanned" ----
        blob_bytes = (b"\x00\x01binary\x00 " + DRIVE_BAD.encode("utf-8") + b"\n\x00")
        p = write("blob.bin", blob_bytes, mode="wb")
        commit("chore: add opaque blob")
        os.remove(p)
        commit("chore: drop opaque blob")
        rc, out = run_check(mod, work, gitp)
        check("H binary skipped and counted", rc, 1, out, ["skipped 1 binary blob"])

        # ---- C a term-list word in a middle blob: must go red ----
        p = write("note2.md", "approved by " + FAKE_TERM + "\n")
        commit("chore: add note2")
        os.remove(p)
        commit("chore: drop note2")
        rc, out = run_check(mod, work, gitp)
        check("C term-list word in a middle blob (should go red)", rc, 1, out, ["L2-term", "rc=1"])

        # ---- D after pushing: the same dirty blob is upstream -> out of range ----
        git(gitp, work, ["push", "-q", "origin", "main"])
        rc, out = run_check(mod, work, gitp)
        check("D already-pushed is out of range (expect green)", rc, 0, out, ["commits=0", "rc=0"])

        # ---- E upstream unresolvable: must be UNDECIDABLE, must not be read as pass ----
        rc, out = run_check(mod, work, gitp, ["--upstream", "refs/remotes/origin/nope"])
        check("E upstream unresolvable (expect 2)", rc, 2, out, ["UNDECIDABLE", "rc=2"])

        # ---- G term list unreadable: not checked is not clean ----
        rc, out = run_check(mod, work, gitp,
                            ["--terms", os.path.join(root, "absent-terms.txt")])
        check("G missing term list = UNDECIDABLE (must not PASS)", rc, 2, out,
              ["terms unreadable", "rc=2", "absent-terms.txt"],
              must_absent=[root, root.replace("\\", "/")])
    finally:
        shutil.rmtree(root, ignore_errors=True)

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
