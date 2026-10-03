# -*- coding: utf-8 -*-
"""用 Playwright 无头浏览器续传 gushiwen 被 JS 挑战(HTTP 512)拦截的章节页。

背景：gushiwen 的章节页(bookv_*.aspx)对 urllib 返回 HTTP 512（JS 挑战式反爬），
TOC 页正常。本脚本用 Chromium 渲染章节页、通过挑战(verificationCode cookie)后提取
正文 div.contson 的渲染文本，续写到 books/<file>。复用 download_gushiwen.py 的
断点续传逻辑：解析已有 "### 章名" 跳过重抓章节。

仅用标准库 + playwright；Chromium 可执行文件需已安装（见 README/requirements）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://www.gushiwen.cn"
DATA_DIR = Path(__file__).resolve().parent
BOOKS_DIR = DATA_DIR / "books"
MANIFEST_PATH = DATA_DIR / "books_manifest.json"

EXE = r"C:\Users\40108\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
    "delete navigator.__proto__.webdriver;"
)
DELAY = 1.5
RETRIES = 3

# 仅含需要 playwright 补抓的书目（其余书用原 urllib 脚本即可）
BOOKS = [
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
]


def list_chapters(gid: str):
    req = urllib.request.Request(f"{BASE}/guwen/book_{gid}.aspx", headers=UA)
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
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


def fetch_chapter(pg, href: str) -> str | None:
    """用已加载 stealth 的 page 抓取章节正文；挑战未过则重载重试。"""
    for _ in range(RETRIES):
        pg.goto(BASE + href, wait_until="domcontentloaded", timeout=60000)
        try:
            pg.wait_for_selector("div.contson", timeout=20000)
            txt = pg.inner_text("div.contson").strip()
            if txt:
                return txt
        except Exception:
            pass
        # 挑战可能未自动放行：等其跑完并带 cookie 重载
        time.sleep(3)
        pg.reload(wait_until="domcontentloaded", timeout=60000)
        try:
            pg.wait_for_selector("div.contson", timeout=20000)
            txt = pg.inner_text("div.contson").strip()
            if txt:
                return txt
        except Exception:
            pass
        time.sleep(2)
    return None


def download_book(meta: dict):
    out_path = BOOKS_DIR / meta["file"]
    done_labels = set()
    if out_path.exists() and out_path.stat().st_size > 0:
        existing = out_path.read_text(encoding="utf-8")
        done_labels = set(re.findall(r"\n### (.+?)\n", existing))
        if done_labels:
            print(f"[resume] {meta['title']} 已有 {len(done_labels)} 章，续传缺失章节")
    print(f"[start] {meta['title']} (gid={meta['gid']})")
    chapters = list_chapters(meta["gid"])
    print(f"  目录章节数: {len(chapters)}")

    parts = []
    ok = len(done_labels)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=EXE,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=UA["User-Agent"])
        pg = ctx.new_page()
        pg.add_init_script(STEALTH)
        for i, (href, label) in enumerate(chapters, 1):
            if label in done_labels:
                continue
            txt = fetch_chapter(pg, href)
            if not txt:
                print(f"  ! 章节失败 {label} ({href})")
                continue
            parts.append(f"\n\n### {label}\n\n{txt}")
            ok += 1
            print(f"  + [{ok}/{len(chapters)}] {label} ({len(txt)}字)")
            time.sleep(DELAY)
        browser.close()

    if not parts:
        print(f"[done] {meta['title']} 无需补充，共 {ok} 章")
        return
    with out_path.open("a", encoding="utf-8") as f:
        f.write("".join(parts))
    print(f"[done] {meta['title']} 累计 {ok} 章 -> {out_path.name} ({out_path.stat().st_size} bytes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()
    targets = [b for b in BOOKS if not args.only or b["title"] in args.only]
    for m in targets:
        download_book(m)
    print("完成。")


if __name__ == "__main__":
    sys.exit(main())
