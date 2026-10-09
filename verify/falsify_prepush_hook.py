#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""falsify_prepush_hook.py — 先证伪检查，再信任它（自检本身必须会红）

对三个部件各做一次「改坏」的变异，然后把**对应**的用例跑在变异副本上：
变异体必须在相关用例上红；对照组（不改）必须绿。打不红的检查是装饰。

  F1 tools/git-hooks/pre-push  拒绝分支改成不放行也返回 0  -> case_b/c/d 必红
  F2 verify/prepush_gate.py    UNDECIDABLE 被折成 PASS      -> case_d 必红
  F3 tools/install_git_hooks.py 内容漂移检查被短路          -> case_h 必红
  C  对照组：不改任何东西                                    -> case_a 必绿

用法：python verify/falsify_prepush_hook.py  → 末行 N/N，rc=0 表示变异全被抓住。
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, os.pardir))
MUTATIONS = [
    ("F1 hook 永不放行也不拒绝",
     os.path.join("tools", "git-hooks", "pre-push"),
     '[ "$rc" -eq 0 ] || refuse "隐私闸非 0',
     '[ "$rc" -eq 0 ] || true #'),
    ("F2 UNDECIDABLE 折成 PASS",
     os.path.join("verify", "prepush_gate.py"),
     "    elif UNDECIDABLE in rcs:\n        out = UNDECIDABLE",
     "    elif UNDECIDABLE in rcs:\n        out = PASS"),
    ("F3 漂移检查被短路",
     os.path.join("tools", "install_git_hooks.py"),
     "    if isha != tsha:",
     "    if False:"),
]
CASES = {"F1": ["case_b", "case_c", "case_d"],
         "F2": ["case_d"],
         "F3": ["case_h"]}


def make_copy(tag):
    dst = os.path.join(tempfile.mkdtemp(prefix="falsify-%s-" % tag.lower()), "copy")
    os.makedirs(os.path.join(dst, "tools", "git-hooks"))
    shutil.copytree(os.path.join(SRC, "verify"), os.path.join(dst, "verify"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(os.path.join(SRC, "tools", "install_git_hooks.py"),
                    os.path.join(dst, "tools", "install_git_hooks.py"))
    shutil.copyfile(os.path.join(SRC, "tools", "git-hooks", "pre-push"),
                    os.path.join(dst, "tools", "git-hooks", "pre-push"))
    return dst


def load_mod(dst, tag):
    spec = importlib.util.spec_from_file_location(
        "st_%s" % tag, os.path.join(dst, "verify", "selftest_prepush_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cases(mod, names):
    root = tempfile.mkdtemp(prefix="falsify-run-")
    res = mod.Res()
    try:
        for n in names:
            try:
                getattr(mod, n)(root, res)
            except Exception as e:                                 # noqa: BLE001
                res.ok(False, "%s 抛异常" % n, "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return res


def main():
    print("对照组（不改任何东西）")
    cdir = make_copy("C")
    cmod = load_mod(cdir, "C")
    cres = run_cases(cmod, ["case_a"])
    ok_control = not cres.fails
    print("  %s  对照组 case_a（%d 断言）" % ("PASS" if ok_control else "FAIL", cres.n))

    total, caught = 1, 1 if ok_control else 0
    for label, path, a, b in MUTATIONS:
        tag = label.split()[0]
        dst = make_copy(tag)
        target = os.path.join(dst, path)
        with open(target, encoding="utf-8", newline="") as fh:
            src = fh.read()
        if a not in src:
            print("  UNDECIDABLE  %s：找不到要改的锚点（判据自己漂了）" % label)
            total += 1
            continue
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(src.replace(a, b, 1))
        mod = load_mod(dst, tag)
        res = run_cases(mod, CASES[tag])
        total += 1
        if res.fails:
            caught += 1
            print("  PASS  %s -> 变异体被 %s 抓住（%d 断言里红 %d）"
                  % (label, "/".join(CASES[tag]), res.n, len(res.fails)))
        else:
            print("  FAIL  %s -> 变异体没被抓住（这套自检是装饰）" % label)
        shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
    shutil.rmtree(os.path.dirname(cdir), ignore_errors=True)
    print("\nSUMMARY %d/%d" % (caught, total))
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
