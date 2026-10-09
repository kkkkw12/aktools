#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点完整性检查（CI 与本地都能跑）。


1. 映射库载荷合法性 —— `wave/wave-mappings.js` 必须是能解析的 `window.WAVE_MAPPINGS = {...};`，四张表齐全；
2. 相对资源完整性 —— 各页面里引用的相对资源必须存在，且不得用根相对路径（站点在子仓库子目录下）；
3. 体积 —— 单文件与整站上限。


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
import sys

# 站点页面（相对资源检查的对象）
PAGES = ["index.html", "wave/index.html", "unbalance/index.html"]

# 体积门禁
MAX_FILE_BYTES = 400 * 1024        # 单文件
MAX_SITE_BYTES = 1024 * 1024       # 整站


def check_js_payload(root: str) -> int:
    """wave-mappings.js 必须是一个合法的 `window.WAVE_MAPPINGS = {...};`"""
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
    body = body[1:].strip().rstrip(";")
    try:
        d = json.loads(body)
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
    """页面里引用的**相对**资源必须存在，且不得用根相对路径。"""
    bad = 0
    for rel in PAGES:
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
            target = os.path.join(base, u.split("?")[0])
            if not os.path.exists(target):
                print("  ✗ %s 引用了不存在的相对资源 %r" % (rel, u))
                bad += 1
    if bad == 0:
        print("  ✓ 三个页面的相对资源齐全，且无根相对路径")
    return bad


def check_sizes(root: str) -> int:
    bad = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".staging")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            n = os.path.getsize(p)
            total += n
            if n > MAX_FILE_BYTES:
                print("  ✗ %s 单文件 %.1f KB > 上限 %d KB"
                      % (os.path.relpath(p, root), n / 1024, MAX_FILE_BYTES // 1024))
                bad += 1
    print("  ✓ 整站 %.1f KB / 上限 %d KB" % (total / 1024, MAX_SITE_BYTES // 1024))
    if total > MAX_SITE_BYTES:
        print("  ✗ 整站超限")
        bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    print("检查站点根：%s\n" % root)

    bad = 0
    print("[1/3] 映射库载荷合法性")
    bad += check_js_payload(root)
    print()

    print("[2/3] 相对资源完整性")
    bad += check_assets(root)
    print()

    print("[3/3] 体积")
    bad += check_sizes(root)
    print()

    if bad:
        print("✗ 检查未通过（%d 项）—— 不要发布。" % bad)
        return 1
    print("✓ 全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
