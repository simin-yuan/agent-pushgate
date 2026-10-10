#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pushscope_gate.py -- the gate for the commit messages about to be pushed: `git push` sends
more than the tree, it sends every commit message too

Why it is needed
  `verify/leakscan.py` scans the **tree** (= the file content that is about to be `git add -A`
  committed and pushed). But a push sends **two things**: the tree, and the **message** of
  every commit in `<upstream>..HEAD`. Tree clean, message carrying a machine path -- the tree
  gate still reports LEAKS=0, and that push writes the path into GitHub's permanent history.
  Measured: the **title** of a data commit carried this machine's absolute path while the tree
  was green the whole time. The wiring has been traced end to end: the export step's
  privacy_gate() prints the gate's first line at the front, and the auto-commit takes
  `stdout.splitlines()[0]` to build the commit title.

Scope -- what this tool does **not** cover (without this section the checks get treated as
something they cannot support)
  - It scans only the messages of `<upstream>..HEAD`, **not the tree** (that is leakscan's job;
    the two are complementary, not overlapping).
  - It **does not scan commits already upstream**: what cannot be pushed is not this push's
    risk (case D guards this line).
  - It does not scan tag / branch names / reflog / stash / notes / PR bodies -- all of these
    are **unknown**, not "absent".
  - It does not judge semantic re-spellings (homophones, transliteration, abbreviations,
    splitting and concatenation) -- the same blind spot as leakscan.
  - **It does not echo the literal of a hit sample by default** (masked as a "length"): this
    gate's output is folded into the next commit message by the export step -- a gate that
    echoes the leak it caught is itself a leak source. To see the literal use `--show-samples`,
    only when the output reaches no publishing surface; for normal debugging use
    `git log -1 <sha>`.
  - It recognises shapes only (drive letter / UNC / AppData) plus the local term list; the
    term list lives in `_local/` (gitignored) and is absent on a clean clone.

Can the checks produce a negative result?
  `verify/selftest_pushscope_gate.py` builds its own local bare repo as the remote and stages
  one state per case, seven in total: clean messages (0) / drive path in a message (1) /
  term-list word in a message (1) / a bad message already upstream (0, out of range) /
  upstream unresolvable (2) / both halves of masking (masked by default - `--show-samples`
  echoes) / term list unreadable (2).
  `verify/falsify_pushscope_gate.py` then applies three mutations to this gate (failure verdict
  disabled / always echo / range includes already-pushed), and the self-test must go red with
  them -- a check you cannot make fail is decoration. It also runs on every push and pull
  request in CI (see .github/workflows/tests.yml).

Exit codes
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3. FAIL takes priority over UNDECIDABLE.
  "no git found" / "not a git repo" / "upstream unresolvable" / "term list unreadable" are all
  UNDECIDABLE -- could not check is not the same as clean.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def find_git(explicit=None):
    """Find git by trying candidates in turn, not via which (the cron service has no PATH).

    The candidates do **not** write a literal drive path: this file is in a public repo, and any
    "drive letter + backslash" shape would hit leakscan's S1 layer -- measured: the first
    version wrote the candidate as a literal install path and this gate went red on itself.
    The candidates are assembled from environment variables (ProgramFiles is a system-level
    variable, nssm's AppEnvironmentExtra **appends**, so it is still there in the service).
    """
    sys.path.insert(0, HERE)
    try:
        import bridge_landed                                   # noqa: E402
        g = bridge_landed.find_git(explicit)
        if g:
            return g
    except Exception:                                          # noqa: BLE001
        pass
    import shutil
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
               os.environ.get("ProgramFiles(x86)")):
        if pf:
            c = os.path.join(pf, "Git", "cmd", "git.exe")
            if os.path.isfile(c):
                return c
    return shutil.which("git")


def disp(path, root):
    """Keep only a relative path / last segment in the output. This gate's stdout is a
    **published surface**; an absolute path must not enter the output -- a rule that holds for
    the people who write the gates too."""
    if not path:
        return "(default)"
    try:
        r = os.path.relpath(path, root)
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return os.path.basename(path)


