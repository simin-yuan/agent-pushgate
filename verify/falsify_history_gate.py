#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""falsify_history_gate.py — 对 history_gate 做变异测试：**自证必须跟着红**

为什么：一道闸的自证绿了，不等于这道闸真的在看东西 —— 可能它压根没接上、或恒返回 0
（verify 纪律：打不红的检查是装饰）。做法：把闸复制到临时目录，注入三种**已知会让它失能**
的变异，然后跑它自己的自证：

| 变异 | 期望 |
|---|---|
| 失能判红（`rc = FAIL` 到达不了） | 自证红在 `B 中间提交的脏 blob` |
| 永不回显（`mask()` 直接返回样本） | 自证红在 `B 打码写明` |
| 把已推的对象也扫进来（范围改成 `HEAD`） | 自证红在 `D 已推的不在范围` |

对照（未变异副本）必须绿；**变异串没匹配上 = 这条变异没验证**，也算失败。

判红必须**红在对的用例上**（看用例名，不看退出码）—— 只数 rc 会把「模块加载崩了」当成
「闸被抓到了」（跑过验证 ≠ 验证的是那件事）。

**在临时目录里跑，不碰本仓**：闸 import 同级模块（leakscan / pushscope_gate），所以把这些
副本一起拷进临时目录，让 `HERE=tmp` 也能 import —— 这样仓里不会出现变异文件。

用法：python verify/falsify_history_gate.py [--repo PATH]
退出码 0 = 对照绿 + 三条变异全被抓到 / 1 = 有变异没被抓到或没打上 / 3 = 环境不足
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
NEED = ["history_gate.py", "selftest_history_gate.py", "leakscan.py",
        "pushscope_gate.py", "bridge_landed.py"]

#: (变异名, 原文, 替换成, 自证里应该变红的用例名)
MUTANTS = [
    ("失能判红（rc 恒 0）",
     "    if hits:\n        rc = FAIL",
     "    if False:\n        rc = FAIL",
     "B 中间提交的脏 blob（应红）"),
    ("永不回显（mask 直接返回样本）",
     'return "<<masked len=%d>>" % len(sample or "")',
     'return sample or ""',
     "B 打码写明（不只沉默）"),
    ("把已推的对象也扫进来",
     '"rev-list", "--objects", "%s..HEAD" % ref',
     '"rev-list", "--objects", "HEAD"',
     "D 已推的不在范围（应绿）"),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args(argv)
    vdir = os.path.join(os.path.abspath(a.repo), "verify")
    missing = [f for f in NEED if not os.path.isfile(os.path.join(vdir, f))]
    if missing:
        print("RUNNER NOT READY   缺文件：%s" % ", ".join(missing))
        return 3

    tmp = tempfile.mkdtemp(prefix="falsify_history_")
    for f in NEED:
        shutil.copy2(os.path.join(vdir, f), os.path.join(tmp, f))
    pristine = os.path.join(tmp, "history_gate.pristine.py")
    shutil.copy2(os.path.join(tmp, "history_gate.py"), pristine)

    def run():
        p = subprocess.run([sys.executable, os.path.join(tmp, "selftest_history_gate.py"),
                            "--target", os.path.join(tmp, "history_gate.py")],
                           cwd=tmp, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    control_rc, out = run()
    print("对照（未变异）  rc=%d  %s" % (control_rc, "OK" if control_rc == 0 else "**不符**"))
    if control_rc != 0:
        print(out[-1500:])
        print("RUNNER NOT READY   未变异副本自证就不绿，变异测试无从谈起")
        return 3

    ok, total = 0, len(MUTANTS)
    src_path = os.path.join(tmp, "history_gate.py")
    for name, old, new, case_label in MUTANTS:
        with open(pristine, encoding="utf-8") as f:
            body = f.read()
        if old not in body:
            print("  [MISS] %-26s 变异串没匹配上（原文已变？）—— 这条变异没验证" % name)
            continue
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body.replace(old, new, 1))
        rc, out = run()
        caught = (rc == 1) and (case_label in out)
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        print("  [%s] %-26s rc=%d  期望红在：%s" % ("OK" if caught else "MISS", name, rc, case_label))
        if not caught:
            print("        " + " | ".join(l.strip() for l in out.splitlines()[-4:]))
        ok += 1 if caught else 0

    print("SUMMARY falsify %d/%d（对照 rc=%d）" % (ok, total, control_rc))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
