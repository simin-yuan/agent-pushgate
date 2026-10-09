#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""history_gate.py — 待推范围里的**对象内容**闸：一次 push 送走的第三样东西

为什么需要它
  发布面已经被拆成两半、各有一道闸：
    ① 树        —— HEAD 的文件内容                 → verify/leakscan.py
    ② 提交信息   —— `<upstream>..HEAD` 的 message    → verify/pushscope_gate.py
  但 `git push` 送走的是**整个新对象集**（`git rev-list --objects <upstream>..HEAD`）。
  于是「一个文件在中间某个提交里带过敏感串、后来又被改干净」这种最普通的情形下：
  树闸绿（HEAD 干净）、信息闸绿（message 干净），而**那一版 blob 仍随这次 push 出去**、
  写进 GitHub 的永久历史 —— 不可回改。本闸就是那第三样。

范围 —— 本工具不包含什么（缺这一节，判据就会被当成它撑不住的东西）
  · 只扫 `<upstream>..HEAD` **范围内**的对象（= 这次真会传过去的东西）。已经在上游的对象不扫：
    推不走的不是这一次的风险。
  · 不扫 message（那是 pushscope_gate 的活）；不判 tag / 分支名 / reflog / stash / notes / PR 正文；
    不判**不可达对象**（rewrite 后的旧对象仍留在本地对象库与 reflog 里 —— 那是本机风险，
    不是发布面，要清是另一件事）。以上均为**未知**，不写成「没有」。
  · 不判语义换写（同音 / 拼音 / 缩写 / 拆分拼接），与 leakscan 同一条盲区。
  · 只扫文本：前 4KB 含 NUL 的 blob 跳过并计数 —— 本工具不判二进制里嵌的信息。
  · 与 leakscan 天然**重叠**（HEAD 的那些 blob 也在范围内）。价值在「只在中间提交里存在」的
    blob，故报告单列 novel 计数，让重叠部分可度量，而不是让读的人自己去分辨。
  · 同一内容挂在多个路径时（rev-list 只给首个路径）只报一条；命中样本默认打码（只给长度）——
    本闸的 stdout 会被 the export step 折进下一条提交信息。要看字面用
    `--show-samples`，只在输出不进任何发布面时用。
  · **豁免沿用树闸的同一份 ALLOW**（`leakscan.ALLOW`，逐条写明理由）。必须如此：范围内一大半
    命中来自形状规则**自己的源码**（写规则的文件天然含被禁形态），不给同一份豁免，这道闸就会
    永远红 —— 一道永远拒绝的闸和没有闸等价。同一处豁免在两道闸上必须给出同一个答案。

判据能否给出否定结果
  `verify/selftest_history_gate.py` 在 temp 里自造本地裸仓摆状态，核心用例是
  「中间提交引入脏 blob、后一个提交把它删掉」：**同一夹具上 leakscan(树) 必须 rc=0，本闸必须 rc=1**
  —— 那才是本闸存在的理由。另有：干净范围(0) / 打码两半 / 词表层 / 二进制跳过 / 已推的不在范围(0) /
  上游解析不到(2) / 词表读不到(2)。`verify/falsify_history_gate.py` 再对本闸注入三处失能变异，
  自证必须跟着红（按**用例名**核对红在对的那一道，只数 rc 会把模块崩了当成抓到了）。

退出码
  PASS=0 / FAIL=1 / UNDECIDABLE=2 / ERROR=3。FAIL 优先于 UNDECIDABLE。
  「找不到 git」「不是 git 仓」「解析不到上游」「词表读不到」一律 UNDECIDABLE —— 查不到 ≠ 干净。
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir))

PASS, FAIL, UNDECIDABLE, ERROR = 0, 1, 2, 3


