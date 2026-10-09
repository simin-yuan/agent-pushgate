#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""selftest_bridge_landed.py — 证明 bridge_landed.py 能说是，也能说不。

做法：在临时目录里自造一个**本地裸仓**当远程，用真的 git 摆出四种状态，
四种状态都走 bridge_landed 的**真路径**（argv → 取 sha → 判定 → 打印 → 退出码），不 mock。

    A 落地        commit + push              → rc 0 / LANDED
    B 卡住        再 commit（时间戳 5h 前）不推 → rc 1 / STALE
    C 恢复        推上去                      → rc 0 / LANDED（证明它不是恒红）
    D 容忍窗内    再 commit（当前时间）不推     → rc 0 / PENDING
    E 取不到远程  remote 指向不存在的路径      → rc 2 / UNDECIDABLE
    F 那 2 算不算通过                          → 显式断言 E 的 rc != 0

不读 live 仓、不联网、不依赖 _local/。--target 可指向模块的**被改坏的副本**，
好让「先把检查改坏、自证必须跟着红」这件事有地方做。

用法
    python verify/selftest_bridge_landed.py [--target PATH] [--git PATH]
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
MAX_AGE_H = 2.0


def load(target):
    spec = importlib.util.spec_from_file_location("bridge_landed_target", target)
    if spec is None or spec.loader is None:
        raise RuntimeError("load 不了目标模块：%s" % target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(gitp, cwd, args, env_extra=None, check=True):
    env = dict(os.environ)
    env.update(env_extra or {})
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    r = subprocess.run([gitp, "-c", "user.name=selftest",
                        "-c", "user.email=selftest@example.invalid",
                        "-c", "commit.gpgsign=false"] + args,
                       cwd=cwd, capture_output=True, text=True, env=env, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError("git %s rc=%d %s %s" % (args[:2], r.returncode,
                                                   r.stdout[-200:], r.stderr[-300:]))
    return r


def run_check(mod, work, gitp):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--repo", work, "--git", gitp, "--max-age-h", str(MAX_AGE_H)])
    return rc, buf.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.path.join(HERE, "bridge_landed.py"))
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    import bridge_landed as ref                                    # noqa: E402
    gitp = a.git or ref.find_git()
    if not gitp:
        print("RUNNER NOT READY   找不到 git —— 这种情形下不许报通过")
        print("SUMMARY cases=0 fail=0 rc=3")
        return 3

    mod = load(a.target)
    root = tempfile.mkdtemp(prefix="bridge_landed_selftest_")
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    cases, failures = [], []

    def expect(name, got_rc, want_rc, out, must_contain):
        ok = (got_rc == want_rc) and all(s in out for s in must_contain)
        cases.append((name, got_rc, want_rc, ok))
        if not ok:
            failures.append("%s: rc=%s(期望 %s) 缺 %r\n%s"
                            % (name, got_rc, want_rc,
                               [s for s in must_contain if s not in out], out[-500:]))
        return ok

    try:
        git(gitp, root, ["init", "-q", "--bare", "-b", "main", origin])
        git(gitp, root, ["init", "-q", "-b", "main", work])
        # 用**相对路径**当远程 URL：绝对路径在这里会被 file:// / 盘符 / MSYS 来回翻译坏掉
        git(gitp, work, ["remote", "add", "origin",
                         os.path.relpath(origin, work).replace("\\", "/")])
        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("1\n")
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", "one"])
        git(gitp, work, ["push", "-q", "-u", "origin", "main"])

        rc, out = run_check(mod, work, gitp)                       # A
        expect("A 落地", rc, 0, out, ["LANDED"])

        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("2\n")
        git(gitp, work, ["add", "-A"])
        # 时间戳用「现在 - 5h」摆，不写字面日期：字面日期会随时间推移失效（今天红、明天不红）
        git(gitp, work, ["commit", "-q", "-m", "two"],
            env_extra={"GIT_COMMITTER_DATE": _hours_ago(5)})
        rc, out = run_check(mod, work, gitp)                       # B
        expect("B 卡住 5h", rc, 1, out, ["STALE"])

        git(gitp, work, ["push", "-q", "origin", "main"])
        rc, out = run_check(mod, work, gitp)                       # C
        expect("C 推上去后恢复", rc, 0, out, ["LANDED"])

        with open(os.path.join(work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("3\n")
        git(gitp, work, ["add", "-A"])
        git(gitp, work, ["commit", "-q", "-m", "three"])
        rc, out = run_check(mod, work, gitp)                       # D
        expect("D 容忍窗内", rc, 0, out, ["PENDING"])

        git(gitp, work, ["remote", "set-url", "origin",
                         os.path.relpath(os.path.join(root, "nope.git"), work)
                         .replace("\\", "/")])
        rc, out = run_check(mod, work, gitp)                       # E
        expect("E 取不到远程", rc, 2, out, ["UNDECIDABLE"])
        if rc == 0:                                                # F
            failures.append("F: 取不到远程被读成了通过（rc=0）")
            cases.append(("F 未知不得读成通过", rc, "!=0", False))
        else:
            cases.append(("F 未知不得读成通过", rc, "!=0", True))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = len(cases) - len(failures)
    for name, got, want, ok in cases:
        print("  %-22s rc=%-6s 期望 %-6s %s" % (name, got, want, "OK" if ok else "**不符**"))
    for f in failures:
        print("  FAIL %s" % f)
    print("SUMMARY cases=%d pass=%d fail=%d target=%s rc=%d"
          % (len(cases), passed, len(failures), os.path.basename(a.target),
             1 if failures else 0))
    return 1 if failures else 0


def _hours_ago(h):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=h)).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    sys.exit(main())
