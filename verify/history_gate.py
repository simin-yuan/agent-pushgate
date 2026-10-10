#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""history_gate.py -- the **object-content** gate for the range about to be pushed: the third
thing a single push sends.

Why it is needed
  The publishing surface is already split in two, each with its own gate:
    1. tree        -- the file content at HEAD            -> verify/leakscan.py
    2. commit msg  -- the messages in `<upstream>..HEAD`  -> verify/pushscope_gate.py
  But `git push` sends the **whole new object set** (`git rev-list --objects
  <upstream>..HEAD`). So in the most ordinary case -- "a file carried a sensitive string in
  some middle commit and was later cleaned up" -- the tree gate is green (HEAD is clean) and
  the message gate is green (messages are clean), yet **that blob is still sent by this push**
  and written into GitHub's permanent history, which cannot be rewritten. This gate is that
  third thing.

Scope -- what this tool does **not** cover (without this section the checks get treated as
something they cannot support)
  - It scans only the objects **inside** `<upstream>..HEAD` (= what will actually be sent).
    Objects already upstream are not scanned: what cannot be pushed is not this push's risk.
  - It does not scan messages (that is pushscope_gate's job); it does not judge tag / branch
    names / reflog / stash / notes / PR bodies; it does not judge **unreachable objects**
    (old objects left behind by a rewrite still sit in the local object store and reflog --
    that is a local-machine risk, not the publishing surface, and cleaning it is a separate
    concern). All of the above are **unknown**, not "absent".
  - It does not judge semantic re-spellings (homophones / transliteration / abbreviations /
    splitting and concatenation) -- the same blind spot as leakscan.
  - Text only: a blob whose first 4KB contain NUL is skipped and counted -- this tool does not
    inspect information embedded in binaries.
  - It naturally **overlaps** with leakscan (the HEAD blobs are also in range). The value is
    in the blobs that exist "only in a middle commit", so the report gives a separate novel
    count, making the overlap measurable rather than leaving it to the reader.
  - When the same content hangs off several paths (rev-list gives only the first path) it is
    reported once; hit samples are masked by default (length only) -- this gate's stdout is
    folded into the next commit message by the export step. To see the literal text use
    `--show-samples`, only when the output reaches no publishing surface.
  - **Exemptions reuse the tree gate's own ALLOW** (`leakscan.ALLOW`, each item with a written
    reason). This is required: over half the in-range hits come from the shape rules' **own
    source** (a file that writes the rules inherently contains forbidden shapes), and without
    the same exemptions this gate could never go green -- a gate that always refuses is equal
    to no gate. The same exemption must give the same answer on both gates.

Can the checks produce a negative result?
  `verify/selftest_history_gate.py` builds its own local bare repo in temp and stages real
  states; the core case is "a middle commit introduces a dirty blob, a later commit deletes
  it": **on the same fixture leakscan (tree) must be rc=0 while this gate must be rc=1** --
  that is the reason this gate exists. Also: clean range (0) / both halves of masking / the
  term-list layer / binary skip / already-pushed out of range (0) / upstream unresolvable (2) /
  term list unreadable (2). `verify/falsify_history_gate.py` then injects three disabling
  mutations into this gate and the self-test must go red with them (checked by **case name**,
  so that a crash of the module is not mistaken for a catch, which counting rc alone would do).

Exit codes
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3. FAIL takes priority over UNDECIDABLE.
  "no git found" / "not a git repo" / "upstream unresolvable" / "term list unreadable" are all
  UNDECIDABLE -- could not check is not the same as clean.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def disp(path, root):
    """Keep only a relative path / last segment in the output. This gate's stdout is a
    **published surface**: an absolute path must not enter the output -- a rule that holds
    for the people who write the gates too."""
    if not path:
        return "(default)"
    try:
        r = os.path.relpath(path, root)
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return os.path.basename(path)


def mask(sample):
    """The masked form of a hit sample: state its length, not its content."""
    return "<<masked len=%d>>" % len(sample or "")


def run_git(gitp, repo, args, timeout=180):
    """A plain git call. cron has no PATH, so gitp must be an explicit path (see
    pushscope_gate.find_git)."""
    return subprocess.run([gitp, "-C", repo] + args, capture_output=True, timeout=timeout,
                          text=True, encoding="utf-8", errors="replace")


