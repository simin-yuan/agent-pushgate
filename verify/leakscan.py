#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""leakscan.py -- public-repo privacy gate: keep "machine paths / operator literal terms"
out of the tree before a push.

Usage
    python verify/leakscan.py [repo_root] [--terms FILE] [--shape-only] [--explain]
    Default root = this repo, default term list = <root>/_local/privterms.txt.
    Run once before export and once in CI (CI can only use --shape-only; see below).

What it checks
  L1 shape layer (no configuration; runs on any machine)
     S1-drive-path        drive-absolute path (letter + colon + slash). The colon must sit
                          on a word boundary, so a URL scheme separator does not count.
     S2-unc-path          network path starting with a double backslash (host + share +
                          at least one segment).
     S3-profile-internal  path inside a user profile (the Local / Roaming segment of
                          AppData).
  L2 literal layer (needs a term list)
     Operator-private literal terms (real name / private directory names / hostnames /
     organisation names), one per line, `#` starts a comment, case-insensitive, matched on
     word boundaries. Default list `_local/privterms.txt`, and `_local/` is in .gitignore,
     so it **never reaches the public repo** -- committing the term list into the repo means
     publishing, once, the very terms you are trying to block: self-defeating.

Scope -- what this tool does **not** cover (without this section the checks get treated as
something they cannot support)
  - L1 only recognises **shape**, not meaning. A synthetic placeholder path has the same
    shape as a real machine path and can only be cleared item by item via ALLOW; every
    exemption must state a reason -- a reason-less exemption is a long-term allowlist for a
    real leak.
  - L1 **cannot catch** a bare literal name with no path around it (a line of prose such as
    "must be approved by <name>"); that is L2's job.
  - L2 depends on a local term list. In CI, on a clean clone, there is no `_local/`, so CI
    runs `--shape-only` and only exercises the shape layer; the output states "L2 not run":
    **a green CI does not prove the literal layer is clean**. Feeding the term list into CI
    needs an Actions secret, which is an outward-facing configuration change and must be
    decided by the repository owner (not done).
  - Text only: it does not inspect information embedded in images or binaries, and it does
    not catch semantic re-spellings (homophones, transliteration, abbreviations, splitting
    and concatenation).
  - It does not judge whether a passage *should* be public, only whether it carries an
    identifier string that should not be.

Can the checks produce a negative result?
  Yes. `selftest_leakscan.py` injects one bad sample per rule S1/S2/S3/L2 (each must go
  red), one **hand-made** clean control (must stay green), one URL false-positive control
  (must not go red), a missing term list (must be UNDECIDABLE, not PASS), and both halves of
  the exemption mechanism (no exemption -> red / exemption -> green). `the mutation
  (falsify) test` then breaks this gate once (forcing rc=0) -> the self-test must go red with
  it: a check you cannot make fail is decoration.

Exit codes
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3. FAIL takes priority over UNDECIDABLE.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

# `_local/` holds this machine's private wiring and the term list itself -- scanning it would
# mean scanning your own blocklist.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist", "_local"}

SHAPES = [
    # The negative lookbehind before the colon excludes URL scheme separators (...ps://...):
    # a drive letter is never preceded by an alphanumeric.
    ("S1-drive-path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s`\"'()\uff08\uff09;\uff1b,\uff0c]+")),
    ("S2-unc-path", re.compile(r"\\\\[^\s\\/`\"'()\uff08\uff09;\uff1b,\uff0c]{1,24}[\\/][^\s`\"'()\uff08\uff09;\uff1b,\uff0c]+")),
    ("S3-profile-internal", re.compile(r"AppData[\\/](?:Local|Roaming)[\\/]", re.I)),
]

#: Exemptions, each reviewed individually with a written reason.
#: key = repo-relative path, value = {normalised match: reason}.
#: A match of `*` exempts the whole file. There is **no "omit for a reason" shortcut**:
#: a missing reason simply cannot be written.
ALLOW = {
    # This file is the source of every shape rule; its own regex source necessarily contains
    # fragments of the forbidden shapes.
    # Without exempting it, it could detect nothing (same reason as any leak scanner's own source).
    "verify/leakscan.py": {"*": "source of the shape rules, inherently contains forbidden-shape fragments"},
    # Likewise: the bad samples must live inside the self-test, otherwise
    # "does it go red?" is unprovable.
    "verify/selftest_leakscan.py": {"*": "bad samples deliberately injected in the self-test"},
    "verify/selftest_ledger_discipline.py": {
        "e:/x/y/z.md": "synthetic placeholder path for case J (not a real machine path)",
        # The JSON-escaped form of the same placeholder path (double backslash) **also**
        # hits the UNC rule -- an escaped string that merely looks like a UNC path, not a
        # real one. The rule is not narrowed for this (narrowing = shrinking the detection
        # surface); instead a per-item exemption keeps it out in the open.
        "/x/y/z.md": "escaped form of the same placeholder (one leading slash remains after normalisation); S2 also hits the escaped string",
    },
}

