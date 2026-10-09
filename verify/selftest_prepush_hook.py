#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_prepush_hook.py — pre-push 那道闸的自证（全在临时夹具仓里，不碰本仓）

判据能不能给出否定结果：能。每条用例都是一次**真实的 git push**，只看两样硬证据：
  ① 远端分支 ref 动没动（`git rev-parse main` on the bare remote）
  ② 闸的留痕日志（<夹具>/.git/prepush_gate.log）有没有新行、rc 是几
不看代码里写了什么，也不看人说了什么。

用例
  A 绿路        干净提交 -> push 成功、远端 ref == 本地 HEAD（闸不许误拦）
  B 树脏        提交体里带词表的字面词 -> 拒绝、远端 ref 不动、日志 rc=1
  C 信息脏      提交信息里带盘符路径形状 -> 拒绝、远端 ref 不动
  D 词表缺      没有词表 -> 三闸 UNDECIDABLE(2) -> 拒绝（「没查」不许当「干净」）
  E 无 PATH + sidecar     -> 闸照样跑（日志新行 rc=0）、push 成功（cron 式环境）
  F 无 PATH 且无 sidecar  -> 找不到解释器 -> 拒绝（fail-closed，不是静默放行）
  G --no-verify 旁路      -> **能绕过**：远端照动、日志无新行（把这个洞测出来，不假装它不存在）
  H 装了又改            -> install --check 必须 rc=1（内容漂移）
  I core.hooksPath 改指 -> install --check 必须 rc=1（装了等于没装）

用法：python verify/selftest_prepush_hook.py   → 末行 N/N，全过 rc=0。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
import pushscope_gate                                              # noqa: E402

GIT = pushscope_gate.find_git()
#: 坏样本按**拼接**造，不写字面量 —— 本文件也在这道闸的扫描面里。
SHAPE = "E" + ":" + "/" + "tmp" + "/" + "sample.txt"
TERM = "zq" + "wflagterm"
ENV = dict(os.environ)
ENV.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GIT_ADVICE": "0"})


class Res:
    def __init__(self):
        self.n = 0
        self.fails = []

    def ok(self, cond, label, detail=""):
        self.n += 1
        if not cond:
            self.fails.append("%s | %s" % (label, detail))
        print("  %s  %s%s" % ("PASS" if cond else "FAIL", label,
                              ("  [" + detail + "]") if (detail and not cond) else ""))


