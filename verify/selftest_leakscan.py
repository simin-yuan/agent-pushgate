#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_leakscan.py — 先证伪这道闸，再信任它

纪律一：控制组必须**自造**，不能是 live 的拷贝 —— 跟着被测对象一起沉的
对照不是对照。所以这里每条用例都在 temp 里现造一棵小树，**不读本仓真树**。
纪律二：跑不红的检查是装饰。S1/S2/S3/L2 各有一个必须变红的坏样本；
另有一个「不许红」的误报对照（URL 协议分隔不是盘符路径）。
纪律三：判据要能给出否定结果 —— `--shape-only` 那一条断言的是「输出里**没有** L2 命中」，
用 want_absent 声明，而不是只看 rc。

无子进程（cron 无 PATH）：全部在进程内调 `leakscan.scan()`。
本文件被 `the mutation (falsify) test` 用来打红闸自身（把 LS 换成一个恒绿的变异体 → 本自证必须报 FAILED）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leakscan as LS  # noqa: E402

# 坏样本。它们出现在本文件里是**故意的**：不写在自证里就证明不了闸会红。
UNC_SAMPLE = "mounted at \\\\fileserver\\share\\notes today\n"
DRIVE_SAMPLE = "产物落在 E:/build/out/report.json 里\n"
APPDATA_SAMPLE = "配置在 AppData/Local/Temp/x 下\n"
TERM_SAMPLE = "本页经 ZZPRIV 批准后发布\n"
CLEAN = "# Notes\n\nnothing operator-specific in here.\n"
URL_ONLY = "see https://github.com/example/example for the mirror\n"
PLACEHOLDER = "合成用例路径 E:/x/y/z.md 仅供判据说明\n"


def tree(d, files):
    for rel, text in files.items():
        p = os.path.join(d, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


def write_terms(d, terms):
    p = os.path.join(d, "_local", "privterms.txt")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# 自造词表\n" + "\n".join(terms) + "\n")
    return p


def run_case(d, files, terms=("zzpriv",), terms_missing=False, shape_only=False,
             allow=None):
    """在 d 里现造一棵树并扫一次。terms_missing=True 时指向一个不存在的词表。"""
    tree(d, files)
    if terms_missing:
        tp = os.path.join(d, "_local", "absent.txt")
    else:
        tp = write_terms(d, terms)
    return LS.scan(d, terms_path=tp, terms_required=not shape_only, allow=allow)


CASES = []


def expect(name, want_rc, subs=(), absent=(), **kw):
    CASES.append((name, want_rc, tuple(subs), tuple(absent), kw))


# A 自造干净对照：必须全绿（且零豁免 —— allow={} 证明干净树不靠白名单放行）
expect("A 自造干净对照（应全绿）", 0,
       ["L1 形状层", "hits=0  PASS", "L2 字面层", "rc=0"], files={"README.md": CLEAN},
       allow={})

# B 误报对照：URL 协议分隔不是盘符路径，**不许红**
expect("B URL 不许误报", 0, ["hits=0  PASS", "rc=0"],
       files={"README.md": CLEAN, "docs/x.md": URL_ONLY}, allow={})

# C S1 盘符路径 → 红
expect("C S1 盘符路径（应红）", 1, ["S1-drive-path", "LEAK", "L1 形状层", "FAIL"],
       files={"README.md": CLEAN, "docs/x.md": DRIVE_SAMPLE}, allow={})

# D S2 UNC → 红
expect("D S2 UNC 路径（应红）", 1, ["S2-unc-path", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": UNC_SAMPLE}, allow={})

# E S3 用户配置内部 → 红
expect("E S3 profile-internal（应红）", 1, ["S3-profile-internal", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": APPDATA_SAMPLE}, allow={})

# F L2 字面词（正文写大写形态，词表是小写 → 证明大小写不敏感）→ 红
expect("F L2 字面词（应红）", 1, ["L2-term", "L2 字面层  terms=1 hits=1  FAIL", "rc=1"],
       files={"README.md": CLEAN, "docs/x.md": TERM_SAMPLE}, allow={})

# G 词表读不到 = UNDECIDABLE，不许猜成 PASS
expect("G 缺词表=UNDECIDABLE（不许 PASS）", 2,
       ["词表读不到", "UNDECIDABLE", "rc=2"],
       files={"README.md": CLEAN}, terms_missing=True, allow={})

# H 豁免机制不是万能放行：同一条占位路径，无豁免红、有豁免绿
expect("H1 同一条占位路径 · 无豁免（应红）", 1, ["S1-drive-path", "rc=1"],
       files={"doc.md": PLACEHOLDER}, allow={})
expect("H2 同一条占位路径 · 有豁免（应绿）", 0, ["allowlisted=1", "rc=0"],
       files={"doc.md": PLACEHOLDER},
       allow={"doc.md": {"e:/x/y/z.md": "合成占位，非真机路径"}})

# I --shape-only：字面层没跑 → rc=0 但**输出里不许出现 L2 命中**，且必须明写「未跑」
expect("I shape-only 不证明字面层", 0,
       ["**未跑**", "不证明", "rc=0"], absent=["L2-term"],
       files={"docs/x.md": TERM_SAMPLE}, shape_only=True, allow={})


def no_abs_root_in_output():
    """把「这道闸的输出里不许出现机器绝对路径」变成可失败的检查。

    为什么要有它：本闸的 stdout 是**发布面** —— the export step 把它折进下一条提交信息
    （实测：本闸自己曾把 root 的绝对路径印进过输出）。
    一个把绝对路径当摘要行打出去的闸，自己就是泄漏源。这条判据能红：把 `_rel` 换回裸路径即可。
    """
    with tempfile.TemporaryDirectory(prefix="ow-leakscan-absroot-") as d:
        with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(CLEAN)
        tp = write_terms(d, ("zzpriv",))
        _rc, lines = LS.scan(d, terms_path=tp)
        blob = "\n".join(lines)
        leaked = [s for s in (d, d.replace("\\", "/"), tp, tp.replace("\\", "/"))
                  if s in blob]
        if leaked:
            print
            return False
        if os.path.basename(d) not in blob:
            print("  [MISS] 闸的输出里连末段都没有：判据退化成「什么都不说」")
            return False
    print("  [PASS] 闸的输出不含绝对根路径/词表绝对路径（只印末段与相对路径）")
    return True


def main():
    fails = []
    extra = [no_abs_root_in_output()]
    with tempfile.TemporaryDirectory(prefix="ow-leakscan-") as root:
        for i, (name, want_rc, subs, absent, kw) in enumerate(CASES):
            d = os.path.join(root, "c%d" % i)
            os.makedirs(d, exist_ok=True)
            rc, lines = run_case(d, **kw)
            blob = "\n".join(lines)
            miss = [s for s in subs if s not in blob]
            unexpected = [s for s in absent if s in blob]
            ok = (rc == want_rc) and not miss and not unexpected
            print("[%s] %s  want_rc=%d got_rc=%d%s%s" % (
                "OK" if ok else "MISS", name, want_rc, rc,
                "" if not miss else "  缺子串=%s" % miss,
                "" if not unexpected else "  不应出现=%s" % unexpected))
            for ln in lines:
                print("      | " + ln)
            if not ok:
                fails.append(name)
    bad = len(fails) + (0 if extra[0] else 1)
    print("\nSELFTEST %s  cases=%d failed=%d" % (
        "OK" if not bad else "FAILED", len(CASES) + 1, bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
