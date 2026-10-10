#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_prepush_hook.py -- self-test for the pre-push gate (everything inside a temp fixture
repo; does not touch this repo)

Can the checks produce a negative result: yes. Every case is a **real git push** and only two
pieces of hard evidence are accepted:
  1. did the remote branch ref move (`git rev-parse main` on the bare remote)
  2. did the gate's trace log (<fixture>/.git/prepush_gate.log) get a new line, and what rc
It does not look at what the code says, nor at what a person says.

Cases
  A clean path        clean commit -> push succeeds, remote ref == local HEAD (the gate must not
                      falsely block)
  B dirty tree        a literal term from the term list in the committed body -> refused, remote
                      ref does not move, log rc=1
  C dirty message     a drive-path shape in the commit message -> refused, remote ref does not move
  D missing term list no term list -> the three gates UNDECIDABLE(2) -> refuse ("not checked" may
                      not be treated as "clean")
  E no PATH + sidecar -> the gate still runs (new log line rc=0), push succeeds (cron-style env)
  F no PATH and no sidecar -> interpreter not found -> refuse (fail-closed, not a silent pass)
  G --no-verify bypass -> **can bypass**: the remote moves, the log gets no new line (measure the
                      hole, do not pretend it does not exist)
  H installed then edited -> install --check must be rc=1 (content drift)
  I core.hooksPath redirected -> install --check must be rc=1 (installing amounts to not installing)

Usage: python verify/selftest_prepush_hook.py   -> last line N/N, all pass rc=0.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
import pushscope_gate                                              # noqa: E402

GIT = pushscope_gate.find_git()
#: bad samples are built by **concatenation**, never written literally -- this file is inside
#: this gate's scan surface too.
SHAPE = "E" + ":" + "/" + "tmp" + "/" + "sample.txt"
TERM = "zq" + "wflagterm"
ENV = dict(os.environ)
ENV.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GIT_ADVICE": "0"})


class Res:
    def __init__(self):
        self.n = 0
        self.fails = []

    def ok(self, cond, label, detail=""):
        self.n += 1
        if not cond:
            self.fails.append("%s | %s" % (label, detail))
        print("  %s  %s%s" % ("PASS" if cond else "FAIL", label,
                              ("  [" + detail + "]") if (detail and not cond) else ""))


def git(args, cwd, env=None, check=True):
    r = subprocess.run([GIT] + list(args), cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env or ENV, timeout=180)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), (r.stderr or "")[-400:]))
    return r


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def build(root, name, with_terms=True):
    """Build a fixture repo: bare remote + work repo (with this repo's gates and installer); the
    baseline is pushed **before the hook is installed**."""
    remote = os.path.join(root, name + "-remote.git")
    work = os.path.join(root, name + "-work")
    git(["init", "--bare", "-b", "main", remote], cwd=root)
    os.makedirs(work)
    git(["init", "-b", "main"], cwd=work)
    git(["config", "user.email", "selftest@example.invalid"], cwd=work)
    git(["config", "user.name", "selftest"], cwd=work)
    shutil.copytree(os.path.join(SRC, "verify"), os.path.join(work, "verify"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(work, "tools", "git-hooks"))
    for rel in (os.path.join("tools", "install_git_hooks.py"),
                os.path.join("tools", "git-hooks", "pre-push")):
        shutil.copyfile(os.path.join(SRC, rel), os.path.join(work, rel))
    write(os.path.join(work, ".gitignore"), "_local/\n")
    if with_terms:
        write(os.path.join(work, "_local", "privterms.txt"),
              "# fixture term list (synthetic, not a real term)\n" + TERM + "\n")
    write(os.path.join(work, "README.md"), "fixture\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "baseline"], cwd=work)
    git(["remote", "add", "origin", remote], cwd=work)
    git(["push", "-u", "origin", "main"], cwd=work)
    return work, remote


def install(work):
    return subprocess.run([sys.executable, os.path.join(work, "tools", "install_git_hooks.py")],
                          cwd=work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=180)


def check(work):
    return subprocess.run([sys.executable, os.path.join(work, "tools", "install_git_hooks.py"),
                           "--check"],
                          cwd=work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=180)


def remote_head(remote):
    r = git(["rev-parse", "main"], cwd=remote, check=False)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def local_head(work):
    return (git(["rev-parse", "HEAD"], cwd=work).stdout or "").strip()


def log_lines(work):
    p = os.path.join(work, ".git", "prepush_gate.log")
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8", errors="replace") as fh:
        return [l for l in fh.read().splitlines() if l.strip()]


def strip_path(root):
    """A cron-style environment: **no python** on PATH (nothing else is changed; git works as
    usual).

    Method: drop the directories of the original PATH that contain python/python3/py. This is
    closer to the question being asked (can the gate come up via its sidecar without python on
    PATH) than clearing the whole PATH, and it does not cripple git itself (measured: with PATH
    cleared or reduced to just the git layer, git itself broke, and that red says nothing about
    the gate).
    """
    parts = [p for p in (ENV.get("PATH") or "").split(os.pathsep) if p]
    keep = [p for p in parts if not path_has_python(p)]
    env = dict(ENV)
    env["PATH"] = os.pathsep.join(keep)
    env["Path"] = os.pathsep.join(keep)
    return env, keep


def path_has_python(path):
    for name in ("python", "python3", "py"):
        if shutil.which(name, path=path):
            return True
    return False


def last_log_rc(work):
    """-> the rc on the last trace line (None when there is no log)."""
    ls = log_lines(work)
    if not ls:
        return None
    m = re.search(r"rc=(\d+)", ls[-1])
    return int(m.group(1)) if m else None


def push(work, env=None, extra=()):
    return git(["push"] + list(extra) + ["origin", "main"], cwd=work, env=env, check=False)


def case_a(root, res):
    work, remote = build(root, "a")
    install(work)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add a clean note"], cwd=work)
    r = push(work)
    res.ok(r.returncode == 0, "A1 clean range pushes fine", (r.stderr or "")[-200:])
    res.ok(remote_head(remote) == local_head(work), "A2 remote ref advanced to local HEAD")
    res.ok(last_log_rc(work) == 0, "A3 trace log rc=0 (the gate really ran for this push)",
           "rc=%s" % last_log_rc(work))


def case_b(root, res):
    work, remote = build(root, "b")
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "leak.txt"), "harmless prefix " + TERM + " suffix\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add sample"], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "B1 literal term in the tree -> push refused", "rc=%s" % r.returncode)
    res.ok("REFUSED" in (r.stderr or ""), "B2 refusal reason appears on stderr")
    res.ok(remote_head(remote) == before, "B3 remote ref did not move")
    res.ok(last_log_rc(work) == 1, "B4 trace log rc=1", "rc=%s" % last_log_rc(work))