def list_range_objects(gitp, repo, ref, timeout=180):
    """-> (items, err). items = [(sha, path)]; path may be empty. A non-empty err means it
    could not be read (not "there are none")."""
    r = run_git(gitp, repo, ["rev-list", "--objects", "%s..HEAD" % ref], timeout=timeout)
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:]
    items = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, path = line.partition(" ")
        items.append((sha, path.strip()))
    return items, ""


def batch_types(gitp, repo, shas, timeout=180):
    """-> {sha: type}. One --batch-check call to get types, no per-object process."""
    if not shas:
        return {}
    inp = ("\n".join(shas) + "\n").encode("ascii")
    r = subprocess.run([gitp, "-C", repo, "cat-file", "--batch-check"],
                       input=inp, capture_output=True, timeout=timeout)
    out = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        p = line.split()
        if len(p) >= 2:
            out[p[0]] = p[1]
    return out


def read_blobs(gitp, repo, shas, timeout=300):
    """-> [(sha, bytes)]. Feed only shas already confirmed to be blobs by --batch-check
    (otherwise the stream parsing goes out of step)."""
    if not shas:
        return []
    inp = ("\n".join(shas) + "\n").encode("ascii")
    r = subprocess.run([gitp, "-C", repo, "cat-file", "--batch"],
                       input=inp, capture_output=True, timeout=timeout)
    out, i, items = r.stdout, 0, []
    while i < len(out):
        j = out.find(b"\n", i)
        if j < 0:
            break
        parts = out[i:j].decode("utf-8", "replace").split()
        if len(parts) < 3:
            i = j + 1                      # lines such as "<sha> missing" have no body
            continue
        try:
            size = int(parts[2])
        except ValueError:
            i = j + 1
            continue
        items.append((parts[0], out[j + 1:j + 1 + size]))
        i = j + 1 + size + 1
    return items


def head_tree_blobs(gitp, repo, timeout=180):
    """The set of blobs in the HEAD tree -- used to compute novel (those that exist only in a
    middle commit)."""
    r = run_git(gitp, repo, ["ls-tree", "-r", "HEAD"], timeout=timeout)
    s = set()
    for line in (r.stdout or "").splitlines():
        p = line.split()
        if len(p) >= 3:
            s.add(p[2])
    return s


def commits_touching(gitp, repo, ref, sha, timeout=120):
    """-> the short shas of commits in range that touch this object (add or delete), at most 8.
    Returns [] if it cannot be read; it does not guess."""
    r = run_git(gitp, repo, ["log", "--format=%h", "--find-object=%s" % sha, "%s..HEAD" % ref],
                timeout=timeout)
    if r.returncode != 0:
        return []
    return [x.strip() for x in (r.stdout or "").split() if x.strip()][:8]


