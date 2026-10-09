#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点检查（CI 与本地都能跑）。五项，都不含项目专属判断：

1. **仓库清单** —— 被 git 跟踪的文件必须落在允许清单内。**多出来的文件一律失败**
   （这是"往公开仓丢了个不该丢的文件"的第一道拦截）；
2. **通用痕迹** —— 绝对盘符路径 / `file:///` / localhost / `docs/<目录>/` 这类
   **放哪儿都不该出现**的写法（不点名任何具体目录，所以检查本身不泄露信息）；
3. **映射库载荷合法性** —— `wave/wave-mappings.js` 必须是能解析的 `window.WAVE_MAPPINGS = {...};`，
   四张表齐全且非空；
4. **关卡数据自洽** —— `data/levels-index.json` 列出的每一关都必须真有文件、磁盘上不得有索引外的
   孤儿文件、每个载荷都能解析（这是"点了 404"与"文件被写坏"的唯一自动拦截）。没有 `data/` 就跳过；
5. **相对资源与体积** —— 页面引用的资源必须存在且不得用根相对路径；站点 ≤ 1 MB、`data/` ≤ 64 MB。

页面清单（第 5 项）**自动发现**：根 `index.html` + 所有 `*/index.html` —— 加新工具时不必再登记一处。

为什么第 2 项只收"通用"特征：任何**具体**的私有目录名/工具链名写进这里，等于把要拦的东西
公开一遍。真正的私有词表在本地发布脚本里（那份不公开），两层分工：
**公开这层挡"谁都不该有的写法"，私有那层挡"我们自己的东西"。**

用法
----
    python scripts/check_publish.py            # 检查仓库根（脚本的上两级）
    python scripts/check_publish.py --root .   # 指定站点根目录
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# ── 允许被跟踪的文件（新增工具时在这里加一行；不在此清单内的文件一律失败）──
ALLOWED_FILES = {
    "index.html", "manifest.webmanifest", ".nojekyll",
    "icon-192.png", "icon-512.png",
    "README.md", ".gitattributes", ".gitignore",
    "wave/index.html", "wave/wave-mappings.js",
    "unbalance/index.html",
}
# 允许的目录前缀：**只收平铺文件**（子目录不许有）
ALLOWED_PREFIXES = {
    "scripts/": (".py",),
    ".github/workflows/": (".yml", ".yaml"),
}
# 允许**嵌套子目录**的目录树（关卡数据按 `data/levels/<分区>/<关>.json` 分层）
ALLOWED_TREES = {
    "data/": (".json",),
}

# ── 通用痕迹：不点名任何具体目录，只说"这种写法不该出现"──
GENERIC = [
    # 盘符路径：要求反斜杠/斜杠后面**还有路径片段**，否则 `file://` 里的 `e:/` 会假报警
    (r"[A-Za-z]:[\\/](?![\\/])", "绝对盘符路径"),
    (r"file:///", "file:// 绝对 URL"),
    (r"127\.0\.0\.1|localhost:\d", "本机地址"),
    (r"docs/[a-z][a-z0-9_-]*/", "指向别处 docs 目录的引用"),
]
TEXT_EXT = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".webmanifest", ".txt", ".md", ".py", ".yml", ".yaml"}

# 体积上限：站点（不含 data/）与数据分开算 —— 否则阶段 2 的 28 MB 关卡数据会直接顶爆
MAX_FILE_BYTES = 400 * 1024          # 单个站点文件
MAX_SITE_BYTES = 1024 * 1024         # 站点（不含 data/）
MAX_DATA_BYTES = 64 * 1024 * 1024    # data/

def discover_pages(root: str) -> list:
    """页面清单 = 根 index.html + 所有 `*/index.html`（自动发现，加新工具不必登记）。"""
    out = []
    if os.path.isfile(os.path.join(root, "index.html")):
        out.append("index.html")
    for d in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, d, "index.html")):
            out.append(d + "/index.html")
    return out

# 本文件的路径：它承载着下面的模式表，没法用这些模式自检，跳过它自己
SELF = "scripts/check_publish.py"


def walk_files(root: str):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".staging")]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"))
    return sorted(out)


