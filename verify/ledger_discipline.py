#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ledger_discipline.py — 台账与预注册纪律的可失败检查器

判什么
  R1 LEDGER_COVERAGE  : 任务队列里每个 `- [x]` 且带 `→ done:` 的任务，其 done 段中的
                        **产物引用**（提交号形状 token：7-12 位十六进制含至少一个 a-f 字母；
                        或绝对路径：盘符路径）至少有一个出现在台账里。
                        `- [x]` 但缺 `→ done:` 也算违规。**只认绝对路径**：相对路径
                        （如 `verify/gate_stat.py`）在台账里会以「未落地」这类反义上下文命中，
                        实测产生过假绿（见 selftest 用例 K），故不纳入。
  R2 PRE_REG_PRECEDES : 每个结算文件（`RUN_*_SETTLE.md` / `SETTLE_*.md`）必须匹配到一个
                        候选预注册（`PRE_REG_*.md`，文件名含相同数字段），且其中至少一个
                        预注册的 mtime ≤ 结算文件 mtime。缺预注册或顺序倒置 → FAIL。
  R3 LEDGER_CONSISTENCY: 台账每行必须是合法 JSON；且 `last_touch` 不得早于 `created`
                        （两者都能解析时）。

范围 —— 本工具**不包含**什么（缺了这一节，判据就会被当成它撑不住的东西）
  · R1 只证明「这个提交号/路径字符串在台账里出现过」。它**不证明**产物真实存在、不证明它
    挂在正确的工项下、也不看它是不是别人写的。artifact 是否真在磁盘上由
    `works/workline_check.py` 负责，本工具不重复造第二套。匹配是**宽松**的：任何一条台账
    记录里出现同一字符串即算覆盖，不做「哪条工项该配哪个产物」的归属判定。
  · R1 **不能分辨引用与反语**：「某路径已交付」和「某路径未落地」在字符串层面同形。绝对路径
    的误配概率低但不为零 —— 所以 R1 是覆盖性线索，不是交付证明（交付证明要读盘或读 VCS）。
  · R2 只比 mtime，**不读文件内容**：mtime 可被复制/检出/同步改写。所以 R2 的 PASS 只说明
    「按文件时间戳看顺序对」，**不说明**「预测确实写在发布之前」。要更强判据得有内嵌
    时间戳的预测台账或 git 提交历史（本项目尚无，记为已知缺口）。
  · R2 的候选匹配按「文件名数字段相交」选取，**可能过宽**（不同来源的 NNN 会互相当候选）。
  · R3 只查行内自洽与行可解析，**不查**跨行顺序、不查 id 唯一性。

判据能否给出否定结果
  能。`selftest_ledger_discipline.py` 对三条规则各打了一次篡改样本（去提交号 / 删预注册 /
  预注册晚于结算 / last_touch 早于 created），四条全部实测变红；另有 1 条正样本必须全绿。
  打不红的检查是装饰 —— 所以测试集里必须有反面样本，这条规矩写在测试文件里。

退出码
  PASS=0 / FAIL=1 / UNDECIDABLE=2（缺文件或读不到，不猜） / ERROR=3（自身异常）
  FAIL 优先于 UNDECIDABLE。判据不足时说「无法判定」，不返回一个看起来像结论的数。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# 本机落点**不进公开仓库**（）：见 .gitignore 的 `_local/`。
# 解析顺序：显式 `--plan/--ledger/--prereg-dir` > `_local/wiring.json`。
# 都缺 → 退到一个**明显不存在的占位**，让工具以 UNDECIDABLE 收场（rc=2），
# 而不是偷偷跑一个错目标还报绿。
_HERE = os.path.dirname(os.path.abspath(__file__))
_WIRING = os.path.normpath(os.path.join(_HERE, os.pardir, "_local", "wiring.json"))


def _default_path(key, placeholder):
    try:
        with open(_WIRING, "r", encoding="utf-8") as fh:
            val = json.load(fh).get(key)
        if val:
            return val
    except Exception:
        pass
    return placeholder


