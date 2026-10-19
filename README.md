# agent-pushgate

[![tests](https://github.com/simin-yuan/agent-pushgate/actions/workflows/tests.yml/badge.svg)](https://github.com/simin-yuan/agent-pushgate/actions/workflows/tests.yml)

A pre-push safety gate for **anything that pushes to a public repo — especially AI
agents**. It answers one question before code leaves your machine: *what is this push
actually going to send, besides the diff you intended?*

Every check is **falsifiable**: each gate ships a self-test that must turn **red** the
moment the gate is sabotaged. A check that cannot fail is decoration, not a check.

## The gates

| Gate | What it inspects | Catches |
|---|---|---|
| `verify/leakscan.py` | the **working tree** | machine paths (drive-letter / UNC / `AppData`), operator-specific literal terms |
| `verify/pushscope_gate.py` | the **commit messages** in the push range | secrets / private paths typed into a commit message |
| `verify/history_gate.py` | the **blob contents** in the push range | a secret that was committed then deleted — still reachable by anyone who clones |
| `verify/ledger_discipline.py` | a task **ledger + pre-registration** | tasks marked done without an artifact, results claimed before the pre-registration was written |
| `verify/prepush_gate.py` | all of the above, wired as a **git `pre-push` hook** | the *manual* `git commit && git push` path, which usually has no gate at all |

Exit codes are uniform: `0` PASS / `1` FAIL / `2` UNDECIDABLE / `3` ERROR. `FAIL` outranks
`UNDECIDABLE`. A missing word-list is `UNDECIDABLE`, **never** silently promoted to PASS.

## Why the word-list lives in `_local/`

`leakscan` / `history_gate` / `pushscope_gate` match operator-specific literals (your real
name, host names, private directory names) from a word-list. **The word-list is never
committed** — it lives in `_local/privterms.txt`, which `.gitignore` excludes. Committing
the list of words you are trying to hide would leak them in one stroke. The repo ships only
the mechanism; supply your own terms:

```sh
mkdir -p _local && printf 'your-name\nyour-host\n' > _local/privterms.txt
python verify/leakscan.py .
```

## Install the pre-push hook

```sh
python tools/install_git_hooks.py            # idempotent; writes .git/hooks/pre-push + sidecars
python tools/install_git_hooks.py --check    # proves the installed hook is byte-identical to the template
```

The hook runs `verify/prepush_gate.py` on every `git push` and refuses (fail-closed) if any
gate returns non-zero. `git push --no-verify` bypasses it — the self-test measures that hole
rather than pretending it isn't there.

## Try it / test it

```sh
python verify/selftest_leakscan.py           # 11 cases
python verify/selftest_pushscope_gate.py     #  9 cases
python verify/selftest_history_gate.py       # 11 cases
python verify/selftest_ledger_discipline.py  # 11 cases
python verify/selftest_prepush_hook.py       # 26 cases (real bare-repo pushes, incl. --no-verify)
python verify/falsify_history_gate.py        # mutate the gate -> the self-test must go red
python verify/falsify_pushscope_gate.py
python verify/falsify_prepush_hook.py
```

All self-tests run entirely in temp fixtures — no network, no live repo. **Zero third-party
dependencies** (stdlib only; git is required for the history/push-scope gates).

## Scope — what this does *not* do

- It judges **identifier strings, not intent**. It cannot tell whether a paragraph *should*
  be public, only whether it carries a string that should not be.
- It scans **text only**. No OCR of images, no binaries, no paraphrase/substrings/acronyms.
- `leakscan` L1 only recognises **shapes**; a synthetic placeholder path looks identical to a
  real one, so genuine exceptions are declared one-by-one in the `ALLOW` table *with a written
  reason* — an exception without a reason is a permanent whitelist for a real leak.
- CI can only run `--shape-only` (no word-list in a clean clone); a green CI run **does not
  prove** the literal term layer is clean.

## Related tools

- **[greencheck](https://github.com/simin-yuan/greencheck)** — mutation testing for validators.
- **[precheck](https://github.com/simin-yuan/precheck)** — prove claims with frozen, pre-registered checks.
- **[self-auditing-agent](https://github.com/simin-yuan/self-auditing-agent)** — an agent auditing its own workflow.

## License

MIT — see [LICENSE](LICENSE).

© 2026 Simin (Simon) Yuan — ORCID [0009-0002-6843-3390](https://orcid.org/0009-0002-6843-3390) · [github.com/simin-yuan](https://github.com/simin-yuan).
