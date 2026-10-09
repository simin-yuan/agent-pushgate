#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pushscope_gate.py — 待推提交信息闸：`git push` 送走的不止是树，还有每一条提交信息

为什么需要它
  `verify/leakscan.py` 扫的是**树**（= 马上要被 `git add -A` 提交、push 的文件内容）。
  但一次 push 送走的是**两样东西**：树，和 `<upstream>..HEAD` 每一条提交的 **message**。
  树干净、信息里带机器路径 —— 树上照样报 LEAKS=0，而那次 push 会把路径写进 GitHub 的永久历史。
  实测：一条数据提交的**标题**里就带着本机
  绝对路径，而树全程绿。来源已核到两端：导出步骤的 privacy_gate() 把闸的第一行
  打在最前面，自动提交取 `stdout.splitlines()[0]` 拼成提交标题。

范围 —— 本工具**不包含**什么（缺这一节，判据就会被当成它撑不住的东西）
  · 只扫 `<upstream>..HEAD` 各提交的 message，**不扫树**（那是 leakscan 的活，两面互补不重叠）。
  · **不扫已经在上游的提交**：推不走的不是这一次的风险（用例 D 守着这条线）。
  · 不扫 tag / 分支名 / reflog / stash / notes / PR 正文 —— 都是**未知**，不写成「没有」。
  · 不判语义换写（同音、拼音、缩写、拆分拼接），与 leakscan 同一条盲区。
  · **默认不回显命中样本的字面**（打码成「长度」）：本闸的输出会被 the export step 折进
    下一条提交信息 —— 一个回显它抓到的泄漏的闸，自己就是泄漏源。
    要看字面用 `--show-samples`，只在输出不进任何发布面时用；正常排障走 `git log -1 <sha>`。
  · 只认形状（盘符 / UNC / AppData）+ 本机词表；词表在 `_local/`（gitignore），干净 clone 上没有。

判据能否给出否定结果
  `verify/selftest_pushscope_gate.py` 自造本地裸仓当远程，七道用例各摆一种状态：
  干净信息(0) / 信息带盘符路径(1) / 信息带词表词(1) / 坏信息已在上游(0，范围外) /
  解析不到上游(2) / 打码两半（默认不回显 · `--show-samples` 回显）/ 词表读不到(2)。
  `verify/falsify_pushscope_gate.py` 再对本闸做三次变异（判红失能 / 永远回显 / 范围含已推），
  自证必须跟着红 —— 打不红的检查是装饰。CI 里也会跑（见 .github/workflows/freshness.yml）。

退出码
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3。FAIL 优先于 UNDECIDABLE。
  「找不到 git」「不是 git 仓」「解析不到上游」「词表读不到」一律 UNDECIDABLE —— 查不到 ≠ 干净。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def find_git(explicit=None):
    """逐条候选找 git，不靠 which（cron 服务没有 PATH）。

    候选**不写字面盘符路径**：本文件在公开仓里，任何「盘符 + 反斜杠」的形状都会命中
    leakscan 的 S1 层 —— 实测过，第一版把候选写成字面安装路径时，这道闸自己就红了
    。候选由环境变量拼出来（ProgramFiles 是 system 级
    变量，nssm 的 AppEnvironmentExtra 是**追加**，服务里仍然在）。
    """
    sys.path.insert(0, HERE)
    try:
        import bridge_landed                                   # noqa: E402
        g = bridge_landed.find_git(explicit)
        if g:
            return g
    except Exception:                                          # noqa: BLE001
        pass
    import shutil
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
               os.environ.get("ProgramFiles(x86)")):
        if pf:
            c = os.path.join(pf, "Git", "cmd", "git.exe")
            if os.path.isfile(c):
                return c
    return shutil.which("git")


def disp(path, root):
    """输出里只留相对路径 / 末段。本闸的 stdout 是**发布面**，
    绝对路径不进输出 —— 这条规矩对写闸的人同样成立。"""
    if not path:
        return "(默认)"
    try:
        r = os.path.relpath(path, root)
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return os.path.basename(path)


