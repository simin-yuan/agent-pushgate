#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_bridge_landed.py -- prove bridge_landed.py can say yes and can say no.

How: build a **local bare repo** in temp as the remote, stage four states with real git, and
drive each through bridge_landed's **real path** (argv -> get shas -> decide -> print -> exit
code), no mocks.

    A landed          commit + push                    -> rc 0 / LANDED
    B stuck           another commit (timestamp 5h ago) not pushed -> rc 1 / STALE
    C recovers        pushed                           -> rc 0 / LANDED (proves it is not always red)
    D within window   another commit (current time) not pushed -> rc 0 / PENDING
    E remote unreachable  remote points at a non-existent path -> rc 2 / UNDECIDABLE
    F is that 2 a pass?   explicitly assert E's rc != 0

Does not read the live repo, no network, does not depend on _local/. --target can point at a
**broken copy** of the module, so that "break the check first, the self-test must go red with
it" has somewhere to happen.

Usage
    python verify/selftest_bridge_landed.py [--target PATH] [--git PATH]
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
MAX_AGE_H = 2.0


def load(target):
    spec = importlib.util.spec_from_file_location("bridge_landed_target", target)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load target module: %s" % target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(gitp, cwd, args, env_extra=None, check=True):
    env = dict(os.environ)
    env.update(env_extra or {})
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    r = subprocess.run([gitp, "-c", "user.name=selftest",
                        "-c", "user.email=selftest@example.invalid",
                        "-c", "commit.gpgsign=false"] + args,
                       cwd=cwd, capture_output=True, text=True, env=env, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError("git %s rc=%d %s %s" % (args[:2], r.returncode,
                                                   r.stdout[-200:], r.stderr[-300:]))
    return r


def run_check(mod, work, gitp):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--repo", work, "--git", gitp, "--max-age-h", str(MAX_AGE_H)])
    return rc, buf.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.path.join(HERE, "bridge_landed.py"))
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    import bridge_landed as ref                                    # noqa: E402
    gitp = a.git or ref.find_git()
    if not gitp:
        print("RUNNER NOT READY   no git found -- in this situation a pass must not be reported")
        print("SUMMARY cases=0 fail=0 rc=3")
        return 3

    mod = load(a.target)
    root = tempfile.mkdtemp(prefix="bridge_landed_selftest_")
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    cases, failures = [], []

    def expect(name, got_rc, want_rc, out, must_contain):
        ok = (got_rc == want_rc) and all(s in out for s in must_contain)
        cases.append((name, got_rc, want_rc, ok))
        if not ok:
            failures.append("%s: rc=%s(want %s) missing %r\n%s"
                            % (name, got_rc, want_rc,
                               [s for s in must_contain if s not in out], out[-500:]))
        return ok

    try:
        git(gitp, root, ["init", "-q", "--bare", "-b", "main", origin])
        git(gitp, root, ["init", "-q", "-b", "main", work])
        # A **relative path** as the remote URL: an absolute path gets mangled here by
        # file:// / drive-letter / MSYS translation
        git(gitp, work, ["remote", "add", "origin",
                         os.path.relpath(origin, work).replace("\\", "/")])
        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("1\n")
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", "one"])
        git(gitp, work, ["push", "-q", "-u", "origin", "main"])

        rc, out = run_check(mod, work, gitp)                       # A
        expect("A landed", rc, 0, out, ["LANDED"])

        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("2\n")
        git(gitp, work, ["add", "-A"])
        # Stage the timestamp as "now - 5h", not a literal date: a literal date goes stale over
        # time (red today, not red tomorrow)
        git(gitp, work, ["commit", "-q", "-m", "two"],
            env_extra={"GIT_COMMITTER_DATE": _hours_ago(5)})
        rc, out = run_check(mod, work, gitp)                       # B
        expect("B stuck 5h", rc, 1, out, ["STALE"])

        git(gitp, work, ["push", "-q", "origin", "main"])
        rc, out = run_check(mod, work, gitp)                       # C
        expect("C recovers after push", rc, 0, out, ["LANDED"])

        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("3\n")
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", "three"])
        rc, out = run_check(mod, work, gitp)                       # D
        expect("D within tolerance window", rc, 0, out, ["PENDING"])

        git(gitp, work, ["remote", "set-url", "origin",
                         os.path.relpath(os.path.join(root, "nope.git"), work)
                         .replace("\\", "/")])
        rc, out = run_check(mod, work, gitp)                       # E
        expect("E remote unreachable", rc, 2, out, ["UNDECIDABLE"])
        if rc == 0:                                                # F
            failures.append("F: an unreachable remote was read as a pass (rc=0)")
            cases.append(("F unknown must not be read as pass", rc, "!=0", False))
        else:
            cases.append(("F unknown must not be read as pass", rc, "!=0", True))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = len(cases) - len(failures)
    for name, got, want, ok in cases:
        print("  %-22s rc=%-6s want %-6s %s" % (name, got, want, "OK" if ok else "**MISMATCH**"))
    for f in failures:
        print("  FAIL %s" % f)
    print("SUMMARY cases=%d pass=%d fail=%d target=%s rc=%d"
          % (len(cases), passed, len(failures), os.path.basename(a.target),
             1 if failures else 0))
    return 1 if failures else 0


def _hours_ago(h):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=h)).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    sys.exit(main())