DEF_PLAN = _default_path("plan", os.path.join("_local", "PLAN.md"))
DEF_LEDGER = _default_path("ledger", os.path.join("_local", "works_agenda.jsonl"))
DEF_PREREG = _default_path("prereg_dir", os.path.join("_local", "prereg"))

TASK_RE = re.compile(r"^- \[( |x)\]\s*\*\*(T\d+)")
TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{7,12}(?![0-9a-fA-F])")
PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s`\"'（）()；;，,]+")
NUM_RE = re.compile(r"\d{3,}")

PASS, FAIL, UNDECIDED = "PASS", "FAIL", "UNDECIDABLE"


def norm(s):
    """路径归一：\\ 与 / 折叠成 /，好让台账里 JSON 转义过的路径能和队列里的裸路径对上。"""
    return re.sub(r"[\\/]+", "/", s or "")


def commit_tokens(text):
    """提交号形状：7-12 位十六进制、含至少一个字母（排除纯数字日期如 20260928）。"""
    out = []
    for m in TOKEN_RE.finditer(text or ""):
        t = m.group(0)
        if not re.search(r"[a-f]", t):
            continue
        if t not in out:
            out.append(t)
    return out


def product_refs(text):
    """产物引用 = 提交号 ∪ 路径（归一化后）。"""
    out = list(commit_tokens(text))
    for m in PATH_RE.finditer(text or ""):
        p = norm(m.group(0)).rstrip(".,;:、）)")
        if len(p) > 8 and p not in out:
            out.append(p)
    return out


def ledger_haystack(path):
    """台账的可比文本：原始行 + 解析后的字段值（后者已去 JSON 转义），统一归一化。"""
    if not os.path.isfile(path):
        return ""
    chunks = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            chunks.append(line)
            try:
                j = json.loads(line)
            except Exception:
                continue
            for v in j.values():
                if isinstance(v, str):
                    chunks.append(v)
                elif isinstance(v, list):
                    chunks.extend(str(x) for x in v)
    return norm("\n".join(chunks))


def parse_tasks(plan_text):
    """→ [(tid, checked, block_text), ...]"""
    tasks, cur = [], None
    for line in (plan_text or "").splitlines():
        m = TASK_RE.match(line)
        if m:
            if cur:
                tasks.append(cur)
            cur = [m.group(2), m.group(1) == "x", line]
        elif cur is not None:
            cur[2] += "\n" + line
    if cur:
        tasks.append(cur)
    return [(t[0], t[1], t[2]) for t in tasks]


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def rule_r1(plan_path, hay):
    if not os.path.isfile(plan_path):
        return UNDECIDED, "缺任务队列文件：%s" % plan_path, []
    tasks = parse_tasks(read_text(plan_path))
    done = [t for t in tasks if t[1]]
    missing, n_ok, no_marker, rows = [], 0, [], []
    for tid, _c, block in done:
        if "→ done:" not in block:
            no_marker.append(tid)
            rows.append("   ↳ %s 有 [x] 但无 → done: 标记" % tid)
            continue
        refs = product_refs(block)
        hit = [t for t in refs if t in hay]
        rows.append("   ↳ %s refs=%d hit=%s" % (
            tid, len(refs), ",".join(hit) if hit else "-"))
        if hit:
            n_ok += 1
        else:
            shown = ",".join(refs[:3]) + ("…" if len(refs) > 3 else "")
            missing.append("%s:[%s]" % (tid, shown or "无产物引用"))
    if missing or no_marker:
        return FAIL, "done=%d 已覆盖=%d 缺=%d %s %s" % (
            len(done), n_ok, len(missing), " ".join(missing),
            ("| 有 [x] 无 → done: %s" % ",".join(no_marker)) if no_marker else ""), rows
    return PASS, "done=%d 已覆盖=%d" % (len(done), n_ok), rows


def rule_r2(prereg_dir):
    if not os.path.isdir(prereg_dir):
        return UNDECIDED, "缺预注册目录：%s" % prereg_dir
    names = sorted(os.listdir(prereg_dir))
    pre = [n for n in names if n.startswith("PRE_REG_") and n.endswith(".md")]
    settles = [n for n in names
               if (n.startswith("RUN_") and n.endswith("_SETTLE.md"))
               or (n.startswith("SETTLE_") and n.endswith(".md"))]
    if not settles:
        return UNDECIDED, "目录里没有结算文件（%d 个预注册）" % len(pre)
    bad, ok = [], 0
    for s in settles:
        segs = set(NUM_RE.findall(s))
        cands = [p for p in pre if segs & set(NUM_RE.findall(p))]
        if not cands:
            bad.append("%s:无预注册" % s)
            continue
        sm = os.path.getmtime(os.path.join(prereg_dir, s))
        good = [c for c in cands
                if os.path.getmtime(os.path.join(prereg_dir, c)) <= sm]
        if good:
            ok += 1
        else:
            bad.append("%s:预注册晚于结算(%s)" % (s, ",".join(cands)))
    if bad:
        return FAIL, "结算=%d 顺序对=%d %s" % (len(settles), ok, " ".join(bad))
    return PASS, "结算=%d 顺序对=%d" % (len(settles), ok)


def rule_r3(ledger_path):
    if not os.path.isfile(ledger_path):
        return UNDECIDED, "缺台账文件：%s" % ledger_path
    bad_json, bad_order, n = [], [], 0
    with open(ledger_path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                j = json.loads(line)
            except Exception as e:
                bad_json.append("L%d(%s)" % (i, type(e).__name__))
                continue
            c, t = parse_ts(j.get("created")), parse_ts(j.get("last_touch"))
            if c and t and t < c:
                bad_order.append("L%d(%s<%s)" % (i, j.get("last_touch"), j.get("created")))
    if bad_json or bad_order:
        return FAIL, "行=%d 坏JSON=%s 倒序=%s" % (n, ",".join(bad_json) or "-",
                                                ",".join(bad_order) or "-")
    return PASS, "行=%d" % n


def check(plan_path, ledger_path, prereg_dir, explain=False):
    try:
        hay = ledger_haystack(ledger_path)
    except Exception:
        hay = ""
    r1_v, r1_d, r1_rows = rule_r1(plan_path, hay)
    r2_v, r2_d = rule_r2(prereg_dir)
    r3_v, r3_d = rule_r3(ledger_path)
    results = [("R1 LEDGER_COVERAGE", r1_v, r1_d, r1_rows),
               ("R2 PRE_REG_PRECEDES", r2_v, r2_d, []),
               ("R3 LEDGER_CONSISTENCY", r3_v, r3_d, [])]
    lines = []
    for name, v, d, rows in results:
        lines.append("%s %s   %s" % (name, v, d))
        if explain:
            lines.extend(rows)
    verdicts = [v for _n, v, _d, _r in results]
    if FAIL in verdicts:
        rc = 1
    elif UNDECIDED in verdicts:
        rc = 2
    else:
        rc = 0
    lines.append("SUMMARY rules=%d PASS=%d FAIL=%d UNDECIDABLE=%d rc=%d" % (
        len(verdicts), verdicts.count(PASS), verdicts.count(FAIL),
        verdicts.count(UNDECIDED), rc))
    return rc, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=DEF_PLAN)
    ap.add_argument("--ledger", default=DEF_LEDGER)
    ap.add_argument("--prereg-dir", default=DEF_PREREG)
    ap.add_argument("--explain", action="store_true",
                    help="逐条列出每个任务的产物引用与命中的 token（查 R1 为什么红/绿）")
    a = ap.parse_args()
    try:
        rc, lines = check(a.plan, a.ledger, a.prereg_dir, explain=a.explain)
    except Exception as e:
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rules=0 rc=3")
        return 3
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
