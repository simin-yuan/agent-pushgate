#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""install_git_hooks.py -- install the repo's hook template into this machine's .git/hooks/,
and be able to **prove it is installed**

Why not "just write .git/hooks/pre-push": .git/ is not version-controlled -- what you write
there, nobody else can see, you yourself will forget, and no one can verify "the installed copy
is still the one in the repo". So it is split into three parts:
  template in the repo (reviewable, diffable) -> the installer lands it locally -> `--check`
  proves the two are byte-identical.

"Claim -> a check that can fail": --check goes red on four things --
  1. not installed; 2. installed but the content drifted (someone hand-edited the installed
  hook); 3. the interpreter / git sidecar is dead (the cron service has no PATH; without the
  sidecar the hook fail-closes and refuses every push);
  4. core.hooksPath points elsewhere -- set that and .git/hooks/ is ignored, so **installing it
  amounts to not installing it**.

Usage
    python tools/install_git_hooks.py            # install (idempotent); runs --check once afterwards
    python tools/install_git_hooks.py --check    # check only, does not write
    python tools/install_git_hooks.py --repo R   # specify the repo (default = the repo this script lives in)
Exit code PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
TEMPLATE_REL = os.path.join("tools", "git-hooks", "pre-push")
HOOK = "pre-push"
#: sidecar name -> purpose (written for the hook to read; the hook does not use PATH to find the
#: interpreter and git)
SIDECARS = {"pre-push.python": "python interpreter", "pre-push.git": "git executable"}
PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def find_git():
    """Reuse the gate's own candidate search (no which; cron has no PATH)."""
    sys.path.insert(0, os.path.join(REPO, "verify"))
    try:
        import pushscope_gate
        return pushscope_gate.find_git()
    except Exception:                                            # noqa: BLE001
        return shutil.which("git")


def paths(repo):
    hooks = os.path.join(repo, ".git", "hooks")
    return hooks, os.path.join(hooks, HOOK), os.path.join(repo, TEMPLATE_REL)


def check(repo):
    """-> (rc, lines). Read-only, writes nothing."""
    hooks, installed, template = paths(repo)
    lines = ["install_git_hooks --check repo=...%s" % os.path.basename(repo)]
    rc_notes = []
    if not os.path.isfile(template):
        lines.append("  UNDECIDABLE: no template in the repo: %s" % TEMPLATE_REL)
        lines.append("SUMMARY template=- installed=- rc=%d" % UNDECIDABLE)
        return UNDECIDABLE, lines
    tsha = sha256(template)
    if not os.path.isfile(installed):
        lines.append("  FAIL: not installed (%s does not exist)" % HOOK)
        lines.append("  template sha256=%s" % tsha[:16])
        lines.append("SUMMARY template=%s installed=- rc=%d" % (tsha[:16], FAIL))
        return FAIL, lines
    isha = sha256(installed)
    if isha != tsha:
        lines.append("  FAIL: installed but content drifted -- the installed copy is not the same file as the template")
        lines.append("  template sha256=%s / installed sha256=%s" % (tsha[:16], isha[:16]))
        rc_notes.append(FAIL)
    else:
        lines.append("  content matches template=installed sha256=%s" % tsha[:16])
    for name, why in SIDECARS.items():
        sp = os.path.join(hooks, name)
        if not os.path.isfile(sp):
            lines.append("  FAIL: missing sidecar %s (%s) -- with no PATH in cron the hook will refuse every push" % (name, why))
            rc_notes.append(FAIL)
            continue
        with open(sp, encoding="utf-8", errors="replace") as fh:
            val = fh.read().strip()
        if not val or not os.path.isfile(val):
            lines.append("  FAIL: sidecar %s points at a non-existent interpreter/program (%s)" % (name, why))
            rc_notes.append(FAIL)
        else:
            lines.append("  sidecar %s -> %s (%s)" % (name, os.path.basename(val), why))
    g = find_git()
    if not g:
        lines.append("  UNDECIDABLE: this process cannot find git, so core.hooksPath cannot be verified")
        rc_notes.append(UNDECIDABLE)
    else:
        r = subprocess.run([g, "-C", repo, "config", "--get", "core.hooksPath"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        hp = (r.stdout or "").strip()
        if hp:
            same = os.path.normcase(os.path.abspath(os.path.join(repo, hp))) == \
                os.path.normcase(os.path.abspath(hooks))
            if same:
                lines.append("  core.hooksPath=%s (points at this hooks directory, consistent)" % hp)
            else:
                lines.append("  FAIL: core.hooksPath=%s points elsewhere -- .git/hooks/ is ignored, so installing it amounts to not installing it" % hp)
                rc_notes.append(FAIL)
        else:
            lines.append("  core.hooksPath is unset (defaults to .git/hooks/, in effect)")
    if FAIL in rc_notes:
        rc = FAIL
    elif UNDECIDABLE in rc_notes:
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY template=%s installed=%s rc=%d" % (tsha[:16], isha[:16], rc))
    return rc, lines


def install(repo):
    hooks, installed, template = paths(repo)
    if not os.path.isfile(template):
        print("ERROR: no template in the repo: %s" % TEMPLATE_REL)
        return UNDECIDABLE
    os.makedirs(hooks, exist_ok=True)
    shutil.copyfile(template, installed)
    try:
        os.chmod(installed, 0o755)
    except OSError:
        pass
    # python sidecar: written with **forward slashes** -- the hook is a sh script, and a
    # backslash would be eaten as an escape.
    py = os.path.abspath(sys.executable).replace("\\", "/")
    with open(os.path.join(hooks, "pre-push.python"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(py + "\n")
    g = find_git()
    if g:
        with open(os.path.join(hooks, "pre-push.git"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(os.path.abspath(g).replace("\\", "/") + "\n")
    else:
        print("note: this process cannot find git, not writing pre-push.git (the hook passes that step back to the gate to find it)")
    print("installed %s" % installed)
    print("  template  sha256=%s bytes=%d" % (sha256(template), os.path.getsize(template)))
    print("  installed sha256=%s bytes=%d" % (sha256(installed), os.path.getsize(installed)))
    print("  sidecar   pre-push.python -> %s" % os.path.basename(py))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install/check git hooks (the template is in the repo, the installed copy is provable)")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if not a.check:
        install(a.repo)
    rc, lines = check(a.repo)
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