SHAPE_RULES = {name for name, _rx in SHAPES}
TERM_RULE = "L2-term"


def norm(s):
    """Normalise a match: fold slashes, strip whitespace, lowercase -- so that `E:\\\\x` and
    `E:/x` map to the same exemption entry."""
    return re.sub(r"[\\/]+", "/", s or "").strip().lower()


def _rel(p, root):
    """Keep only a relative path / last segment in the output. This gate's stdout is a
    **published surface**: the export step folds it into the next commit message, so it must
    not itself carry a machine-absolute path."""
    if not p:
        return "(default)"
    try:
        r = os.path.relpath(p, root)
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return os.path.basename(p)


def load_terms(path):
    """-> (terms, status). status: RAN (list read, even if empty) / MISSING (unreadable)."""
    if not path or not os.path.isfile(path):
        return [], "MISSING"
    terms = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t = line.strip()
            if t and not t.startswith("#"):
                terms.append(t)
    return terms, "RAN"


def term_re(term):
    """Case-insensitive + word boundary. Non-ASCII terms fall naturally outside [A-Za-z0-9],
    so they still match as substrings."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.I)


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def scan(root, terms_path=None, terms_required=True, allow=None, explain=False):
    """Callable in-process (the self-test does not use a subprocess -- cron has no PATH).
    -> (rc, lines)"""
    root = os.path.abspath(root)
    allow = ALLOW if allow is None else allow
    if terms_path is None:
        terms_path = os.path.join(root, "_local", "privterms.txt")

    terms, tstat = load_terms(terms_path) if terms_required else ([], "SKIPPED")

    hits, allowed = [], []
    scanned = binary = 0
    for p in iter_files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            binary += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        scanned += 1

        found, seen = [], set()
        for rule, rx in SHAPES:
            for m in rx.finditer(text):
                found.append((rule, m.group(0)))
        for t in terms:
            for m in term_re(t).finditer(text):
                found.append((TERM_RULE, m.group(0)))
        for rule, sample in found:
            k = (rule, norm(sample))
            if k in seen:
                continue
            seen.add(k)
            cleared = allow.get(rel, {})
            reason = cleared.get(norm(sample))
            if reason is None and "*" in cleared:
                reason = cleared["*"]
            if reason is None:
                hits.append((rel, rule, sample))
            else:
                allowed.append((rel, rule, sample, reason))

    sh = [h for h in hits if h[1] in SHAPE_RULES]
    lt = [h for h in hits if h[1] == TERM_RULE]

    lines = ["leakscan root=...%s"
             % os.path.basename(root)]
    if tstat == "RAN":
        lines.append("  terms %s (%d terms)" % (_rel(terms_path, root), len(terms)))
    if explain:
        for rel, rule, sample, reason in allowed:
            lines.append("  allow  %s: %s -> %r  (%s)" % (rel, rule, sample[:60], reason))
    for rel, rule, sample in hits:
        lines.append("  LEAK   %s: %s -> %r" % (rel, rule, sample[:60]))

    lines.append("L1 shape layer  rules=%d files=%d hits=%d  %s" % (
        len(SHAPES), scanned, len(sh), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("L2 literal layer  **NOT RUN** (--shape-only): this run's green does **not prove** the literal-term half")
    elif tstat == "MISSING":
        lines.append("L2 literal layer  terms unreadable (%s) -> UNDECIDABLE, not guessed as PASS"
                     % _rel(terms_path, root))
    else:
        lines.append("L2 literal layer  terms=%d hits=%d  %s" % (
            len(terms), len(lt), "FAIL" if lt else "PASS"))
    if binary:
        lines.append("  note: skipped %d binary file(s) (this tool does not inspect data embedded in binaries)" % binary)

    if hits:
        rc = 1
    elif tstat == "MISSING":
        rc = 2
    else:
        rc = 0
    lines.append("SUMMARY files=%d allowlisted=%d LEAKS=%d rc=%d" % (
        scanned, len(allowed), len(hits), rc))
    return rc, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Public-repo privacy gate (machine paths / operator literal terms)")
    ap.add_argument("root", nargs="?", default=REPO)
    ap.add_argument("--terms", default=None,
                    help="term list path. Default <root>/_local/privterms.txt (gitignored)")
    ap.add_argument("--shape-only", action="store_true",
                    help="run the L1 shape layer only (used by CI; the output states 'L2 not run')")
    ap.add_argument("--explain", action="store_true", help="list every exemption with its reason")
    a = ap.parse_args(argv)
    try:
        rc, lines = scan(a.root, terms_path=a.terms,
                         terms_required=not a.shape_only, explain=a.explain)
    except Exception as e:                                   # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=3")
        return 3
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