def git(args, cwd, env=None, check=True):
    r = subprocess.run([GIT] + list(args), cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env or ENV, timeout=180)
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), (r.stderr or "")[-400:]))
    return r


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def build(root, name, with_terms=True):
    """造一个夹具仓：bare 远端 + 工作仓（含本仓的闸与安装器），基线**在装 hook 之前**推上去。"""
    remote = os.path.join(root, name + "-remote.git")
    work = os.path.join(root, name + "-work")
    git(["init", "--bare", "-b", "main", remote], cwd=root)
    os.makedirs(work)
    git(["init", "-b", "main"], cwd=work)
    git(["config", "user.email", "selftest@example.invalid"], cwd=work)
    git(["config", "user.name", "selftest"], cwd=work)
    shutil.copytree(os.path.join(SRC, "verify"), os.path.join(work, "verify"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(work, "tools", "git-hooks"))
    for rel in (os.path.join("tools", "install_git_hooks.py"),
                os.path.join("tools", "git-hooks", "pre-push")):
        shutil.copyfile(os.path.join(SRC, rel), os.path.join(work, rel))
    write(os.path.join(work, ".gitignore"), "_local/\n")
    if with_terms:
        write(os.path.join(work, "_local", "privterms.txt"),
              "# 夹具词表（自造词，不是真词）\n" + TERM + "\n")
    write(os.path.join(work, "README.md"), "fixture\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "baseline"], cwd=work)
    git(["remote", "add", "origin", remote], cwd=work)
    git(["push", "-u", "origin", "main"], cwd=work)
    return work, remote


def install(work):
    return subprocess.run([sys.executable, os.path.join(work, "tools", "install_git_hooks.py")],
                          cwd=work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=180)


def check(work):
    return subprocess.run([sys.executable, os.path.join(work, "tools", "install_git_hooks.py"),
                           "--check"],
                          cwd=work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=180)


def remote_head(remote):
    r = git(["rev-parse", "main"], cwd=remote, check=False)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def local_head(work):
    return (git(["rev-parse", "HEAD"], cwd=work).stdout or "").strip()


def log_lines(work):
    p = os.path.join(work, ".git", "prepush_gate.log")
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8", errors="replace") as fh:
        return [l for l in fh.read().splitlines() if l.strip()]


def strip_path(root):
    """cron 式环境：PATH 里**没有 python**（其它一律不动，git 照常工作）。

    做法：把原 PATH 里「含 python/python3/py」的那些目录摘掉。比整条 PATH 清空更接近
    要问的那个问题（闸能不能在没有 python 的 PATH 下靠 sidecar 起来），也不会把 git 自己
    搞残（实测：PATH 清空/只留 git 那一层时，git 自己就崩了，那样的红不说明闸的事）。
    """
    parts = [p for p in (ENV.get("PATH") or "").split(os.pathsep) if p]
    keep = [p for p in parts if not path_has_python(p)]
    env = dict(ENV)
    env["PATH"] = os.pathsep.join(keep)
    env["Path"] = os.pathsep.join(keep)
    return env, keep


def path_has_python(path):
    for name in ("python", "python3", "py"):
        if shutil.which(name, path=path):
            return True
    return False


def last_log_rc(work):
    """→ 最后一行留痕里的 rc（没有日志 = None）。"""
    ls = log_lines(work)
    if not ls:
        return None
    m = re.search(r"rc=(\d+)", ls[-1])
    return int(m.group(1)) if m else None


def push(work, env=None, extra=()):
    return git(["push"] + list(extra) + ["origin", "main"], cwd=work, env=env, check=False)


def case_a(root, res):
    work, remote = build(root, "a")
    install(work)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add a clean note"], cwd=work)
    r = push(work)
    res.ok(r.returncode == 0, "A1 干净范围 push 成功", (r.stderr or "")[-200:])
    res.ok(remote_head(remote) == local_head(work), "A2 远端 ref 已前进到本地 HEAD")
    res.ok(last_log_rc(work) == 0, "A3 留痕日志 rc=0（闸真的为这次 push 跑过）",
           "rc=%s" % last_log_rc(work))


def case_b(root, res):
    work, remote = build(root, "b")
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "leak.txt"), "harmless prefix " + TERM + " suffix\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add sample"], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "B1 树里带字面词 -> push 被拒", "rc=%s" % r.returncode)
    res.ok("REFUSED" in (r.stderr or ""), "B2 拒绝理由出现在 stderr")
    res.ok(remote_head(remote) == before, "B3 远端 ref 未动")
    res.ok(last_log_rc(work) == 1, "B4 留痕日志 rc=1", "rc=%s" % last_log_rc(work))


def case_c(root, res):
    work, remote = build(root, "c")
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: note about " + SHAPE], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "C1 提交信息里带路径形状 -> push 被拒", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == before, "C2 远端 ref 未动")
    res.ok(last_log_rc(work) == 1, "C3 留痕日志 rc=1", "rc=%s" % last_log_rc(work))


def case_d(root, res):
    work, remote = build(root, "d", with_terms=False)
    install(work)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: add a clean note"], cwd=work)
    r = push(work)
    res.ok(r.returncode != 0, "D1 没有词表 -> 拒绝（没查 != 干净）", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == before, "D2 远端 ref 未动")
    res.ok(last_log_rc(work) == 2, "D3 留痕日志 rc=2（UNDECIDABLE）", "rc=%s" % last_log_rc(work))