def disp(path, root):
    """输出里只留相对路径 / 末段。本闸的 stdout 是**发布面**：
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


def mask(sample):
    """命中样本的打码形态：只说它多长，不说它是什么。"""
    return "<<masked len=%d>>" % len(sample or "")


def run_git(gitp, repo, args, timeout=180):
    """一次普通的 git 调用。cron 无 PATH，故 gitp 必须是显式路径（见 pushscope_gate.find_git）。"""
    return subprocess.run([gitp, "-C", repo] + args, capture_output=True, timeout=timeout,
                          text=True, encoding="utf-8", errors="replace")


def list_range_objects(gitp, repo, ref, timeout=180):
    """→ (items, err)。items = [(sha, path)]；path 可能为空串。err 非空 = 取不到（不是「没有」）。"""
    r = run_git(gitp, repo, ["rev-list", "--objects", "%s..HEAD" % ref], timeout=timeout)
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:]
    items = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, path = line.partition(" ")
        items.append((sha, path.strip()))
    return items, ""


def batch_types(gitp, repo, shas, timeout=180):
    """→ {sha: type}。用一次 --batch-check 取类型，不逐个起进程。"""
    if not shas:
        return {}
    inp = ("\n".join(shas) + "\n").encode("ascii")
    r = subprocess.run([gitp, "-C", repo, "cat-file", "--batch-check"],
                       input=inp, capture_output=True, timeout=timeout)
    out = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        p = line.split()
        if len(p) >= 2:
            out[p[0]] = p[1]
    return out


def read_blobs(gitp, repo, shas, timeout=300):
    """→ [(sha, bytes)]。只喂 --batch-check 已确认是 blob 的 sha（否则流解析会错位）。"""
    if not shas:
        return []
    inp = ("\n".join(shas) + "\n").encode("ascii")
    r = subprocess.run([gitp, "-C", repo, "cat-file", "--batch"],
                       input=inp, capture_output=True, timeout=timeout)
    out, i, items = r.stdout, 0, []
    while i < len(out):
        j = out.find(b"\n", i)
        if j < 0:
            break
        parts = out[i:j].decode("utf-8", "replace").split()
        if len(parts) < 3:
            i = j + 1                      # 「<sha> missing」这类没有 body 的行
            continue
        try:
            size = int(parts[2])
        except ValueError:
            i = j + 1
            continue
        items.append((parts[0], out[j + 1:j + 1 + size]))
        i = j + 1 + size + 1
    return items


def head_tree_blobs(gitp, repo, timeout=180):
    """HEAD 树里的 blob 集合 —— 用来算 novel（只存在于中间提交的那些）。"""
    r = run_git(gitp, repo, ["ls-tree", "-r", "HEAD"], timeout=timeout)
    s = set()
    for line in (r.stdout or "").splitlines():
        p = line.split()
        if len(p) >= 3:
            s.add(p[2])
    return s


def commits_touching(gitp, repo, ref, sha, timeout=120):
    """→ 该对象（增或删）出现在范围里的提交短 sha 列表（最多 8 个）。取不到就返回 []，不猜。"""
    r = run_git(gitp, repo, ["log", "--format=%h", "--find-object=%s" % sha, "%s..HEAD" % ref],
                timeout=timeout)
    if r.returncode != 0:
        return []
    return [x.strip() for x in (r.stdout or "").split() if x.strip()][:8]


def scan_history(repo, upstream=None, terms_path=None, terms_required=True,
                 show_samples=False, gitp=None):
    """进程内可调用（自证与 the export step 都走这里 —— cron 无 PATH，少一个静默失效的环节）。
    → (rc, lines)"""
    repo = os.path.abspath(repo)
    tag = "history repo=…%s" % os.path.basename(repo)
    if terms_path is None:
        terms_path = os.path.join(repo, "_local", "privterms.txt")
    sys.path.insert(0, HERE)
    import leakscan as LS                                       # noqa: E402
    import pushscope_gate as PG                                 # noqa: E402

    gitp = gitp or PG.find_git()
    if not gitp:
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE：找不到 git（cron 无 PATH）—— 没查 ≠ 干净",
                             "SUMMARY upstream=- commits=? blobs=? LEAKS=? rc=%d" % UNDECIDABLE]
    if not os.path.isdir(os.path.join(repo, ".git")):
        return UNDECIDABLE, [tag,
                             "  UNDECIDABLE：这不是 git 仓（没有 .git）—— 取不到「这次会传哪些对象」",
                             "SUMMARY upstream=- commits=? blobs=? LEAKS=? rc=%d" % UNDECIDABLE]

    ref, ustat, uwhy = PG.resolve_upstream(gitp, repo, upstream)
    lines = ["%s 上游=%s 范围=%s..HEAD" % (tag, ref or "(无)", ref or "(无)")]
    if ustat != "OK":
        lines.append("  UNDECIDABLE：%s" % uwhy)
        lines.append("SUMMARY upstream=%s commits=? blobs=? LEAKS=? rc=%d" % (ref or "-", UNDECIDABLE))
        return UNDECIDABLE, lines

    items, err = list_range_objects(gitp, repo, ref)
    if items is None:
        lines.append("  UNDECIDABLE：git rev-list --objects %s..HEAD 取不到：%s" % (ref, err))
        lines.append("SUMMARY upstream=%s commits=? blobs=? LEAKS=? rc=%d" % (ref, UNDECIDABLE))
        return UNDECIDABLE, lines

    shas = [s for s, _ in items]
    paths = {}
    for s, p in items:
        paths.setdefault(s, p)
    types = batch_types(gitp, repo, shas)
    blob_shas = sorted(s for s in shas if types.get(s) == "blob")
    head_blobs = head_tree_blobs(gitp, repo)
    novel = [s for s in blob_shas if s not in head_blobs]
    n_commits = sum(1 for s in shas if types.get(s) == "commit")

    terms, tstat = LS.load_terms(terms_path) if terms_required else ([], "SKIPPED")

    hits, allowed, binary, scanned = [], [], 0, 0
    for sha, body in read_blobs(gitp, repo, blob_shas):
        if b"\x00" in body[:4096]:
            binary += 1
            continue
        text = body.decode("utf-8", "replace")
        scanned += 1
        found, seen = [], set()
        for rule, rx in LS.SHAPES:
            for m in rx.finditer(text):
                found.append((rule, m.group(0)))
        for t in terms:
            for m in LS.term_re(t).finditer(text):
                found.append((LS.TERM_RULE, m.group(0)))
        rel = (paths.get(sha, "") or "").replace("\\", "/")
        cleared = LS.ALLOW.get(rel, {})          # 与树闸同一份豁免（理由缺一不可）
        for rule, sample in found:
            k = (rule, LS.norm(sample))
            if k in seen:
                continue
            seen.add(k)
            if rel and (LS.norm(sample) in cleared or "*" in cleared):
                allowed.append((sha, rel, rule, sample))
            else:
                hits.append((sha, rel, rule, sample))

    novel_set = set(novel)
    hits.sort(key=lambda h: (h[0], h[2]))
    for sha, path, rule, sample in hits:
        shown = sample[:60] if show_samples else mask(sample)
        who = ",".join(commits_touching(gitp, repo, ref, sha))
        where = "  [只在中间提交里 · 树闸看不见]" if sha in novel_set else "  [也在 HEAD 树里]"
        lines.append("  LEAK   blob %s %s: %s -> %s%s%s"
                     % (sha[:12], path or "(无路径)", rule, shown, where,
                        ("  涉及提交=%s" % who) if who else ""))

    sh = [h for h in hits if h[2] in LS.SHAPE_RULES]
    lt = [h for h in hits if h[2] == LS.TERM_RULE]
    lines.append("B0 范围       commits=%d objects=%d blobs=%d（只存在于中间提交的 %d）"
                 % (n_commits, len(shas), len(blob_shas), len(novel)))
    lines.append("B1 对象内容 形状层  blobs=%d rules=%d hits=%d 豁免=%d  %s"
                 % (scanned, len(LS.SHAPES), len(sh), len(allowed), "FAIL" if sh else "PASS"))
    if tstat == "SKIPPED":
        lines.append("B2 对象内容 字面层  **未跑**（terms_required=False）："
                     "本次运行的绿**不证明**字面词那一半")
    elif tstat == "MISSING":
        lines.append("B2 对象内容 字面层  词表读不到（%s）→ UNDECIDABLE，不猜成 PASS"
                     % disp(terms_path, repo))
    else:
        lines.append("B2 对象内容 字面层  terms=%d hits=%d  %s"
                     % (len(terms), len(lt), "FAIL" if lt else "PASS"))
    if binary:
        lines.append("  注：跳过二进制 blob %d 个（本工具不判二进制里嵌的信息）" % binary)
    if hits and not show_samples:
        lines.append("  注：命中样本默认打码（只给长度）—— 本闸的输出会被折进下一条提交信息"
                     "。要看字面：git cat-file blob <sha>。")

    if hits:
        rc = FAIL
    elif tstat == "MISSING":
        rc = UNDECIDABLE
    else:
        rc = PASS
    lines.append("SUMMARY upstream=%s commits=%d blobs=%d LEAKS=%d allowlisted=%d rc=%d"
                 % (ref, n_commits, len(blob_shas), len(hits), len(allowed), rc))
    return rc, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="待推范围内对象内容的隐私闸（树之外、信息之外的第三样）")
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
        rc, lines = scan_history(a.repo, upstream=a.upstream, terms_path=a.terms,
                                 terms_required=not a.shape_only,
                                 show_samples=a.show_samples, gitp=a.git)
    except Exception as e:                                      # noqa: BLE001
        print("R0 INTERNAL ERROR   %s: %s" % (type(e).__name__, e))
        print("SUMMARY rc=3")
        return ERROR
    for ln in lines:
        print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