def scan_history(repo, upstream=None, terms_path=None, terms_required=True,
                 show_samples=False, gitp=None):
    """Callable in-process (the self-test and the export step both go through here -- cron has
    no PATH, so one less silently-failing link). -> (rc, lines)"""
    repo = os.path.abspath(repo)
    tag = "history repo=...%s" % os.path.basename(repo)
    if terms_path is None:
        terms_path = os.path.join(repo, "_local", "privterms.txt")
    sys.path.insert(0, HERE)
    import leakscan as LS                                       # noqa: E402
    import pushscope_gate as PG                                 # noqa: E402

    gitp = gitp or PG.find_git()
    if not gitp:
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE: no git found (cron has no PATH) -- not checked is not clean",
                             "SUMMARY upstream=- commits=? blobs=? LEAKS=? rc=%d" % UNDECIDABLE]
    if not os.path.isdir(os.path.join(repo, ".git")):
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE: this is not a git repo (no .git) -- cannot tell which objects this push would send",
                             "SUMMARY upstream=- commits=? blobs=? LEAKS=? rc=%d" % UNDECIDABLE]

    ref, ustat, uwhy = PG.resolve_upstream(gitp, repo, upstream)
    lines = ["%s upstream=%s range=%s..HEAD" % (tag, ref or "(none)", ref or "(none)")]
    if ustat != "OK":
        lines.append("  UNDECIDABLE: %s" % uwhy)
        lines.append("SUMMARY upstream=%s commits=? blobs=? LEAKS=? rc=%d" % (ref or "-", UNDECIDABLE))
        return UNDECIDABLE, lines

    items, err = list_range_objects(gitp, repo, ref)
    if items is None:
        lines.append("  UNDECIDABLE: git rev-list --objects %s..HEAD failed: %s" % (ref, err))
        lines.append("SUMMARY upstream=%s commits=? blobs=? LEAKS=? rc=%d" % (ref, UNDECIDABLE))
        return UNDECIDABLE, lines

    shas = [s for s, _ in items]
    paths = {}
    for s, p in items:
        paths.setdefault(s, p)
    types = batch_types(gitp, repo, shas)
    blob_shas = sorted(s for s in shas if types.get(s) == "blob")
    head_blobs = head_tree_blobs(gitp, repo)
    novel = [s for s in blob_shas if s not in head_blobs]
    n_commits = sum(1 for s in shas if types.get(s) == "commit")

    terms, tstat = LS.load_terms(terms_path) if terms_required else ([], "SKIPPED")

    hits, allowed, binary, scanned = [], [], 0, 0
    for sha, body in read_blobs(gitp, repo, blob_shas):
        if b"\x00" in body[:4096]:
            binary += 1
            continue
        text = body.decode("utf-8", "replace")
        scanned += 1
        found, seen = [], set()
        for rule, rx in LS.SHAPES:
            for m in rx.finditer(text):
                found.append((rule, m.group(0)))
        for t in terms:
            for m in LS.term_re(t).finditer(text):
                found.append((LS.TERM_RULE, m.group(0)))
        rel = (paths.get(sha, "") or "").replace("\\", "/")
        cleared = LS.ALLOW.get(rel, {})          # the same exemptions as the tree gate
        for rule, sample in found:
            k = (rule, LS.norm(sample))
            if k in seen:
                continue
            seen.add(k)
            if rel and (LS.norm(sample) in cleared or "*" in cleared):
                allowed.append((sha, rel, rule, sample))
            else:
                hits.append((sha, rel, rule, sample))

    novel_set = set(novel)
    hits.sort(key=lambda h: (h[0], h[2]))
    for sha, path, rule, sample in hits:
        shown = sample[:60] if show_samples else mask(sample)
        who = ",".join(commits_touching(gitp, repo, ref, sha))
        where = "  [only in a middle commit - invisible to the tree gate]" if sha in novel_set else "  [also in the HEAD tree]"
        lines.append("  LEAK   blob %s %s: %s -> %s%s%s"
                     % (sha[:12], path or "(no path)", rule, shown, where,
                        ("  touched by=%s" % who) if who else ""))

    sh = [h for h in hits if h[2] in LS.SHAPE_RULES]
    lt = [h for h in hits if h[2] == LS.TERM_RULE]
    lines.append("B0 range      commits=%d objects=%d blobs=%d (only-in-middle-commit: %d)"
                 % (n_commits, len(shas), len(blob_shas), len(novel)))
    lines.append("B1 object content shape layer  blobs=%d rules=%d hits=%d exempt=%d  %s"
                 % (scanned, len(LS.SHAPES), len(sh), len(allowed), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("B2 object content literal layer  **NOT RUN** (terms_required=False): "
                     "this run's green does **not prove** the literal-term half")
    elif tstat == "MISSING":
        lines.append("B2 object content literal layer  terms unreadable (%s) -> UNDECIDABLE, not guessed as PASS"
                     % disp(terms_path, repo))
    else:
        lines.append("B2 object content literal layer  terms=%d hits=%d  %s"
                     % (len(terms), len(lt), "FAIL" if lt else "PASS"))
    if binary:
        lines.append("  note: skipped %d binary blob(s) (this tool does not inspect data embedded in binaries)" % binary)
    if hits and not show_samples:
        lines.append("  note: hit samples are masked by default (length only) -- this gate's output is"
                     " folded into the next commit message. To see the literal: git cat-file blob <sha>.")

    if hits:
        rc = FAIL
    elif tstat == "MISSING":
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY upstream=%s commits=%d blobs=%d LEAKS=%d allowlisted=%d rc=%d"
                 % (ref, n_commits, len(blob_shas), len(hits), len(allowed), rc))
    return rc, lines


def main(argv=None):
    # This tool prints text taken from the repository (paths, messages, samples). A Windows
    # console defaults to a legacy code page: one character it cannot encode would abort the
    # check with UnicodeEncodeError instead of printing a verdict. Escape what it cannot carry.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:                       # stdout replaced (StringIO) or already wrapped
        pass
    ap = argparse.ArgumentParser(description="Privacy gate for the object content of the range about to be pushed (the third thing, beyond tree and messages)")
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
        rc, lines = scan_history(a.repo, upstream=a.upstream, terms_path=a.terms,
                                 terms_required=not a.shape_only,
                                 show_samples=a.show_samples, gitp=a.git)
    except Exception as e:                                      # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=3")
        return ERROR
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
