#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bridge_landed.py — 导出**落地腿**到底落地了没有？

一句话背景：export job 在本机 commit 一次，再 push。**本地 HEAD 前进 ≠ 远程收到。**
实测过一次：本地多出一个提交、导出步骤的 stdout 空、记录仍 ok，
而远程 main 原地不动 —— 从「本机看」一切正常，从「外面看」这次推送已经停了。

本判据只回答一件事：本地领先 `origin/<branch>` 的提交里，**最老的那个已经等了多久**。

用法
    python verify/bridge_landed.py [--repo R] [--remote origin] [--branch main]
                                   [--max-age-h 2.0] [--git PATH]

退出码
    0 PASS        落地（HEAD == 远程），或在容忍窗内刚提交（PENDING）
    1 FAIL        有提交卡在本地超过 --max-age-h（输出腿没落地）
    2 UNDECIDABLE 取不到远程（网络 / 工具缺失 / 远程对象不在本地）—— **不得当 0 读**
    3 ERROR       其它异常

判据能否给出否定结果
    能。`verify/selftest_bridge_landed.py` 用**本地裸仓**自造四种状态各跑一遍
    （落地 0 / 刚提交 0 / 卡住 1 / 取不到远程 2），四种状态全部走真的 git 调用，不 mock。
    `--max-age-h` 默认 2.0 是**选择**不是测量：导出节拍 12h、一次成功的推送是秒级，
    两小时还没落地只能是腿断了。改这个数 = 改判据。

范围 —— 本工具**不包含**什么（缺这一节，判据就会被当成它撑不住的东西）
    · 只比 sha；不判远程内容与本地内容是否一致。
    · 不判失败原因（凭证 / 网络 / 权限），**不读任何凭证**，不做任何网络写操作。
    · 不判 GitHub 侧的门跑没跑（那是 verify/check.py 的事）。
    · 年龄取提交者时间 `%cI`，且**依据本机时钟**；本机时钟错，年龄就错。
    · 远程 sha 走 `git ls-remote`（权威），**不**走本地 `origin/*` 跟踪 ref ——
      后者只由本机 push/fetch 更新，腿断了它自己不会知道（它当时还停在旧值上）。
    · 算得出「最老的未落地提交多大岁数」≠「它等推送等了多久」：后者本机测不到，
      这里只是用前者近似「上一次成功落地距今」。
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

DEFAULT_MAX_AGE_H = 2.0

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def find_git(explicit=None):
    """cron 服务没有 PATH，所以逐条候选找，不靠 which。"""
    cands = [explicit, os.environ.get("BRIDGE_GIT")]
    home = os.environ.get("HERMES_HOME")
    if home:
        cands.append(os.path.join(home, "git", "mingw64", "bin", "git.exe"))
    pf = os.environ.get("ProgramFiles")
    if pf:
        cands.append(os.path.join(pf, "Git", "cmd", "git.exe"))
    cands.append(shutil.which("git"))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    for c in cands:
        if c and not os.path.isabs(c) and shutil.which(c):
            return c
    return None


def git_run(git, repo, args, timeout=90):
    return subprocess.run([git] + args, cwd=repo, capture_output=True, text=True,
                          timeout=timeout)


def local_head(git, repo):
    r = git_run(git, repo, ["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def remote_head(git, repo, remote, branch):
    r = git_run(git, repo, ["ls-remote", remote, "refs/heads/%s" % branch], timeout=120)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out.split()[0] if out else None


def unpushed(git, repo, remote_sha):
    """→ [(sha, iso_committer_date)]，remote_sha..HEAD。远程对象不在本地 → None。"""
    r = git_run(git, repo, ["log", "--format=%H|%cI", "%s..HEAD" % remote_sha])
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if "|" in line:
            sha, iso = line.split("|", 1)
            rows.append((sha, iso))
    return rows


def oldest_age_h(rows, now=None):
    """最老的未落地提交距现在多少小时。解析不了一律返回 None（不猜 0）。"""
    stamps = []
    for _sha, iso in rows or []:
        try:
            stamps.append(datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc))
        except ValueError:
            return None
    if not stamps:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - min(stamps)).total_seconds() / 3600.0


def decide(local_sha, remote_sha, age_h, max_age_h):
    """纯判定：拿不到的那几种一律 UNDECIDABLE，绝不静默降级成 PASS。"""
    if not local_sha or not remote_sha:
        return UNDECIDABLE, "UNDECIDABLE", [
            "拿不到本地或远程 sha（远程没回答 / 工具缺失）—— 这不是「落地了」"]
    if local_sha == remote_sha:
        return PASS, "LANDED", ["本地 HEAD == 远程 %s" % local_sha[:10]]
    if age_h is None:
        return UNDECIDABLE, "UNDECIDABLE", [
            "本地领先远程，但算不出落后提交的年龄（时间戳解析失败 / 本地没有该远程对象）"]
    if age_h > max_age_h:
        return FAIL, "STALE", [
            "本地领先远程，最老的未落地提交已 %.2fh（> %.2fh）—— 输出腿没落地"
            % (age_h, max_age_h)]
    return PASS, "PENDING", [
        "本地领先远程，最老的未落地提交 %.2fh，仍在 %.2fh 容忍窗内"
        % (age_h, max_age_h)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地提交有没有真正推上去（本地 HEAD vs 远程 branch）")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H)
    ap.add_argument("--git", default=None)
    a = ap.parse_args(argv)

    try:
        git = find_git(a.git)
        if not git:
            print("R0 INTERNAL ERROR   找不到 git（cron 无 PATH，用 --git 显式给）")
            print("SUMMARY rc=%d" % ERROR)
            return ERROR
        lsha = local_head(git, a.repo)
        rsha = remote_head(git, a.repo, a.remote, a.branch)
        rows = unpushed(git, a.repo, rsha) if (lsha and rsha and lsha != rsha) else []
        age = oldest_age_h(rows)
        rc, status, notes = decide(lsha, rsha, age, a.max_age_h)
    except Exception as e:                                       # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=%d" % ERROR)
        return ERROR

    print("bridge_landed repo=%s remote=%s/%s max_age=%.2fh" % (a.repo, a.remote, a.branch,
                                                               a.max_age_h))
    print("  local  HEAD   %s" % (lsha or "(取不到)"))
    print("  remote %-6s %s" % (a.branch, rsha or "(取不到)"))
    if rows:
        print("  未落地提交 %d 个，最老 %.2fh：%s" % (len(rows), age if age else -1,
                                                      rows[-1][0][:10]))
    for n in notes:
        print("  %s" % n)
    print("SUMMARY status=%s rc=%d" % (status, rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