def case_e(root, res):
    work, remote = build(root, "e")
    install(work)
    env, keep = strip_path(root)
    res.ok(not path_has_python(os.pathsep.join(keep)), "E0 前提：这条 PATH 里确实没有 python",
           "keep=%d 项" % len(keep))
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work, env=env)
    git(["commit", "-m", "docs: add a clean note"], cwd=work, env=env)
    r = push(work, env=env)
    ls = log_lines(work)
    res.ok(r.returncode == 0, "E1 PATH 里没有 python/git 时仍 push 成功（走 sidecar）",
           (r.stderr or "")[-200:])
    res.ok(remote_head(remote) == local_head(work), "E2 远端 ref 已前进")
    res.ok(last_log_rc(work) == 0, "E3 闸真的跑了（留痕 rc=0）", "rc=%s" % last_log_rc(work))


def case_f(root, res):
    work, remote = build(root, "f")
    install(work)
    os.remove(os.path.join(work, ".git", "hooks", "pre-push.python"))
    env, keep = strip_path(root)
    before = remote_head(remote)
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work, env=env)
    git(["commit", "-m", "docs: add a clean note"], cwd=work, env=env)
    r = push(work, env=env)
    res.ok(r.returncode != 0, "F1 找不到解释器 -> 拒绝（fail-closed）", "rc=%s" % r.returncode)
    res.ok("解释器" in (r.stderr or "") or "REFUSED" in (r.stderr or ""), "F2 拒绝理由出现在 stderr")
    res.ok(remote_head(remote) == before, "F3 远端 ref 未动")
    res.ok(not log_lines(work), "F4 日志为空（闸确实没跑起来，没有被伪装成跑了）")


def case_g(root, res):
    work, remote = build(root, "g")
    install(work)
    before_lines = len(log_lines(work))
    write(os.path.join(work, "notes.md"), "clean line\n")
    git(["add", "-A"], cwd=work)
    git(["commit", "-m", "docs: note about " + SHAPE], cwd=work)
    r = push(work, extra=("--no-verify",))
    res.ok(r.returncode == 0, "G1 --no-verify 能绕过（洞被证实，不是假设）", "rc=%s" % r.returncode)
    res.ok(remote_head(remote) == local_head(work), "G2 脏提交信息照样推出去了")
    res.ok(len(log_lines(work)) == before_lines, "G3 日志无新行 —— 旁路不留痕迹，这是已知边界")


def case_h(root, res):
    work, remote = build(root, "h")
    install(work)
    with open(os.path.join(work, ".git", "hooks", "pre-push"), "a", encoding="utf-8") as fh:
        fh.write("# tampered\n")
    r = check(work)
    res.ok(r.returncode == 1, "H1 已装 hook 被改 -> --check rc=1", "rc=%s stdout=%s"
           % (r.returncode, (r.stdout or "")[-160:].replace("\n", " / ")))


def case_i(root, res):
    work, remote = build(root, "i")
    install(work)
    other = os.path.join(root, "i-otherhooks")
    os.makedirs(other, exist_ok=True)
    git(["config", "core.hooksPath", other], cwd=work)
    r = check(work)
    res.ok(r.returncode == 1, "I1 core.hooksPath 改指 -> --check rc=1（装了等于没装）",
           "rc=%s stdout=%s" % (r.returncode, (r.stdout or "")[-160:].replace("\n", " / ")))


def main():
    if not GIT:
        print("ERROR：找不到 git，无法跑夹具")
        return 3
    res = Res()
    root = tempfile.mkdtemp(prefix="prepush-selftest-")
    try:
        print("夹具根：%s（跑完删）" % os.path.basename(root))
        for fn in (case_a, case_b, case_c, case_d, case_e, case_f, case_g, case_h, case_i):
            print("· %s" % fn.__name__)
            try:
                fn(root, res)
            except Exception as e:                                 # noqa: BLE001
                res.ok(False, "%s 抛出异常" % fn.__name__, "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nSUMMARY %d/%d" % (res.n - len(res.fails), res.n))
    for f in res.fails:
        print("  FAILED %s" % f)
    return 0 if not res.fails else 1


if __name__ == "__main__":
    sys.exit(main())