def repo_files(root: str):
    """检查对象 = **将被提交的文件**（已跟踪的 + 未跟踪但未被 ignore 的）。

    两点都要照顾：
    * 不能裸遍历 —— 本地有两份被 ignore 的私有文件（`README1.md` / `AGENTS.md`），
      裸遍历会把它们算进来，既误报"清单外文件"、又误报其中的绝对路径；
    * 也不能只看已跟踪 —— 本地发布脚本是在 `git add` **之前**跑这个检查的，
      新生成的 `data/` 那时还没进索引，只看已跟踪就会漏掉整个数据目录。
    git 不可用时退回遍历（CI 与本机都有 git，正常走不到）。
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z",
                              "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            files = sorted(l for l in out.stdout.split("\0") if l.strip())
            if files:
                return files, "git ls-files --cached --others"
    except Exception:
        pass
    return walk_files(root), "目录遍历（git 不可用）"


def allowed(rel: str) -> bool:
    if rel in ALLOWED_FILES:
        return True
    ext = os.path.splitext(rel)[1].lower()
    for pre, exts in ALLOWED_PREFIXES.items():
        rest = rel[len(pre):] if rel.startswith(pre) else None
        if rest and "/" not in rest and ext in exts:
            return True
    for pre, exts in ALLOWED_TREES.items():
        rest = rel[len(pre):] if rel.startswith(pre) else None
        if rest and ext in exts:
            return True
    return False


def check_manifest(files: list, src: str) -> int:
    extra = sorted(f for f in files if not allowed(f))
    if extra:
        print("  ✗ 有 %d 个文件不在允许清单内：" % len(extra))
        for f in extra:
            print("      %s" % f)
        print("      ⇒ 确认它该被发布；该发就加进 check_publish.py 的 ALLOWED_FILES/ALLOWED_PREFIXES，"
              "不该发就加进 .gitignore")
        return len(extra)
    print("  ✓ %d 个跟踪文件全部在允许清单内（来源：%s）" % (len(files), src))
    return 0


def check_generic(root: str, files: list) -> int:
    bad = 0
    for p in files:
        if p == SELF:
            continue               # 本文件的模式表里就写着这些 pattern，没法自检
        if p.startswith("data/"):
            continue               # 数据是生成的载荷，不是"作者写的文本"；由第 4 项校验
        if os.path.splitext(p)[1].lower() not in TEXT_EXT:
            continue
        full = os.path.join(root, p.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        try:
            text = open(full, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for pat, label in GENERIC:
            hits = re.findall(pat, text)
            if not hits:
                continue
            bad += 1
            lines = [i + 1 for i, l in enumerate(text.split("\n")) if re.search(pat, l)][:3]
            print("  ✗ %-24s %-22s %d 次  行 %s  %s"
                  % (p, label, len(hits), lines, sorted(set(hits))[:2]))
    if bad == 0:
        print("  ✓ 未发现绝对路径 / file:/// / localhost / 外部 docs 引用")
    return bad


def check_js_payload(root: str) -> int:
    p = os.path.join(root, "wave", "wave-mappings.js")
    if not os.path.isfile(p):
        print("  ✗ 缺 wave/wave-mappings.js")
        return 1
    t = open(p, encoding="utf-8").read()
    i = t.find("window.WAVE_MAPPINGS")
    if i < 0:
        print("  ✗ wave-mappings.js 里找不到 window.WAVE_MAPPINGS")
        return 1
    body = t[i + len("window.WAVE_MAPPINGS"):].strip()
    if not body.startswith("="):
        print("  ✗ wave-mappings.js 载荷不是赋值形式")
        return 1
    try:
        d = json.loads(body[1:].strip().rstrip(";"))
    except Exception as e:
        print("  ✗ wave-mappings.js 载荷不是合法 JSON：%s" % e)
        return 1
    need = {"unitNames", "delayBorn", "levelNames", "trapClass"}
    miss = need - set(d)
    if miss:
        print("  ✗ wave-mappings.js 缺表：%s" % sorted(miss))
        return 1
    empty = [k for k in need if not d[k]]
    if empty:
        print("  ✗ wave-mappings.js 有空表：%s（多半是构建截断）" % sorted(empty))
        return 1
    print("  ✓ wave-mappings.js 合法：%s"
          % "  ".join("%s=%d" % (k, len(d[k])) for k in sorted(need)))
    return 0


def check_assets(root: str) -> int:
    bad = 0
    pages = discover_pages(root)
    for rel in pages:
        p = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            print("  ✗ 缺页面 %s" % rel)
            bad += 1
            continue
        text = open(p, encoding="utf-8").read()
        base = os.path.dirname(p)
        for m in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', text):
            u = m.group(1)
            if u.startswith(("http:", "https:", "data:", "#", "mailto:")) or u == "":
                continue
            if u.startswith("/"):
                print("  ✗ %s 用了根相对路径 %r（站点在子目录下会 404）" % (rel, u))
                bad += 1
                continue
            if not os.path.exists(os.path.join(base, u.split("?")[0])):
                print("  ✗ %s 引用了不存在的相对资源 %r" % (rel, u))
                bad += 1
    if bad == 0:
        print("  ✓ %d 个页面的相对资源齐全，且无根相对路径（自动发现）" % len(pages))
    return bad


def check_data(root: str) -> int:
    """关卡数据自洽：索引 ↔ 文件一一对应、无孤儿、载荷能解析。

    没有 `data/` 就跳过 —— 站点不带关卡数据时这一项不适用。
    """
    idx_p = os.path.join(root, "data", "levels-index.json")
    if not os.path.isfile(idx_p):
        print("  · 无 data/levels-index.json —— 跳过（本站点未托管关卡数据）")
        return 0
    try:
        idx = json.load(open(idx_p, encoding="utf-8"))
    except Exception as e:
        print("  ✗ data/levels-index.json 不是合法 JSON：%s" % e)
        return 1
    areas, levels = idx.get("areas"), idx.get("levels")
    if not isinstance(areas, list) or not isinstance(levels, dict):
        print("  ✗ data/levels-index.json 结构不对（需要 areas:[] 与 levels:{}）")
        return 1

    bad = 0
    lvroot = os.path.join(root, "data", "levels")
    listed, missing = [], []
    for a, arr in levels.items():
        for bare in arr:
            rel = a + "/" + bare
            listed.append(rel)
            if not os.path.isfile(os.path.join(lvroot, rel.replace("/", os.sep) + ".json")):
                missing.append(rel)
    if missing:
        print("  ✗ 索引里有 %d 关没有对应文件，前几关：%s" % (len(missing), missing[:5]))
        bad += len(missing)

    disk = set()
    for dp, _dn, fn in os.walk(lvroot):
        for f in fn:
            if f.endswith(".json"):
                r = os.path.relpath(os.path.join(dp, f), lvroot).replace("\\", "/")
                disk.add(r[:-5])
    orphan = sorted(disk - set(listed))
    if orphan:
        print("  ✗ 磁盘上有 %d 个索引外的孤儿文件，前几个：%s" % (len(orphan), orphan[:5]))
        bad += len(orphan)

    broken = []
    for rel in listed:
        p = os.path.join(lvroot, rel.replace("/", os.sep) + ".json")
        if not os.path.isfile(p):
            continue
        try:
            json.load(open(p, encoding="utf-8"))
        except Exception:
            broken.append(rel)
    if broken:
        print("  ✗ 有 %d 个关卡载荷解析失败（多半被截断），前几个：%s" % (len(broken), broken[:5]))
        bad += len(broken)

    if bad == 0:
        print("  ✓ 关卡数据自洽：索引 %d 关 / 分区 %d 个，文件一一对应、载荷均可解析"
              % (len(listed), len(areas)))
    return bad


def check_sizes(root: str, files: list) -> int:
    bad = 0
    site = data = 0
    for p in files:
        full = os.path.join(root, p.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        n = os.path.getsize(full)
        if p.startswith("data/"):
            data += n
            continue
        site += n
        if n > MAX_FILE_BYTES:
            print("  ✗ %s 单文件 %.1f KB > 上限 %d KB" % (p, n / 1024, MAX_FILE_BYTES // 1024))
            bad += 1
    print("  ✓ 站点（不含 data/）%.1f KB / 上限 %d KB" % (site / 1024, MAX_SITE_BYTES // 1024))
    if site > MAX_SITE_BYTES:
        print("  ✗ 站点超限")
        bad += 1
    if data:
        print("  ✓ data/ %.1f MB / 上限 %d MB" % (data / 1048576, MAX_DATA_BYTES // 1048576))
        if data > MAX_DATA_BYTES:
            print("  ✗ data/ 超限")
            bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    print("检查仓库根：%s\n" % root)

    files, src = repo_files(root)

    bad = 0
    print("[1/5] 仓库清单（只允许多出来的文件 = 0）")
    bad += check_manifest(files, src)
    print()

    print("[2/5] 通用痕迹")
    bad += check_generic(root, files)
    print()

    print("[3/5] 映射库载荷 + 相对资源")
    bad += check_js_payload(root)
    bad += check_assets(root)
    print()

    print("[4/5] 关卡数据自洽")
    bad += check_data(root)
    print()

    print("[5/5] 体积")
    bad += check_sizes(root, files)
    print()

    if bad:
        print("✗ 检查未通过（%d 项）—— 不要发布。" % bad)
        return 1
    print("✓ 全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
