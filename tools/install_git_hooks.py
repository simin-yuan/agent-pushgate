#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""install_git_hooks.py — 把仓里的 hook 模板装进本机 .git/hooks/，并**能自证装着**

为什么不是「直接写 .git/hooks/pre-push」：.git/ 不进版本控制 —— 写进去的东西别人看不见、
自己也会忘，而且没人能验证「装的那份还是不是仓里那份」。所以拆成三段：
  模板进仓（可 review、可 diff） → 安装器落到本机 → `--check` 证明两者逐字节相同。

「声明 → 可失败的检查」：--check 会红在四件事上 ——
  ① 没装；② 装了但内容漂移（有人手改过已装的 hook）；③ 解释器 / git 的 sidecar 失效
  （cron 服务没有 PATH，sidecar 没了 hook 会 fail-closed 拒绝一切 push）；
  ④ core.hooksPath 指向别处 —— 设了它 .git/hooks/ 就被忽略，**装了等于没装**。

用法
    python tools/install_git_hooks.py            # 安装（幂等）；装完自动跑一次 --check
    python tools/install_git_hooks.py --check    # 只检查，不动盘
    python tools/install_git_hooks.py --repo R   # 指定仓（默认 = 本脚本所在仓）
退出码 PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))
TEMPLATE_REL = os.path.join("tools", "git-hooks", "pre-push")
HOOK = "pre-push"
#: sidecar 名 -> 用途（写给 hook 读；hook 不靠 PATH 找解释器与 git）
SIDECARS = {"pre-push.python": "python 解释器", "pre-push.git": "git 可执行文件"}
PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def find_git():
    """复用闸自己的候选查找（不靠 which；cron 无 PATH）。"""
    sys.path.insert(0, os.path.join(REPO, "verify"))
    try:
        import pushscope_gate
        return pushscope_gate.find_git()
    except Exception:                                            # noqa: BLE001
        return shutil.which("git")


def paths(repo):
    hooks = os.path.join(repo, ".git", "hooks")
    return hooks, os.path.join(hooks, HOOK), os.path.join(repo, TEMPLATE_REL)


def check(repo):
    """→ (rc, lines)。只读，不写盘。"""
    hooks, installed, template = paths(repo)
    lines = ["install_git_hooks --check repo=…%s" % os.path.basename(repo)]
    rc_notes = []
    if not os.path.isfile(template):
        lines.append("  UNDECIDABLE：仓里没有模板 %s" % TEMPLATE_REL)
        lines.append("SUMMARY template=- installed=- rc=%d" % UNDECIDABLE)
        return UNDECIDABLE, lines
    tsha = sha256(template)
    if not os.path.isfile(installed):
        lines.append("  FAIL：未安装（%s 不存在）" % HOOK)
        lines.append("  template sha256=%s" % tsha[:16])
        lines.append("SUMMARY template=%s installed=- rc=%d" % (tsha[:16], FAIL))
        return FAIL, lines
    isha = sha256(installed)
    if isha != tsha:
        lines.append("  FAIL：装了但内容漂移 —— 已装的与模板不是同一份")
        lines.append("  template sha256=%s / installed sha256=%s" % (tsha[:16], isha[:16]))
        rc_notes.append(FAIL)
    else:
        lines.append("  内容一致 template=installed sha256=%s" % tsha[:16])
    for name, why in SIDECARS.items():
        sp = os.path.join(hooks, name)
        if not os.path.isfile(sp):
            lines.append("  FAIL：缺 sidecar %s（%s）—— cron 无 PATH 时 hook 会拒绝一切 push" % (name, why))
            rc_notes.append(FAIL)
            continue
        with open(sp, encoding="utf-8", errors="replace") as fh:
            val = fh.read().strip()
        if not val or not os.path.isfile(val):
            lines.append("  FAIL：sidecar %s 指向的解释器/程序不存在（%s）" % (name, why))
            rc_notes.append(FAIL)
        else:
            lines.append("  sidecar %s -> %s（%s）" % (name, os.path.basename(val), why))
    g = find_git()
    if not g:
        lines.append("  UNDECIDABLE：本进程找不到 git，无法核对 core.hooksPath")
        rc_notes.append(UNDECIDABLE)
    else:
        r = subprocess.run([g, "-C", repo, "config", "--get", "core.hooksPath"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        hp = (r.stdout or "").strip()
        if hp:
            same = os.path.normcase(os.path.abspath(os.path.join(repo, hp))) == \
                os.path.normcase(os.path.abspath(hooks))
            if same:
                lines.append("  core.hooksPath=%s（指向本 hooks 目录，一致）" % hp)
            else:
                lines.append("  FAIL：core.hooksPath=%s 指向别处 —— .git/hooks/ 被忽略，装了等于没装" % hp)
                rc_notes.append(FAIL)
        else:
            lines.append("  core.hooksPath 未设置（默认 .git/hooks/，生效）")
    if FAIL in rc_notes:
        rc = FAIL
    elif UNDECIDABLE in rc_notes:
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY template=%s installed=%s rc=%d" % (tsha[:16], isha[:16], rc))
    return rc, lines


def install(repo):
    hooks, installed, template = paths(repo)
    if not os.path.isfile(template):
        print("ERROR：仓里没有模板 %s" % TEMPLATE_REL)
        return UNDECIDABLE
    os.makedirs(hooks, exist_ok=True)
    shutil.copyfile(template, installed)
    try:
        os.chmod(installed, 0o755)
    except OSError:
        pass
    # python sidecar：写成 **正斜杠** 形态 —— hook 是 sh 脚本，反斜杠会被当转义吃掉。
    py = os.path.abspath(sys.executable).replace("\\", "/")
    with open(os.path.join(hooks, "pre-push.python"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(py + "\n")
    g = find_git()
    if g:
        with open(os.path.join(hooks, "pre-push.git"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(os.path.abspath(g).replace("\\", "/") + "\n")
    else:
        print("注：本进程找不到 git，不写 pre-push.git（hook 里那一步会交给闸自己找）")
    print("installed %s" % installed)
    print("  template  sha256=%s bytes=%d" % (sha256(template), os.path.getsize(template)))
    print("  installed sha256=%s bytes=%d" % (sha256(installed), os.path.getsize(installed)))
    print("  sidecar   pre-push.python -> %s" % os.path.basename(py))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="装/查 git hooks（模板进仓，装的这份可自证）")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if not a.check:
        install(a.repo)
    rc, lines = check(a.repo)
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