def run_git(gitp, repo, args, timeout=120):
    import subprocess
    return subprocess.run([gitp, "-C", repo] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def mask(sample):
    """命中样本的打码形态：只说它多长，不说它是什么。"""
    return "<<masked len=%d>>" % len(sample or "")


def resolve_upstream(gitp, repo, upstream):
    """→ (ref, status, why)。status: OK / UNDECIDABLE。"""
    if upstream:
        ref = upstream
    else:
        r = run_git(gitp, repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        ref = (r.stdout or "").strip()
        if r.returncode != 0 or not ref or ref == "@{u}":
            ref = ""
            for cand in ("origin/main", "origin/master"):
                c = run_git(gitp, repo, ["rev-parse", "--verify", "--quiet", cand + "^{commit}"])
                if c.returncode == 0 and (c.stdout or "").strip():
                    ref = cand
                    break
            if not ref:
                return None, "UNDECIDABLE", ("取不到上游：@{u} 未设置，"
                                             "origin/main / origin/master 也不在本地")
    v = run_git(gitp, repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    if v.returncode != 0 or not (v.stdout or "").strip():
        return ref, "UNDECIDABLE", "上游 %s 解析不到（本机无该 ref / 未 fetch）" % ref
    return ref, "OK", ""


def list_unpushed(gitp, repo, ref):
    """→ (records, err)。records = [(sha, message)]；err 非空 = 取不到（不是「没有」）。"""
    r = run_git(gitp, repo, ["log", "--format=%H%x1f%B%x1e", "%s..HEAD" % ref])
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:]
    recs = []
    for chunk in (r.stdout or "").split("\x1e"):
        chunk = chunk.strip("\r\n \t")
        if "\x1f" not in chunk:
            continue
        sha, msg = chunk.split("\x1f", 1)
        recs.append((sha.strip(), msg))
    return recs, ""


def scan_messages(repo, upstream=None, terms_path=None, terms_required=True,
                  show_samples=False, gitp=None):
    """进程内可调用（自证与 the export step 都走这里 —— cron 无 PATH，少一个静默失效的环节）。
    → (rc, lines)"""
    repo = os.path.abspath(repo)
    tag = "pushscope repo=…%s" % os.path.basename(repo)
    if terms_path is None:
        terms_path = os.path.join(repo, "_local", "privterms.txt")
    sys.path.insert(0, HERE)
    import leakscan as LS                                      # noqa: E402

    gitp = gitp or find_git()
    if not gitp:
        return UNDECIDABLE, [tag, "  UNDECIDABLE：找不到 git（cron 无 PATH）—— 没查 ≠ 干净",
                             "SUMMARY upstream=- commits=? LEAKS=? rc=%d" % UNDECIDABLE]
    if not os.path.isdir(os.path.join(repo, ".git")):
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE：这不是 git 仓（没有 .git）—— 查不到「将被 push 的东西」"
                             "不等于「没有东西会被 push」",
                             "SUMMARY upstream=- commits=? LEAKS=? rc=%d" % UNDECIDABLE]

    ref, ustat, uwhy = resolve_upstream(gitp, repo, upstream)
    lines = ["%s 上游=%s 范围=%s..HEAD" % (tag, ref or "(无)", ref or "(无)")]
    if ustat != "OK":
        lines.append("  UNDECIDABLE：%s" % uwhy)
        lines.append("SUMMARY upstream=%s commits=? LEAKS=? rc=%d" % (ref or "-", UNDECIDABLE))
        return UNDECIDABLE, lines

    recs, err = list_unpushed(gitp, repo, ref)
    if recs is None:
        lines.append("  UNDECIDABLE：git log %s..HEAD 取不到：%s" % (ref, err))
        lines.append("SUMMARY upstream=%s commits=? LEAKS=? rc=%d" % (ref, UNDECIDABLE))
        return UNDECIDABLE, lines

    terms, tstat = LS.load_terms(terms_path) if terms_required else ([], "SKIPPED")

    hits = []
    for sha, msg in recs:
        found, seen = [], set()
        for rule, rx in LS.SHAPES:
            for m in rx.finditer(msg):
                found.append((rule, m.group(0)))
        for t in terms:
            for m in LS.term_re(t).finditer(msg):
                found.append((LS.TERM_RULE, m.group(0)))
        for rule, sample in found:
            k = (rule, LS.norm(sample))
            if k in seen:
                continue
            seen.add(k)
            hits.append((sha, rule, sample))

    for sha, rule, sample in hits:
        shown = sample[:60] if show_samples else mask(sample)
        lines.append("  LEAK   %s: msg %s -> %s" % (sha[:12], rule, shown))
    sh = [h for h in hits if h[1] in LS.SHAPE_RULES]
    lt = [h for h in hits if h[1] == LS.TERM_RULE]
    lines.append("C1 提交信息 形状层  commits=%d rules=%d hits=%d  %s"
                 % (len(recs), len(LS.SHAPES), len(sh), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("C2 提交信息 字面层  **未跑**（terms_required=False）："
                     "本次运行的绿**不证明**字面词那一半")
    elif tstat == "MISSING":
        lines.append("C2 提交信息 字面层  词表读不到（%s）→ UNDECIDABLE，不猜成 PASS"
                     % disp(terms_path, repo))
    else:
        lines.append("C2 提交信息 字面层  terms=%d hits=%d  %s"
                     % (len(terms), len(lt), "FAIL" if lt else "PASS"))
    if hits and not show_samples:
        lines.append("  注：命中样本默认打码（只给长度）—— 本闸的输出会被折进下一条提交信息"
                     "。要看字面：git log -1 <sha>。")

    if hits:
        rc = FAIL
    elif tstat == "MISSING":
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY upstream=%s commits=%d LEAKS=%d rc=%d" % (ref, len(recs), len(hits), rc))
    return rc, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="待推提交信息闸（树之外的发布面）")
    ap.add_argument("--repo", default=REPO, help="被查的仓（默认 = 本脚本所在仓）")
    ap.add_argument("--upstream", default=None,
                    help="上游 ref（默认 @{u}，再退 origin/main、origin/master）")
    ap.add_argument("--terms", default=None,
                    help="词表路径。默认 <repo>/_local/privterms.txt（gitignore）")
    ap.add_argument("--shape-only", action="store_true",
                    help="只跑形状层（干净 clone 用；输出会明写「字面层未跑」）")
    ap.add_argument("--show-samples", action="store_true",
                    help="回显命中字面：**只在输出不进任何发布面时用**")
    ap.add_argument("--git", default=None, help="git 可执行文件路径（cron 无 PATH）")
    a = ap.parse_args(argv)
    try:
        rc, lines = scan_messages(a.repo, upstream=a.upstream, terms_path=a.terms,
                                  terms_required=not a.shape_only,
                                  show_samples=a.show_samples, gitp=a.git)
    except Exception as e:                                     # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=3")
        return ERROR
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
