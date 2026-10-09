#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_pushscope_gate.py — 证明「待推提交信息闸」能说是，也能说不。

做法：temp 里自造一个**本地裸仓**当远程（不联网、不读 live 仓、不依赖 _local/），用真的 git
摆出七种状态，每种都走 pushscope_gate 的**真路径**（argv → 取范围 → 判定 → 打印 → 退出码），不 mock。

  A 干净待推信息                 → rc 0 / commits=1（跑了 ≠ 静默）
  B 信息带盘符路径               → rc 1，点名 sha 与规则；**输出里不许出现该样本的字面**（打码）
  F 同一状态 + --show-samples    → rc 1，样本字面**必须**出现（打码的两半，缺一半就是死规则）
  C 信息带词表词                 → rc 1 / L2-term
  D 坏信息已在上游（推上去）      → rc 0（范围 = 待推；已推的不是这一次的风险）
  E 解析不到上游                 → rc 2 UNDECIDABLE（不许被读成通过）
  G 词表读不到                   → rc 2（没查 ≠ 干净）；输出里不许出现**机器绝对路径**
                                   

坏样本**拼接构造、不在源码里写成字面**：本文件自己也在 leakscan 扫的那棵树里，
写成字面会被旁边那道闸拒绝发布。`no_literal_sample_in_source()` 把这件事变成可失败的检查。
`pushscope_gate.py` 第一版把 git 候选写成了字面安装路径（盘符+反斜杠）—— 当场被那棵树闸
判红；把这条教训写进注释，又红了一次。所以这里连样本形状都不复写。G 用例守着输出侧。

用法
    python verify/selftest_pushscope_gate.py [--target PATH] [--git PATH]
退出码 0 = 全部符合预期 / 1 = 有用例不符 / 3 = 环境不足（跑不了，不算通过）
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
        raise RuntimeError("load 不了目标模块：%s" % target)
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
    """把「夹具不许在源码里写成字面」变成可失败的检查，而不是靠记。"""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    bad = [s for s in (FAKE_TERM, DRIVE_BAD) if s in src]
    if bad:
        print("  [MISS] 自证源码含字面坏样本 %r —— leakscan 会拒绝发布这份文件" % (bad,))
        return False
    print("  [PASS] 自证源码不含字面坏样本（拼接构造）")
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
        print("RUNNER NOT READY   找不到 git —— 这种情形下不许报通过")
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
            failures.append("%s: rc=%s(期望 %s) 缺=%r 不应出现=%r\n%s"
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
        # 用**相对路径**当远程 URL：绝对路径会被 file:// / 盘符 / MSYS 来回翻译坏掉
        git(gitp, work, ["remote", "add", "origin",
                         os.path.relpath(origin, work).replace("\\", "/")])
        os.makedirs(os.path.join(work, "_local"), exist_ok=True)
        with open(os.path.join(work, "_local", "privterms.txt"), "w", encoding="utf-8") as fh:
            fh.write("# 自造词表（自证用，非 live）\n" + FAKE_TERM + "\n")
        commit_file("1", "base one")
        git(gitp, work, ["push", "-q", "-u", "origin", "main"])

        # ---- A 干净待推信息：必须绿，且必须自报扫了几条（跑了 ≠ 静默） ----
        commit_file("2", "chore: selftest clean commit")
        rc, out = run_check(mod, work, gitp)
        expect("A 干净待推信息", rc, 0, out, ["上游=origin/main", "commits=1", "rc=0"])

        # ---- B 信息带盘符路径：必须红，点名 sha 与规则，且不回显字面 ----
        sha_bad = commit_file("3", "chore: leak at " + DRIVE_BAD)
        rc, out = run_check(mod, work, gitp)
        expect("B 盘符路径进信息（应红）", rc, 1, out,
               ["S1-drive-path", sha_bad[:12], "commits=2", "rc=1"],
               must_absent=[DRIVE_BAD])
        expect("B 打码写明（不只沉默）", rc, 1, out, ["masked len="])

        # ---- F 打码的另一半：--show-samples 时字面必须出现 ----
        rc, out = run_check(mod, work, gitp, ["--show-samples"])
        expect("F --show-samples 回显字面", rc, 1, out, [DRIVE_BAD, "rc=1"])

        # ---- C 信息带词表词：必须红 ----
        commit_file("4", "chore: approved by " + FAKE_TERM)
        rc, out = run_check(mod, work, gitp)
        expect("C 词表词进信息（应红）", rc, 1, out, ["L2-term", "rc=1"])

        # ---- D 推上去之后：坏信息已在上游 → 不在范围内 ----
        git(gitp, work, ["push", "-q", "origin", "main"])
        rc, out = run_check(mod, work, gitp)
        expect("D 坏信息已在上游（范围外，应绿）", rc, 0, out, ["commits=0", "rc=0"])

        # ---- E 解析不到上游：必须 UNDECIDABLE，不许被读成通过 ----
        rc, out = run_check(mod, work, gitp, ["--upstream", "refs/remotes/origin/nope"])
        expect("E 上游解析不到（应 2）", rc, 2, out, ["UNDECIDABLE", "rc=2"])
        if rc == 0:
            failures.append("E: 未知被读成了通过（rc=0）")

        # ---- G 词表读不到：没查 ≠ 干净 ----
        commit_file("5", "chore: selftest clean again")
        rc, out = run_check(mod, work, gitp,
                            ["--terms", os.path.join(root, "absent-terms.txt")])
        expect("G 缺词表=UNDECIDABLE（不许 PASS）", rc, 2, out,
               ["词表读不到", "rc=2", "absent-terms.txt"],
               must_absent=[root, root.replace("\\", "/")])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = len(cases) - len(failures)
    print("\n  %-34s %s" % ("自证源码无字面坏样本", "OK" if ok_src[0] else "**不符**"))
    for name, got, want, ok in cases:
        print("  %-34s rc=%-6s 期望 %-6s %s" % (name, got, want, "OK" if ok else "**不符**"))
    for f in failures:
        print("  FAIL %s" % f)
    total_fail = len(failures) + (0 if ok_src[0] else 1)
    print("SUMMARY cases=%d pass=%d fail=%d target=%s rc=%d"
          % (len(cases) + 1, len(cases) + 1 - total_fail, total_fail,
             os.path.basename(a.target), 1 if total_fail else 0))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
