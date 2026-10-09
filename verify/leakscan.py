#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""leakscan.py — 公开仓隐私闸：把「机器路径 / 操作者字面词」挡在 push 之前

用法
    python verify/leakscan.py [repo_root] [--terms FILE] [--shape-only] [--explain]
    默认 root = 本仓根，默认词表 = <root>/_local/privterms.txt。
    导出前跑一次、CI 跑一次（CI 只能是 --shape-only，理由见下）。

判什么
  L1 形状层（不需配置，任何机器上都能跑）
     S1-drive-path        盘符绝对路径（字母 + 冒号 + 斜杠开头）。冒号前必须是词边界，
                          所以 URL 里的协议分隔不算。
     S2-unc-path          双反斜杠开头的网络路径（主机 + 共享 + 至少一段）。
     S3-profile-internal  用户配置内部路径（AppData 的 Local / Roaming 段）。
  L2 字面层（需词表）
     操作者私有字面词（真名 / 私有目录名 / 主机名 / 机构名），一行一个，`#` 起注释，
     大小写不敏感、按词边界匹配。默认词表 `_local/privterms.txt`，而 `_local/` 在
     .gitignore 里，**不进公开仓** —— 把词表提交进仓 = 把要挡的词自己公开一次，自毁。

范围 —— 本工具**不包含**什么（缺这一节，判据就会被当成它撑不住的东西）
  · L1 只认**形状**，不认语义。合成占位路径与真实机器路径同形，只能靠 ALLOW 逐条豁免；
    豁免必须写明理由 —— 无理由的豁免就是真泄漏的长期白名单。
  · L1 **抓不到**不带路径的纯字面人名（正文里的「须 <名> 批」），那归 L2。
  · L2 依赖本地词表。CI 在干净 clone 上没有 `_local/`，故 CI 用 `--shape-only` 只跑形状层，
    输出里会打印「L2 未跑」：**CI 的绿不证明字面层干净**。把词表送进 CI 需要 Actions
    secret，那是对外配置变更，须由仓库所有者决定（未做）。
  · 只扫文本：不判图片/二进制里嵌的信息，不判语义换写法（同音、拼音、缩写、拆分拼接）。
  · 不判「这段话该不该公开」，只判它有没有带上不该带的**标识串**。

判据能否给出否定结果
  能。`selftest_leakscan.py` 给 S1/S2/S3/L2 各注入一个坏样本（各自必须红）、一个**自造**的
  干净对照（必须绿）、一个 URL 误报对照（不许红）、缺词表（必须 UNDECIDABLE 而非 PASS）、
  以及豁免机制的两半（无豁免红 / 有豁免绿）。`the mutation (falsify) test` 再把本闸改坏一次（强制 rc=0）
  → 自证必须跟着红：打不红的检查是装饰。

退出码
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3。FAIL 优先于 UNDECIDABLE。
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

# `_local/` 装的是本机私有接线与词表本身 —— 扫它就等于扫自己的禁词表。
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist", "_local"}

SHAPES = [
    # 冒号前的负向后顾排除 URL 协议分隔（…ps://…）：盘符前不会是字母数字。
    ("S1-drive-path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s`\"'（）()；;，,]+")),
    ("S2-unc-path", re.compile(r"\\\\[^\s\\/`\"'（）()；;，,]{1,24}[\\/][^\s`\"'（）()；;，,]+")),
    ("S3-profile-internal", re.compile(r"AppData[\\/](?:Local|Roaming)[\\/]", re.I)),
]

#: 逐条审过、写明理由的豁免。键 = 仓库相对路径，值 = {归一化后的命中串: 理由}。
#: 命中串 `*` 表示该文件整体豁免。**不给「按理由省略」的捷径**：理由缺失就写不出来。
ALLOW = {
    # 本文件是所有形状规则的源码；正则源码里必然出现被禁形态的片段。
    # 不豁免它，它就检测不了任何东西（同 any leak scanner's own source 的豁免理由）。
    "verify/leakscan.py": {"*": "形状规则的源码，天然含被禁形态的片段"},
    # 同理：坏样本必须住在自证里，否则「它会不会红」无从证明。
    "verify/selftest_leakscan.py": {"*": "自证里故意注入的坏样本"},
    "verify/selftest_ledger_discipline.py": {
        "e:/x/y/z.md": "用例 J 的合成占位路径（非真机路径）",
        # 同一份占位路径的 JSON 转义形态（双反斜杠写法）会**顺带**命中 UNC 规则 —— 是转义串
        # 长得像 UNC，不是真 UNC。规则不为此收窄（收窄 = 缩小检测面），改用逐条豁免把它留在明面上。
        "/x/y/z.md": "同一占位路径的转义形态（归一后前导斜杠只剩一个），S2 在转义串上也会命中",
    },
}

SHAPE_RULES = {name for name, _rx in SHAPES}
TERM_RULE = "L2-term"


def norm(s):
    """命中串归一：斜杠折叠、去空白、小写 —— 好让 `E:\\\\x` 与 `E:/x` 对上同一条豁免。"""
    return re.sub(r"[\\/]+", "/", s or "").strip().lower()


def _rel(p, root):
    """输出里只留相对路径 / 末段。本闸的 stdout 是**发布面**：
    the export step 会把它折进下一条提交信息，所以它自己不许带机器绝对路径。"""
    if not p:
        return "(默认)"
    try:
        r = os.path.relpath(p, root)
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return os.path.basename(p)


def load_terms(path):
    """→ (terms, status)。status: RAN（读到词表，哪怕是空的）/ MISSING（读不到）。"""
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
    """大小写不敏感 + 词边界。CJK 词天然落在 [A-Za-z0-9] 之外，同样按子串命中。"""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.I)


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def scan(root, terms_path=None, terms_required=True, allow=None, explain=False):
    """在进程内可调用（自证不靠子进程 —— cron 无 PATH）。→ (rc, lines)"""
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

    lines = ["leakscan root=…%s"
             % os.path.basename(root)]
    if tstat == "RAN":
        lines.append("  词表 %s（%d 词）" % (_rel(terms_path, root), len(terms)))
    if explain:
        for rel, rule, sample, reason in allowed:
            lines.append("  allow  %s: %s -> %r  （%s）" % (rel, rule, sample[:60], reason))
    for rel, rule, sample in hits:
        lines.append("  LEAK   %s: %s -> %r" % (rel, rule, sample[:60]))

    lines.append("L1 形状层  rules=%d files=%d hits=%d  %s" % (
        len(SHAPES), scanned, len(sh), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("L2 字面层  **未跑**（--shape-only）：本次运行的绿**不证明**字面词那一半")
    elif tstat == "MISSING":
        lines.append("L2 字面层  词表读不到（%s）→ UNDECIDABLE，不猜成 PASS"
                     % _rel(terms_path, root))
    else:
        lines.append("L2 字面层  terms=%d hits=%d  %s" % (
            len(terms), len(lt), "FAIL" if lt else "PASS"))
    if binary:
        lines.append("  注：跳过二进制文件 %d 个（本工具不判二进制里嵌的信息）" % binary)

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
    ap = argparse.ArgumentParser(description="公开仓隐私闸（机器路径 / 操作者字面词）")
    ap.add_argument("root", nargs="?", default=REPO)
    ap.add_argument("--terms", default=None,
                    help="词表路径。默认 <root>/_local/privterms.txt（gitignore）")
    ap.add_argument("--shape-only", action="store_true",
                    help="只跑 L1 形状层（CI 用；输出会明写「L2 未跑」）")
    ap.add_argument("--explain", action="store_true", help="逐条列出豁免及理由")
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
