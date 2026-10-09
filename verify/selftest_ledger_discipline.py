#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_ledger_discipline.py — 先证伪检查器，再信任它

纪律：每条规则都必须用**篡改样本打红一次**。跑不红的检查是装饰 —— 它把「没人验」
伪装成「验过了」。本文件对 R1/R2/R3 各打两到三次，外加 1 条正样本必须全绿，
再加 1 条 UNDECIDABLE（缺件不许猜成 PASS）。

无子进程（cron 无 PATH）：全部在进程内调用 verify/ledger_discipline.py 的 check()。
无输出 = 没法验；所以每条用例都打印期望 vs 实得。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_discipline as LD  # noqa: E402

T0 = 1_700_000_000  # 固定基准时间，避免依赖真实时钟


def w(path, text, mtime=None):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def plan_text(checked=True, with_marker=True, token="abc1234"):
    if checked:
        body = ("- [x] **T1 · 事**\n"
                "  → done: 提交 `%s` @ 2026-01-01\n" % token) if with_marker else \
               "- [x] **T1 · 事**\n"
    else:
        body = "- [ ] **T1 · 事**\n"
    return "# PLAN\n\n## 队列\n\n" + body + "\n- [ ] **T2 · 还没做**\n"


def ledger_line(wid="W-1", created="2026-01-01T00:00:00+08:00",
                touch="2026-01-01T00:00:00+08:00", note="提交 abc1234"):
    return json.dumps({"id": wid, "kind": "works", "title": "t",
                       "status": "running", "created": created,
                       "last_touch": touch, "artifact": "", "note": note},
                      ensure_ascii=False)


def case(d, *, plan="", ledger="", preregs=(), settles=(), plan_missing=False,
         ledger_missing=False):
    """搭一套夹具并跑 check()，返回 (rc, 逐行输出)"""
    pd = os.path.join(d, "plan.md")
    ld = os.path.join(d, "works_agenda.jsonl")
    rg = os.path.join(d, "prereg")
    os.makedirs(rg, exist_ok=True)
    if not plan_missing:
        w(pd, plan)
    if not ledger_missing:
        w(ld, ledger)
    for name, text, mt in preregs:
        w(os.path.join(rg, name), text, mt)
    for name, text, mt in settles:
        w(os.path.join(rg, name), text, mt)
    return LD.check(pd, ld, rg)


CASES = []


def expect(name, want_rc, want_substrings, **kw):
    CASES.append((name, want_rc, want_substrings, kw))


# A 正样本：done 带提交号且台账里有；预注册早于结算；台账行自洽 → 0 / 全 PASS
expect("A 正样本（应全绿）", 0,
       ["R1 LEDGER_COVERAGE PASS", "R2 PRE_REG_PRECEDES PASS", "R3 LEDGER_CONSISTENCY PASS"],
       plan=plan_text(), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# B R1 红：台账里没有那个提交号
expect("B R1 红：台账缺提交号", 1, ["R1 LEDGER_COVERAGE FAIL"],
       plan=plan_text(token="abc1234"), ledger=ledger_line(wid="W-2", note="别的提交 deadbee"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# C R1 红：勾了 [x] 但没有 → done: 标记
expect("C R1 红：勾了无 done 标记", 1, ["R1 LEDGER_COVERAGE FAIL", "无 → done:"],
       plan=plan_text(with_marker=False), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# D R2 红：结算存在但完全没有预注册
expect("D R2 红：结算无预注册", 1, ["R2 PRE_REG_PRECEDES FAIL", "无预注册"],
       plan=plan_text(), ledger=ledger_line(),
       settles=[("RUN_009_SETTLE.md", "s", T0)])

# E R2 红：预注册比结算晚
expect("E R2 红：预注册晚于结算", 1, ["R2 PRE_REG_PRECEDES FAIL", "预注册晚于结算"],
       plan=plan_text(), ledger=ledger_line(),
       preregs=[("PRE_REG_run_002.md", "p", T0 + 7200)],
       settles=[("RUN_002_SETTLE.md", "s", T0)])

# F R3 红：last_touch 早于 created
expect("F R3 红：last_touch 倒流", 1, ["R3 LEDGER_CONSISTENCY FAIL", "倒序"],
       plan=plan_text(), ledger=ledger_line(created="2026-01-02T00:00:00+08:00",
                                            touch="2026-01-01T00:00:00+08:00"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# G R3 红：坏 JSON 行
expect("G R3 红：坏 JSON 行", 1, ["R3 LEDGER_CONSISTENCY FAIL", "坏JSON=L2"],
       plan=plan_text(), ledger=ledger_line() + "\n{not json\n",
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# H 未做任务不算违规：只有 - [ ] 的队列 → R1 应 PASS（判据不能把「没做」当违规）
expect("H 未做任务不误报", 0, ["R1 LEDGER_COVERAGE PASS", "done=0"],
       plan=plan_text(checked=False), ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])

# I UNDECIDABLE：缺任务队列文件 → rc=2，不许猜成 PASS
expect("I 缺件=无法判定", 2, ["R1 LEDGER_COVERAGE UNDECIDABLE", "rc=2"],
       plan_missing=True, ledger=ledger_line(),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


# J 产物是文件（无提交号）：台账里以 JSON 转义形式写的裸路径也必须命中（测归一化）
PLAN_J = "# PLAN\n\n- [x] **T1 · 事**\n  → done: 产物 `E:/x/y/z.md`\n"
expect("J 路径引用（测转义归一）", 0, ["R1 LEDGER_COVERAGE PASS"], plan=PLAN_J,
       ledger=ledger_line(wid="W-3", note="落盘 E:\\x\\y\\z.md 384 B"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


# K 回归：**假绿样本** —— 相对路径在台账里以「未落地」这种反义上下文出现过，
#   必须仍判 FAIL。这是实测踩到的真事故（该行引用的提交不在台账，
#   却因命中了另一条写「未落地」的记录而被放行）。
PLAN_K = "# PLAN\n\n- [x] **T1 · 事**\n  → done: 产物 `verify/leakscan.py` @ x\n"
expect("K 回归：反义上下文不得当覆盖", 1, ["R1 LEDGER_COVERAGE FAIL", "T1:"],
       plan=PLAN_K,
       ledger=ledger_line(wid="W-9", note="未查清：verify/gate_stat.py 未落地"),
       preregs=[("PRE_REG_run_001.md", "p", T0)],
       settles=[("RUN_001_SETTLE.md", "s", T0 + 3600)])


def main():
    fails = []
    with tempfile.TemporaryDirectory(prefix="ldselftest_") as root:
        for i, (name, want_rc, subs, kw) in enumerate(CASES):
            d = os.path.join(root, "c%d" % i)
            os.makedirs(d, exist_ok=True)
            rc, lines = case(d, **kw)
            blob = "\n".join(lines)
            miss = [s for s in subs if s not in blob]
            ok = (rc == want_rc) and not miss
            print("[%s] %s  want_rc=%d got_rc=%d%s" % (
                "OK" if ok else "MISS", name, want_rc, rc,
                "" if not miss else "  缺子串=%s" % miss))
            for ln in lines:
                print("      | " + ln)
            if not ok:
                fails.append(name)
    print("\nSELFTEST %s  cases=%d failed=%d" % (
        "OK" if not fails else "FAILED", len(CASES), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
