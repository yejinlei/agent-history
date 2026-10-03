# -*- coding: utf-8 -*-
"""从 gushiwen.cn 抓取编年体/史评类古籍，生成 txt 到 books/ 并更新 books_manifest.json。

仅使用标准库（urllib + re），适配项目本地虚拟环境 .venv。

书籍以 gushiwen 的短链 id 标识（如 book_b30d262ad60d.aspx -> id=b30d262ad60d）。
章节页正文位于 <div class="contson">…</div>。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.gushiwen.cn"
DATA_DIR = Path(__file__).resolve().parent
BOOKS_DIR = DATA_DIR / "books"
MANIFEST_PATH = DATA_DIR / "books_manifest.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DELAY = 0.25
RETRIES = 4

# gushiwen 短链 id -> 书目元数据
BOOKS = [
    {
        "gid": "b30d262ad60d",
        "file": "zizhi_tongjian.txt",
        "title": "资治通鉴",
        "author": "司马光 等",
        "year": 1084,
        "level": 1,
        "color": "#0f766e",
        "dynasty_id": "beisong",
        "span": [-403, 959],
        "tags": ["编年体", "原始史料", "通鉴类"],
        "description": "《资治通鉴》，司马光等撰，1084年成书。编年体通史，涵盖周威烈王至后周世宗。",
    },
    {
        "gid": "b4b07e8f20d0",
        "file": "du_tongjian_lun.txt",
        "title": "读通鉴论",
        "author": "王夫之",
        "year": 1690,
        "level": 2,
        "color": "#7c3aed",
        "dynasty_id": "ming",
        "span": [-403, 959],
        "tags": ["史论", "研究著作", "通鉴类"],
        "description": "《读通鉴论》，王夫之撰，明末清初。逐卷评论《资治通鉴》史事。",
    },
    {
        "gid": "e0036a165cd6",
        "file": "shi_tong.txt",
        "title": "史通",
        "author": "刘知几",
        "year": 710,
        "level": 2,
        "color": "#be123c",
        "dynasty_id": "tang",
        "span": [-403, 710],
        "tags": ["史学理论", "研究著作"],
        "description": "《史通》，刘知几撰，710年成书。中国第一部史学批评专著。",
    },
]


def fetch(url: str) -> str:
    last = None
    backoff = 1.0
    for _ in range(RETRIES * 3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 503, 512):
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    raise RuntimeError(f"fetch failed {url}: {last}")


def clean_text(html: str):
    # 优先取正文 contson 区块
    m = re.search(r'<div[^>]*class="contson"[^>]*>(.*?)</div>', html, re.S)
    if not m:
        # 退化：取第一个较大的内容 div
        m = re.search(r'<div[^>]*class="(?:sons|conts)[\w ]*"[^>]*>(.*?)</div>', html, re.S)
    if not m:
        return None
    block = m.group(1)
    block = re.sub(r"<br\s*/?>", "\n", block, flags=re.I)
    block = re.sub(r"</(?:p|div|li|tr)>", "\n", block, flags=re.I)
    block = re.sub(r"<[^>]+>", "", block)
    block = block.replace("&nbsp;", " ").replace("&amp;", "&")
    block = re.sub(r"&[a-z]+;", "", block)
    lines = [ln.strip() for ln in block.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def list_chapters(gid: str):
    html = fetch(f"{BASE}/guwen/book_{gid}.aspx")
    seen = set()
    out = []
    for href, label in re.findall(
        r'href=["\'](/guwen/bookv_[0-9A-Fa-f]+\.aspx)["\'][^>]*>(.*?)</a>', html, re.S
    ):
        if href in seen:
            continue
        seen.add(href)
        out.append((href, re.sub(r"<[^>]+>", "", label).strip()))
    return out


def download_book(meta: dict, force: bool):
    out_path = BOOKS_DIR / meta["file"]
    # 断点续传：若文件已存在，解析已有章节标题，跳过重抓
    done_labels = set()
    if out_path.exists() and out_path.stat().st_size > 0 and not force:
        existing = out_path.read_text(encoding="utf-8")
        done_labels = set(re.findall(r"\n### (.+?)\n", existing))
        if done_labels:
            print(f"[resume] {meta['title']} 已有 {len(done_labels)} 章，续传缺失章节")
    print(f"[start] {meta['title']} (gid={meta['gid']})")
    chapters = list_chapters(meta["gid"])
    print(f"  目录章节数: {len(chapters)}")
    parts = []
    ok = 0
    for i, (href, label) in enumerate(chapters, 1):
        if label in done_labels:
            ok += 1
            continue
        try:
            html = fetch(BASE + href)
        except RuntimeError as e:
            print(f"  ! 章节失败 {label}: {e}")
            continue
        txt = clean_text(html)
        if not txt:
            print(f"  ! 空正文 {label} ({href})")
            continue
        parts.append(f"\n\n### {label}\n\n{txt}")
        ok += 1
        if i % 20 == 0:
            print(f"  ... {i}/{len(chapters)}")
        time.sleep(DELAY)
    if not parts and done_labels:
        print(f"[done] {meta['title']} 无需补充，共 {ok} 章")
        return
    if not parts:
        print(f"[fail] {meta['title']} 无有效章节")
        return
    if out_path.exists():
        with out_path.open("a", encoding="utf-8") as f:
            f.write("".join(parts))
    else:
        header = f"# {meta['title']}\n# 作者：{meta['author']}\n"
        out_path.write_text(header + "".join(parts), encoding="utf-8")
    print(f"[done] {meta['title']} 累计 {ok} 章 -> {out_path.name} ({out_path.stat().st_size} bytes)")


def update_manifest():
    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for m in BOOKS:
        manifest[m["title"]] = {
            "file": m["file"],
            "title": m["title"],
            "author": m["author"],
            "year": m["year"],
            "level": m["level"],
            "color": m["color"],
            "dynasty_id": m["dynasty_id"],
            "span": m["span"],
            "tags": m["tags"],
            "description": m["description"],
        }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[manifest] 更新 {len(manifest)} 条 -> {MANIFEST_PATH.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 txt")
    ap.add_argument("--only", nargs="*", default=None, help="仅下载指定 title")
    args = ap.parse_args()
    targets = [b for b in BOOKS if not args.only or b["title"] in args.only]
    for m in targets:
        download_book(m, args.force)
    update_manifest()
    print("完成。")


if __name__ == "__main__":
    sys.exit(main())
