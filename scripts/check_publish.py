#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点检查（CI 与本地都能跑）。六项，都不含项目专属判断：

1. **仓库清单** —— 被 git 跟踪的文件必须落在允许清单内。多出来的文件一律失败；
   工具目录自动发现，同级 `.html`/`.js` 放行，加新工具不必在这里登记；
2. **通用痕迹** —— 绝对盘符路径 / `file:///` / localhost / `docs/<目录>/` 这类
   放哪儿都不该出现的写法；
3. **载荷与资源** —— 工具目录下的映射/数据载荷（`.js`）必须能解析成非空对象；
   说明载荷（`*-notes.js`）必须结构完整、id 不重复、逐条有标题与正文；
   页面 `?` 挂点（`data-note`）与说明载荷 id 必须一一对应；
   页面引用的相对资源必须存在且不得用根相对路径；
4. **关卡数据自洽** —— `data/levels-index.json` 列出的每一关都必须真有文件、磁盘上不得有索引外的
   孤儿文件、每个载荷都能解析。没有 `data/` 就跳过；
5. **体积** —— 站点 ≤ 1 MB、`data/` ≤ 64 MB、单文件 ≤ 400 KB；
6. **首页可达性** —— 每个工具目录必须被首页链到，首页也不得链到不存在的目录。

页面清单与工具目录都自动发现：根 `index.html` + 所有 `*/index.html`。

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

# ── 允许被跟踪的文件（根层的固定项；不在此清单内、又不落在下面的目录规则里的，一律失败）──
ALLOWED_FILES = {
    "index.html", "manifest.webmanifest", ".nojekyll",
    "icon-192.png", "icon-512.png",
    "README.md", ".gitattributes", ".gitignore",
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
# 工具目录：**自动发现**，不登记名字 —— 顶层目录里只要有 `index.html` 就是一个工具页，
# 该目录允许平铺 `.html` / `.js`（页面 + 它的数据/说明载荷）。
# 为什么平铺：`file://` 双击打开是这套工具的日常用法，fetch 在这种场景下不可用，
# 载荷只能是同级 `<script src>`，所以同级 js 是本设计的一部分，不是意外产物。
TOOL_DIR_EXTS = (".html", ".js")

# ── 通用痕迹：不点名任何具体目录，只说"这种写法不该出现"──
GENERIC = [
    # 盘符路径：要求反斜杠/斜杠后面**还有路径片段**，否则 `file://` 里的 `e:/` 会假报警
    (r"[A-Za-z]:[\\/](?![\\/])", "绝对盘符路径"),
    (r"file:///", "file:// 绝对 URL"),
    (r"127\.0\.0\.1|localhost:\d", "本机地址"),
    (r"docs/[a-z][a-z0-9_-]*/", "指向别处 docs 目录的引用"),
]
TEXT_EXT = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".webmanifest", ".txt", ".md", ".py", ".yml", ".yaml"}

# 体积上限：站点（不含 data/）与数据分开算 —— 否则几十 MB 的关卡数据会直接顶爆
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


def _git_env() -> dict:
    """给 git 子进程的环境：摘掉 `GIT_CONFIG_*`。

    有些自动化宿主会设 `GIT_CONFIG_COUNT` 却不给配套的 `GIT_CONFIG_KEY_0`，
    git 于是直接 `fatal: unable to parse command-line config` —— 所有读命令都失败，
    本脚本就退回目录遍历，把被 .gitignore 排除的本地文件算进清单，报出假「清单外文件」。
    这里对 git 只有读需求，摘掉这几个变量不影响任何判断。
    """
    e = dict(os.environ)
    for k in [k for k in e if k.upper().startswith("GIT_CONFIG_")]:
        e.pop(k)
    return e