def run_git(gitp, repo, args, timeout=120):
    import subprocess
    return subprocess.run([gitp, "-C", repo] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def mask(sample):
    """The masked form of a hit sample: state its length, not its content."""
    return "<<masked len=%d>>" % len(sample or "")


def resolve_upstream(gitp, repo, upstream):
    """-> (ref, status, why). status: OK / UNDECIDABLE."""
    if upstream:
        ref = upstream
    else:
        r = run_git(gitp, repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        ref = (r.stdout or "").strip()
        if r.returncode != 0 or not ref or ref == "@{u}":
            ref = ""
            for cand in ("origin/main", "origin/master"):
                c = run_git(gitp, repo, ["rev-parse", "--verify", "--quiet", cand + "^{commit}"])
                if c.returncode == 0 and (c.stdout or "").strip():
                    ref = cand
                    break
            if not ref:
                return None, "UNDECIDABLE", ("no upstream reachable: @{u} is unset and neither "
                                             "origin/main nor origin/master exists locally")
    v = run_git(gitp, repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    if v.returncode != 0 or not (v.stdout or "").strip():
        return ref, "UNDECIDABLE", "upstream %s unresolvable (no such ref locally / not fetched)" % ref
    return ref, "OK", ""


def list_unpushed(gitp, repo, ref):
    """-> (records, err). records = [(sha, message)]; a non-empty err means it could not be read
    (not "there are none")."""
    r = run_git(gitp, repo, ["log", "--format=%H%x1f%B%x1e", "%s..HEAD" % ref])
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:]
    recs = []
    for chunk in (r.stdout or "").split("\x1e"):
        chunk = chunk.strip("\r\n \t")
        if "\x1f" not in chunk:
            continue
        sha, msg = chunk.split("\x1f", 1)
        recs.append((sha.strip(), msg))
    return recs, ""


def scan_messages(repo, upstream=None, terms_path=None, terms_required=True,
                  show_samples=False, gitp=None):
    """Callable in-process (the self-test and the export step both go through here -- cron has
    no PATH, so one less silently-failing link). -> (rc, lines)"""
    repo = os.path.abspath(repo)
    tag = "pushscope repo=...%s" % os.path.basename(repo)
    if terms_path is None:
        terms_path = os.path.join(repo, "_local", "privterms.txt")
    sys.path.insert(0, HERE)
    import leakscan as LS                                      # noqa: E402

    gitp = gitp or find_git()
    if not gitp:
        return UNDECIDABLE, [tag, "  UNDECIDABLE: no git found (cron has no PATH) -- not checked is not clean",
                             "SUMMARY upstream=- commits=? LEAKS=? rc=%d" % UNDECIDABLE]
    if not os.path.isdir(os.path.join(repo, ".git")):
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE: this is not a git repo (no .git) -- failing to see what would"
                             " be pushed is not the same as nothing being pushed",
                             "SUMMARY upstream=- commits=? LEAKS=? rc=%d" % UNDECIDABLE]

    ref, ustat, uwhy = resolve_upstream(gitp, repo, upstream)
    lines = ["%s upstream=%s range=%s..HEAD" % (tag, ref or "(none)", ref or "(none)")]
    if ustat != "OK":
        lines.append("  UNDECIDABLE: %s" % uwhy)
        lines.append("SUMMARY upstream=%s commits=? LEAKS=? rc=%d" % (ref or "-", UNDECIDABLE))
        return UNDECIDABLE, lines

    recs, err = list_unpushed(gitp, repo, ref)
    if recs is None:
        lines.append("  UNDECIDABLE: git log %s..HEAD failed: %s" % (ref, err))
        lines.append("SUMMARY upstream=%s commits=? LEAKS=? rc=%d" % (ref, UNDECIDABLE))
        return UNDECIDABLE, lines

    terms, tstat = LS.load_terms(terms_path) if terms_required else ([], "SKIPPED")

    hits = []
    for sha, msg in recs:
        found, seen = [], set()
        for rule, rx in LS.SHAPES:
            for m in rx.finditer(msg):
                found.append((rule, m.group(0)))
        for t in terms:
            for m in LS.term_re(t).finditer(msg):
                found.append((LS.TERM_RULE, m.group(0)))
        for rule, sample in found:
            k = (rule, LS.norm(sample))
            if k in seen:
                continue
            seen.add(k)
            hits.append((sha, rule, sample))

    for sha, rule, sample in hits:
        shown = sample[:60] if show_samples else mask(sample)
        lines.append("  LEAK   %s: msg %s -> %s" % (sha[:12], rule, shown))
    sh = [h for h in hits if h[1] in LS.SHAPE_RULES]
    lt = [h for h in hits if h[1] == LS.TERM_RULE]
    lines.append("C1 commit msg shape layer  commits=%d rules=%d hits=%d  %s"
                 % (len(recs), len(LS.SHAPES), len(sh), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("C2 commit msg literal layer  **NOT RUN** (terms_required=False): "
                     "this run's green does **not prove** the literal-term half")
    elif tstat == "MISSING":
        lines.append("C2 commit msg literal layer  terms unreadable (%s) -> UNDECIDABLE, not guessed as PASS"
                     % disp(terms_path, repo))
    else:
        lines.append("C2 commit msg literal layer  terms=%d hits=%d  %s"
                     % (len(terms), len(lt), "FAIL" if lt else "PASS"))
    if hits and not show_samples:
        lines.append("  note: hit samples are masked by default (length only) -- this gate's output is"
                     " folded into the next commit message. To see the literal: git log -1 <sha>.")

    if hits:
        rc = FAIL
    elif tstat == "MISSING":
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY upstream=%s commits=%d LEAKS=%d rc=%d" % (ref, len(recs), len(hits), rc))
    return rc, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gate for the commit messages about to be pushed (a publishing surface beyond the tree)")
    ap.add_argument("--repo", default=REPO, help="the repo to scan (default = the repo this script lives in)")
    ap.add_argument("--upstream", default=None,
                    help="upstream ref (default @{u}, then falling back to origin/main, origin/master)")
    ap.add_argument("--terms", default=None,
                    help="term list path. Default <repo>/_local/privterms.txt (gitignored)")
    ap.add_argument("--shape-only", action="store_true",
                    help="shape layer only (for a clean clone; the output states 'literal layer not run')")
    ap.add_argument("--show-samples", action="store_true",
                    help="echo the hit literals: **only when the output reaches no publishing surface**")
    ap.add_argument("--git", default=None, help="git executable path (cron has no PATH)")
    a = ap.parse_args(argv)
    try:
        rc, lines = scan_messages(a.repo, upstream=a.upstream, terms_path=a.terms,
                                  terms_required=not a.shape_only,
                                  show_samples=a.show_samples, gitp=a.git)
    except Exception as e:                                     # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=3")
        return ERROR
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