def case_c(root, res):
    work, remote = build(root, "c")
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: note about " + SHAPE], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "C1 drive-path shape in the commit message -> push refused", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == before, "C2 remote ref did not move")
    res.ok(last_log_rc(work) == 1, "C3 trace log rc=1", "rc=%s" % last_log_rc(work))


def case_d(root, res):
    work, remote = build(root, "d", with_terms=False)
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add a clean note"], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "D1 no term list -> refuse (not checked != clean)", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == before, "D2 remote ref did not move")
    res.ok(last_log_rc(work) == 2, "D3 trace log rc=2 (UNDECIDABLE)", "rc=%s" % last_log_rc(work))


def case_e(root, res):
    work, remote = build(root, "e")
    install(work)
    env, keep = strip_path(root)
    res.ok(not path_has_python(os.pathsep.join(keep)), "E0 precondition: this PATH really has no python",
           "keep=%d entries" % len(keep))
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work, env=env)
    git(["commit", "-m", "docs: add a clean note"], cwd=work, env=env)
    r = push(work, env=env)
    ls = log_lines(work)
    res.ok(r.returncode == 0, "E1 still pushes fine with no python/git on PATH (via the sidecar)",
           (r.stderr or "")[-200:])
    res.ok(remote_head(remote) == local_head(work), "E2 remote ref advanced")
    res.ok(last_log_rc(work) == 0, "E3 the gate really ran (trace rc=0)", "rc=%s" % last_log_rc(work))


def case_f(root, res):
    work, remote = build(root, "f")
    install(work)
    os.remove(os.path.join(work, ".git", "hooks", "pre-push.python"))
    env, keep = strip_path(root)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work, env=env)
    git(["commit", "-m", "docs: add a clean note"], cwd=work, env=env)
    r = push(work, env=env)
    res.ok(r.returncode != 0, "F1 interpreter not found -> refuse (fail-closed)", "rc=%s" % r.returncode)
    res.ok("interpreter" in (r.stderr or "") or "REFUSED" in (r.stderr or ""), "F2 refusal reason appears on stderr")
    res.ok(remote_head(remote) == before, "F3 remote ref did not move")
    res.ok(not log_lines(work), "F4 log is empty (the gate really did not start; not disguised as having run)")


def case_g(root, res):
    work, remote = build(root, "g")
    install(work)
    before_lines = len(log_lines(work))
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: note about " + SHAPE], cwd=work)
    r = push(work, extra=("--no-verify",))
    res.ok(r.returncode == 0, "G1 --no-verify bypasses it (the hole is proven, not assumed)", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == local_head(work), "G2 the dirty commit message went out anyway")
    res.ok(len(log_lines(work)) == before_lines, "G3 no new log lines -- the bypass leaves no trace, a known boundary")


def case_h(root, res):
    work, remote = build(root, "h")
    install(work)
    with open(os.path.join(work, ".git", "hooks", "pre-push"), "a", encoding="utf-8") as fh:
        fh.write("# tampered\n")
    r = check(work)
    res.ok(r.returncode == 1, "H1 installed hook tampered -> --check rc=1", "rc=%s stdout=%s"
           % (r.returncode, (r.stdout or "")[-160:].replace("\n", " / ")))


def case_i(root, res):
    work, remote = build(root, "i")
    install(work)
    other = os.path.join(root, "i-otherhooks")
    os.makedirs(other, exist_ok=True)
    git(["config", "core.hooksPath", other], cwd=work)
    r = check(work)
    res.ok(r.returncode == 1, "I1 core.hooksPath redirected -> --check rc=1 (installing amounts to not installing)",
           "rc=%s stdout=%s" % (r.returncode, (r.stdout or "")[-160:].replace("\n", " / ")))


def main():
    if not GIT:
        print("ERROR: no git found, cannot run the fixtures")
        return 3
    res = Res()
    root = tempfile.mkdtemp(prefix="prepush-selftest-")
    try:
        print("fixture root: %s (deleted on exit)" % os.path.basename(root))
        for fn in (case_a, case_b, case_c, case_d, case_e, case_f, case_g, case_h, case_i):
            print("- %s" % fn.__name__)
            try:
                fn(root, res)
            except Exception as e:                                 # noqa: BLE001
                res.ok(False, "%s raised an exception" % fn.__name__, "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nSUMMARY %d/%d" % (res.n - len(res.fails), res.n))
    for f in res.fails:
        print("  FAILED %s" % f)
    return 0 if not res.fails else 1


if __name__ == "__main__":
    sys.exit(main())