def repo_files(root: str):
    """检查对象 = **将被提交的文件**（已跟踪的 + 未跟踪但未被 ignore 的）。

    两点都要照顾：
    * 不能裸遍历 —— 本地有被 .gitignore 排除的文件，裸遍历会把它们算进来，
      既误报「清单外文件」、又误报其中的绝对路径；
    * 也不能只看已跟踪 —— 新生成的 `data/` 那时还没进索引，
      只看已跟踪就会漏掉整个数据目录。
    git 不可用时退回遍历（CI 与本机都有 git，正常走不到）。
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z",
                              "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, timeout=60, env=_git_env())
        if out.returncode == 0:
            files = sorted(l for l in out.stdout.split("\0") if l.strip())
            if files:
                return files, "git ls-files --cached --others"
        else:
            # 退回遍历会把被 .gitignore 排除的本地私有文件算进来 ⇒ 假报警，
            # 所以这里必须把「降级」喊出来，而不是静悄悄换个口径。
            print("  ⚠ git ls-files 失败（%s）—— 退回目录遍历，"
                  "被 .gitignore 排除的文件会被当成清单外文件" % (out.stderr.strip()[:160] or "无输出"))
    except Exception as e:
        print("  ⚠ 调不到 git（%s）—— 退回目录遍历，同上" % e)
    return walk_files(root), "目录遍历（git 不可用）"


def allowed(rel: str, tool_dirs: frozenset = frozenset()) -> bool:
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
    head, sep, rest = rel.partition("/")
    if sep and head in tool_dirs and "/" not in rest and ext in TOOL_DIR_EXTS:
        return True
    return False


def discover_tool_dirs(files: list) -> frozenset:
    """工具目录 = 清单里出现过 `<目录>/index.html` 的那些顶层目录（与 discover_pages 同口径）。"""
    return frozenset(p.split("/")[0] for p in files
                     if p.count("/") == 1 and p.endswith("/index.html"))


def check_manifest(files: list, src: str) -> int:
    tool_dirs = discover_tool_dirs(files)
    extra = sorted(f for f in files if not allowed(f, tool_dirs))
    if extra:
        print("  ✗ 有 %d 个文件不在允许清单内：" % len(extra))
        for f in extra:
            print("      %s" % f)
        print("      ⇒ 确认它该被发布；该发就加进 check_publish.py 的 ALLOWED_FILES/ALLOWED_PREFIXES，"
              "不该发就加进 .gitignore")
        print("      （顶层目录里带 index.html 的算一个工具，同级 .html/.js 自动放行；"
              "本次识别到的工具目录：%s）"
              % (", ".join(sorted(tool_dirs)) or "无"))
        return len(extra)
    print("  ✓ %d 个跟踪文件全部在允许清单内（来源：%s；工具目录：%s）"
          % (len(files), src, ", ".join(sorted(tool_dirs)) or "无"))
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


def check_js_payload(root: str, files: list) -> int:
    """映射/数据载荷（工具目录下非 `*-notes.js` 的 `.js`）结构自检。

    只认 `window.X = {…};` 形式的 JSON 对象载荷：必须能解析、非空、且没有空表。
    其它形态的 `.js`（包括说明载荷）交给各自的检查，这里跳过。
    """
    tool_dirs = discover_tool_dirs(files)
    payloads = sorted(p for p in files
                      if p.count("/") == 1
                      and p.split("/")[0] in tool_dirs
                      and p.endswith(".js")
                      and not p.endswith("-notes.js"))
    if not payloads:
        print("  · 无映射/数据载荷 —— 跳过")
        return 0
    bad = 0
    checked = 0
    for rel in payloads:
        t = open(os.path.join(root, rel.replace("/", os.sep)), encoding="utf-8").read()
        m = re.search(r"window\.([A-Za-z_]\w*)\s*=\s*", t)
        if not m:
            continue                      # 不是 window.X = 赋值形式，跳过
        body = t[m.end():].strip()
        if not body.startswith("{"):
            continue                      # 不是对象字面量，跳过
        checked += 1
        try:
            d, _ = json.JSONDecoder().raw_decode(body)
        except ValueError as e:
            print("  ✗ %s 载荷不是合法 JSON：%s" % (rel, e))
            bad += 1
            continue
        if not isinstance(d, dict) or not d:
            print("  ✗ %s 载荷不是非空对象" % rel)
            bad += 1
            continue
        empty = [k for k, v in d.items()
                 if isinstance(v, (dict, list, str)) and not v]
        if empty:
            print("  ✗ %s 有空表：%s（多半是构建截断）" % (rel, sorted(empty)))
            bad += 1
            continue
        sizes = ["%s=%d" % (k, len(v)) for k, v in sorted(d.items())
                 if isinstance(v, (dict, list))]
        print("  ✓ %s 合法：%s" % (rel, "  ".join(sizes) or "%d 键" % len(d)))
    if checked == 0:
        print("  · 无 window.X = {…} 形式的载荷 —— 跳过")
    return bad


def check_notes_payload(root: str, files: list) -> int:
    """说明载荷（工具目录下 `*-notes.js`）自洽：赋值形式 + 能解析 + 每条 title/body 非空 + id 不重复。

    页面靠 id 取文案；少一个花括号，说明就整块空白，检查该拦住。
    页面挂点与载荷 id 的对应关系由 check_note_refs 校验。没有 `*-notes.js` 就跳过。

    为什么不用 json.loads：body 是多段单引号字符串 `+` 拼接的（为了排版可读），
    那不是 JSON。所以这里做结构检查：字符串/注释-aware 的花括号配平 +
    每条必须齐 id / title / body 三件。
    """
    payloads = sorted(p for p in files
                      if p.count("/") == 1 and p.endswith("-notes.js"))
    if not payloads:
        print("  · 无 *-notes.js 说明载荷 —— 跳过")
        return 0
    bad = 0
    for rel in payloads:
        t = open(os.path.join(root, rel.replace("/", os.sep)), encoding="utf-8").read()
        m = re.search(r"window\.([A-Za-z_]\w*)\s*=\s*\{", t)
        if not m:
            print("  ✗ %s 不是 window.X = {…} 赋值形式" % rel)
            bad += 1
            continue
        err = _balance_error(t, m.end() - 1)
        if err:
            print("  ✗ %s 结构不完整：%s" % (rel, err))
            bad += 1
            continue
        ids = re.findall(r'\{\s*id:\s*"([^"]+)"', t)
        if not ids:
            print("  ✗ %s 解析不出任何 note 条目" % rel)
            bad += 1
            continue
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            print("  ✗ %s 有重复 id：%s" % (rel, dup))
            bad += 1
            continue
        # 按条目绑定：逐条查 title / body 非空，避免全文件计数被正文里的字样凑数糊弄
        chunks = re.split(r'\{\s*id:\s*"', t)[1:]
        weak = []
        if len(chunks) == len(ids):
            for cid, ch in zip(ids, chunks):
                mt = re.search(r'\btitle:\s*"([^"]*)"', ch)
                if not mt or not mt.group(1).strip():
                    weak.append("%s: 缺标题" % cid)
                    continue
                mb = re.search(r'\bbody:\s*\n?\s*([\'"])', ch)
                if not mb:
                    weak.append("%s: 缺正文" % cid)
                elif re.search(r"\bbody:\s*\n?\s*(['\"])\1", ch):
                    weak.append("%s: 正文为空串" % cid)
            if weak:
                print("  ✗ %s 有条目内容不全：%s" % (rel, "; ".join(weak[:4])))
                bad += 1
                continue
        # 切块数与 id 数不符时启发式失效，回退到全文件计数，绝不因此误报失败
        n_title = len(re.findall(r'\btitle:\s*"', t))
        n_body = len(re.findall(r'\bbody:\s*\n?\s*[\'"]', t))
        if not (len(ids) == n_title == n_body):
            print("  ✗ %s 有条目缺 title 或 body：id %d / title %d / body %d"
                  % (rel, len(ids), n_title, n_body))
            bad += 1
            continue
        print("  ✓ %s 结构完整：%d 条说明（全局变量 %s）" % (rel, len(ids), m.group(1)))
    return bad


def check_note_refs(root: str, files: list) -> int:
    """页面 `?` 挂点（`data-note="…"`）与同目录说明载荷 id 一一对应。

    只认双引号属性形态：页面 JS 里的单引号字面量（如 `'[data-note]'`、
    `getAttribute('data-note')`）不会被误计。挂点有而载荷无 → 报错；
    载荷有而挂点无（只在「全部说明」里出现）→ 仅提示，不算错。
    工具目录没有 `*-notes.js` 就跳过。
    """
    tool_dirs = discover_tool_dirs(files)
    notes = {}
    for p in files:
        if p.count("/") == 1 and p.endswith("-notes.js"):
            notes.setdefault(p.split("/")[0], []).append(p)
    bad = 0
    n = 0
    for d in sorted(tool_dirs):
        rels = notes.get(d)
        if not rels:
            continue
        page = os.path.join(root, d, "index.html")
        if not os.path.isfile(page):
            continue
        html = open(page, encoding="utf-8").read()
        refs = set(re.findall(r'data-note="([^"]+)"', html))
        ids = set()
        for rel in rels:
            t = open(os.path.join(root, rel.replace("/", os.sep)), encoding="utf-8").read()
            ids.update(re.findall(r'\{\s*id:\s*"([^"]+)"', t))
        miss = sorted(refs - ids)
        orphan = sorted(ids - refs)
        n += 1
        if miss:
            print("  ✗ %s/index.html 有 %d 个 ? 挂点在说明载荷里缺条目：%s"
                  % (d, len(miss), ", ".join(miss)))
            bad += len(miss)
        if orphan:
            print("  · %s 说明载荷里 %d 个 id 未在正文挂 ?（只在全部说明出现）：%s"
                  % (d, len(orphan), ", ".join(orphan)))
        if not miss:
            print("  ✓ %s/index.html 的 %d 个 ? 挂点都能对上说明条目" % (d, len(refs)))
    if n == 0:
        print("  · 没有需要核对的说明挂点 —— 跳过")
    return bad

def _balance_error(t: str, start: int):
    """从 start（指向顶层 `{`）起做字符串/注释-aware 的花括号配平。返回错误串或 None。

    为什么要自己数：载荷是 JS 不是 JSON（body 用单引号串 + `+` 拼接），json.loads 用不了，
    而 `node --check` 这类外部依赖不该出现在 CI 的通用检查里。
    """
    stack = []
    i, n, line = start, len(t), t[:start].count("\n") + 1
    while i < n:
        c = t[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and t[i + 1] == "/":
            j = t.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if c == "/" and i + 1 < n and t[i + 1] == "*":
            j = t.find("*/", i + 2)
            if j < 0:
                return "第 %d 行的块注释没闭合" % line
            line += t.count("\n", i, j)
            i = j + 2
            continue
        if c in "'\"`":
            q, i = c, i + 1
            closed = False
            while i < n:
                if t[i] == "\\":
                    i += 2
                    continue
                if t[i] == q:
                    closed = True
                    break
                if t[i] == "\n":
                    if q != "`":
                        return "第 %d 行的字符串没闭合（单/双引号串不许跨行）" % line
                    line += 1
                i += 1
            if not closed:
                return "第 %d 行的字符串没闭合" % line
            i += 1
            continue
        if c == "{":
            stack.append(line)
        elif c == "}":
            if not stack:
                return "第 %d 行多了一个 }" % line
            stack.pop()
            if not stack:
                return None          # 顶层对象闭合了，后面只剩 `;`
        i += 1
    return "花括号没配平（还差 %d 个，最早未闭合在第 %d 行）" % (len(stack), stack[0])


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


def check_index_links(root: str, files: list) -> int:
    """首页必须给每个工具目录留入口 —— 反向也查：首页不该链到不存在的目录。

    站点只有一个发现路径：首页卡片 → 工具页。新加了工具目录忘了在首页挂卡，
    等于发布了一个没人能点到的页面（Pages 上没有目录索引，爬虫也不会来）。
    """
    idx = os.path.join(root, "index.html")
    if not os.path.isfile(idx):
        print("  ✗ 缺首页 index.html")
        return 1
    text = open(idx, encoding="utf-8").read()
    # 两边都归成**裸目录名**再比：首页写的是 href="wave/"，清单里是 "wave/index.html"
    linked = {m.group(1) for m in re.finditer(r'href="([A-Za-z0-9_-]+)/"', text)}
    dirs = discover_tool_dirs(files)
    bad = 0
    for miss in sorted(dirs - linked):
        print("  ✗ %s/ 没有被首页链到（发布了孤儿页）" % miss)
        bad += 1
    for ghost in sorted(linked - dirs):
        print("  ✗ 首页链到了 %s/，但它不是工具目录（目录缺 index.html）" % ghost)
        bad += 1
    if bad == 0:
        print("  ✓ 首页入口与工具目录一一对应：%s" % (", ".join(sorted(dirs)) or "（无工具）"))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    print("检查仓库根：%s\n" % root)

    files, src = repo_files(root)

    bad = 0
    print("[1/6] 仓库清单（只允许多出来的文件 = 0）")
    bad += check_manifest(files, src)
    print()

    print("[2/6] 通用痕迹")
    bad += check_generic(root, files)
    print()

    print("[3/6] 映射/数据载荷 + 说明载荷 + 挂点对账 + 相对资源")
    bad += check_js_payload(root, files)
    bad += check_notes_payload(root, files)
    bad += check_note_refs(root, files)
    bad += check_assets(root)
    print()

    print("[4/6] 关卡数据自洽")
    bad += check_data(root)
    print()

    print("[5/6] 体积")
    bad += check_sizes(root, files)
    print()

    print("[6/6] 首页可达性（工具页必须在首页有入口）")
    bad += check_index_links(root, files)
    print()

    if bad:
        print("✗ 检查未通过（%d 项）—— 不要发布。" % bad)
        return 1
    print("✓ 全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
