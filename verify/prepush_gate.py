#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prepush_gate.py — 手动 push 路径上的那道闸（发布前隐私闸的第三个入口）

用法
    python verify/prepush_gate.py [--repo R] [--upstream REF] [--terms FILE]
                                  [--git GIT] [--quiet] [--no-log]

退出码 PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3（FAIL 优先于 UNDECIDABLE）。

为什么有它
  发布路径有两条，闸原先只盖住一条：
    ① 自动export job —— the export step 出口处进程内调三闸（fail-closed）；
    ② **手动** `git commit && git push` —— 一条闸都没有。实测那次泄漏
       正是手动提交进去的。
  本文件是 ② 的闸体，被 .git/hooks/pre-push 调用（经 tools/install_git_hooks.py 安装）。
  判据与 ① **共用同一批函数**（leakscan / pushscope_gate / history_gate），不另写一套 ——
  同一件事两个入口，不是两套标准。

范围 —— 本闸**不包含**什么（缺这一节，判据会被当成它撑不住的东西）
  · 只在装了 hook 的本机克隆上生效。`git push --no-verify` 绕过；自证
    （verify/selftest_prepush_hook.py）里有一条用例**专门把这个洞测出来**，不假装它不存在。
    别的机器 / CI 上没有这道闸。
  · 只盖 git 这一条出口。别的出口（chat / X / 邮件 / 网页表单）不在这里。
  · 取不到基（新分支首推、本地没有 remote-tracking ref）→ UNDECIDABLE=2 → 拒绝，
    不猜成干净。这是 fail-closed 的代价，写在明面上。
  · 不判二进制里嵌的信息、不判语义换写法（同 leakscan 的边界面）。

判据能否给出否定结果
  能。自证给 9 条真实 push（干净 / 树脏 / 信息脏 / 词表缺 / 无 PATH / 无 PATH 且无 sidecar /
  --no-verify 旁路 / hook 被改 / core.hooksPath 被改指），每条只认「远端 ref 动没动 + 留痕
  日志」两样硬证据。verify/falsify_prepush_hook.py 再把三个部件各改坏一次，自证必须跟着红。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3
NAME = {PASS: "PASS", FAIL: "FAIL", UNDECIDABLE: "UNDECIDABLE", ERROR: "ERROR"}
CST = timezone(timedelta(hours=8))


def run(repo, upstream=None, terms_path=None, gitp=None):
    """进程内调三闸（cron 无 PATH，少一个静默失效的环节）→ (rc, lines)"""
    repo = os.path.abspath(repo)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import leakscan
    import pushscope_gate
    import history_gate

    lines = ["prepush_gate repo=…%s 基=%s（= 本次 push 实际发送的范围）"
             % (os.path.basename(repo), upstream or "(自动：@{u} / origin/main)")]
    trc, tl = leakscan.scan(repo, terms_path=terms_path)
    mrc, ml = pushscope_gate.scan_messages(repo, upstream=upstream, terms_path=terms_path, gitp=gitp)
    hrc, hl = history_gate.scan_history(repo, upstream=upstream, terms_path=terms_path, gitp=gitp)
    for ln in list(tl) + list(ml) + list(hl):
        lines.append("  " + ln)
    rcs = (trc, mrc, hrc)
    if FAIL in rcs:
        out = FAIL
    elif UNDECIDABLE in rcs:
        out = UNDECIDABLE
    elif any(r != PASS for r in rcs):
        out = ERROR
    else:
        out = PASS
    lines.append("PREPUSH %s  tree=%d msg=%d hist=%d -> rc=%d"
                 % (NAME.get(out, "?"), trc, mrc, hrc, out))
    return out, lines


def write_log(repo, rc, base):
    """留痕（.git/prepush_gate.log）：闸跑过没有、结论是什么。

    只写结论与基的前 12 位 —— 命中样本是发布面材料，不进这里。
    这一行是「闸在 push 真的跑了」的**外部痕迹**：没有它，一次 --no-verify 旁路和
    「闸放行了」长得一模一样。
    """
    p = os.path.join(repo, ".git", "prepush_gate.log")
    if not os.path.isdir(os.path.dirname(p)):
        return None
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("%s rc=%s base=%s\n" % (datetime.now(CST).isoformat(timespec="seconds"),
                                         rc, (base or "auto")[:12]))
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="手动 push 路径上的隐私闸（树 + 提交信息 + 待推对象）")
    ap.add_argument("--repo", default=REPO, help="被查的仓（默认 = 本脚本所在仓）")
    ap.add_argument("--upstream", default=None,
                    help="本次推送的基（pre-push stdin 给的远端 sha）。缺省 = 自动找 @{u} / origin/main")
    ap.add_argument("--terms", default=None, help="词表路径（默认 <repo>/_local/privterms.txt）")
    ap.add_argument("--git", default=None, help="git 可执行文件（cron 无 PATH；hook 从 sidecar 传）")
    ap.add_argument("--quiet", action="store_true", help="只打最后一行结论")
    ap.add_argument("--no-log", action="store_true", help="不写 .git/prepush_gate.log")
    a = ap.parse_args(argv)
    try:
        rc, lines = run(a.repo, upstream=a.upstream, terms_path=a.terms, gitp=a.git)
    except Exception as e:                                        # noqa: BLE001
        print("PREPUSH ERROR %s: %s" % (type(e).__name__, e))
        print("PREPUSH ERROR rc=3")
        if not a.no_log:
            try:
                write_log(a.repo, ERROR, a.upstream)
            except OSError:
                pass
        return ERROR
    for ln in (lines[-1:] if a.quiet else lines):
        print(ln)
    if not a.no_log:
        try:
            write_log(a.repo, rc, a.upstream)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
