# -*- coding: utf-8 -*-
"""从 hunterhug.github.io/china-history 抓取二十四史原文，生成 txt 到 books/。

策略：
- 访问首页抓取书目（排除 *-白话 目录）。
- 对每本书的索引页抓取所有 href 含 "原文" 的链接。
- 访问每章原文页，用 HTMLParser 丢弃 <a>/<script>/<style>/<footer>/<nav>，只保留正文段落。
- 合并章节并按统一格式写入 books/<book_id>.txt，同时生成/更新 books_manifest.json。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

BASE_SITE = "https://hunterhug.github.io/china-history"
DATA_DIR = Path(__file__).resolve().parent
BOOKS_DIR = DATA_DIR / "books"
MANIFEST_PATH = DATA_DIR / "books_manifest.json"

SKIP_TAGS = {"script", "style", "head", "a", "footer", "nav", "header",
              "aside", "noscript"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "section", "article"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        out = "".join(self.parts)
        out = re.sub(r"[ \t　]+", " ", out)
        out = re.sub(r"\n{2,}", "\n\n", out)
        return out.strip()


def fetch(url: str, timeout: int = 40, retries: int = 3) -> str:
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last_err or ConnectionError(f"无法获取 {url}")


def quote_path(fragment: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="/") for part in fragment.split("/"))


def fetch_page(fragment: str) -> str:
    return fetch(f"{BASE_SITE}/{quote_path(fragment)}")


def extract_links(page: str) -> list[tuple[str, str]]:
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S)
    out = []
    for href, label in pattern.findall(page):
        label = re.sub(r'<[^>]+>', '', label).strip()
        if not label:
            continue
        out.append((href, label))
    return out


_NAV_WORDS = {
    "← 阿星星的首页", "阿星星的首页", "首页", "目录", "目錄", "返回目录",
    "回目录", "上一节", "下一节", "上一章", "下一章", "上一页", "下一页",
    "上页", "下页", "上章", "下章", "↑", "<<", ">>", "（原文）", "原文",
}


def page_to_text(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        pass
    text = extractor.text()
    # 后处理：删除常见导航/版权/站点残留单行
    filtered: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in _NAV_WORDS:
            continue
        low = line.lower()
        if any(w in low for w in ("版权所有", "阿星星", "手机阅读", "无弹窗", "纯文字")):
            continue
        if "©" in line:
            continue
        # 页面自动章节索引（如 "第一章-五帝本纪-原文"）已在 <a> 中剔除，
        # 这里再过滤可能漏出的单行章节索引条目
        if re.fullmatch(r".*-.*（原文）", line):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def book_text_from_index(index_fragment: str, max_workers: int = 4) -> str:
    index_html = fetch_page(index_fragment)
    links = extract_links(index_html)
    chapter_urls: list[tuple[str, str]] = []
    for href, label in links:
        if "原文" not in href and "原文" not in label:
            continue
        # 忽略白话相关
        if "白话" in href or "白话" in label:
            continue
        # 相对路径
        if href.startswith(("http", "javascript", "mailto", "#")):
            continue
        chapter_urls.append((href, label))

    # 去重并保持顺序
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for href, label in chapter_urls:
        if href in seen:
            continue
        seen.add(href)
        unique.append((href, label))

    base_dir = "/".join(index_fragment.split("/")[:-1])  # e.g. 史记/史记
    print(f"  {index_fragment}: {len(unique)} original chapters")

    def fetch_one(item: tuple[int, tuple[str, str]]) -> tuple[int, str, str]:
        i, (href, label) = item
        ch_fragment = f"{base_dir}/{href.lstrip('./')}"
        try:
            ch_html = fetch_page(ch_fragment)
            ch_text = page_to_text(ch_html)
        except Exception as e:
            print(f"    skip {ch_fragment}: {e}")
            return i, label, ""
        return i, label, ch_text

    results: list[tuple[int, str, str]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(fetch_one, enumerate(unique)))
    results.sort(key=lambda x: x[0])

    parts: list[str] = []
    for _, label, ch_text in results:
        if not ch_text:
            continue
        parts.append(f"\n\n===== {label} =====\n\n")
        parts.append(ch_text)
    return "".join(parts).strip()


# 书目元数据（ span=大致时间范围，level=1 原始史料，2 现代研究）
BOOK_META: dict[str, dict] = {
    "shiji": {"title": "史记", "author": "司马迁", "year": -91, "dynasty_id": "xihan",
              "span": (-3000, -91), "tags": ["二十四史", "纪传体", "原始史料"]},
    "hanshu": {"title": "汉书", "author": "班固", "year": 82, "dynasty_id": "donghan",
               "span": (-206, 24), "tags": ["二十四史", "纪传体", "原始史料"]},
    "houhanshu": {"title": "后汉书", "author": "范晔", "year": 445, "dynasty_id": "donghan",
                  "span": (25, 220), "tags": ["二十四史", "纪传体", "原始史料"]},
    "sanguozhi": {"title": "三国志", "author": "陈寿", "year": 289, "dynasty_id": "jin",
                  "span": (220, 280), "tags": ["二十四史", "纪传体", "原始史料"]},
    "jinshu": {"title": "晋书", "author": "房玄龄等", "year": 648, "dynasty_id": "tang",
               "span": (265, 420), "tags": ["二十四史", "纪传体", "原始史料"]},
    "songshu": {"title": "宋书", "author": "沈约", "year": 488, "dynasty_id": "nanbei",
                "span": (420, 479), "tags": ["二十四史", "纪传体", "原始史料"]},
    "nanqishu": {"title": "南齐书", "author": "萧子显", "year": 537, "dynasty_id": "nanbei",
                 "span": (479, 502), "tags": ["二十四史", "纪传体", "原始史料"]},
    "liangshu": {"title": "梁书", "author": "姚思廉", "year": 636, "dynasty_id": "nanbei",
                 "span": (502, 557), "tags": ["二十四史", "纪传体", "原始史料"]},
    "chenshu": {"title": "陈书", "author": "姚思廉", "year": 636, "dynasty_id": "nanbei",
                "span": (557, 589), "tags": ["二十四史", "纪传体", "原始史料"]},
    "weishu": {"title": "魏书", "author": "魏收", "year": 554, "dynasty_id": "nanbei",
               "span": (386, 550), "tags": ["二十四史", "纪传体", "原始史料"]},
    "beiqiishu": {"title": "北齐书", "author": "李百药", "year": 636, "dynasty_id": "nanbei",
                  "span": (550, 577), "tags": ["二十四史", "纪传体", "原始史料"]},
    "zhoushu": {"title": "周书", "author": "令狐德棻等", "year": 636, "dynasty_id": "nanbei",
                "span": (557, 581), "tags": ["二十四史", "纪传体", "原始史料"]},
    "suishu": {"title": "隋书", "author": "魏徵等", "year": 656, "dynasty_id": "sui",
               "span": (581, 618), "tags": ["二十四史", "纪传体", "原始史料"]},
    "nanshi": {"title": "南史", "author": "李延寿", "year": 659, "dynasty_id": "nanbei",
               "span": (420, 589), "tags": ["二十四史", "纪传体", "原始史料"]},
    "beishi": {"title": "北史", "author": "李延寿", "year": 659, "dynasty_id": "nanbei",
               "span": (386, 618), "tags": ["二十四史", "纪传体", "原始史料"]},
    "jiutangshu": {"title": "旧唐书", "author": "刘昫等", "year": 945, "dynasty_id": "tang",
                   "span": (618, 907), "tags": ["二十四史", "纪传体", "原始史料"]},
    "jiusidai": {"title": "旧五代史", "author": "薛居正等", "year": 974, "dynasty_id": "wudai",
                 "span": (907, 960), "tags": ["二十四史", "纪传体", "原始史料"]},
    "xintangshu": {"title": "新唐书", "author": "欧阳修、宋祁", "year": 1060, "dynasty_id": "tang",
                   "span": (618, 907), "tags": ["二十四史", "纪传体", "原始史料"]},
    "xinwudaishi": {"title": "新五代史", "author": "欧阳修", "year": 1072, "dynasty_id": "wudai",
                    "span": (907, 960), "tags": ["二十四史", "纪传体", "原始史料"]},
    "songshi": {"title": "宋史", "author": "脱脱等", "year": 1345, "dynasty_id": "beisong",
                "span": (960, 1279), "tags": ["二十四史", "纪传体", "原始史料"]},
    "liaoshi": {"title": "辽史", "author": "脱脱等", "year": 1344, "dynasty_id": "liao",
                "span": (907, 1125), "tags": ["二十四史", "纪传体", "原始史料"]},
    "jinshi": {"title": "金史", "author": "脱脱等", "year": 1345, "dynasty_id": "jindyn",
               "span": (1115, 1234), "tags": ["二十四史", "纪传体", "原始史料"]},
    "yuanshi": {"title": "元史", "author": "宋濂等", "year": 1370, "dynasty_id": "yuan",
                "span": (1271, 1368), "tags": ["二十四史", "纪传体", "原始史料"]},
    "mingshi": {"title": "明史", "author": "张廷玉等", "year": 1739, "dynasty_id": "ming",
                "span": (1368, 1644), "tags": ["二十四史", "纪传体", "原始史料"]},
}

COLORS = ["#b91c1c", "#1d4ed8", "#047857", "#b45309", "#7e22ce", "#be185d",
          "#0f766e", "#4338ca", "#a16207", "#991b1b", "#047857", "#c2410c"]


def build_manifest() -> dict:
    manifest: dict[str, dict] = {}
    for i, (bid, meta) in enumerate(BOOK_META.items()):
        file_name = f"{bid}.txt"
        manifest[bid] = {
            "file": file_name,
            "title": meta["title"],
            "author": meta["author"],
            "year": meta["year"],
            "level": 1,
            "color": COLORS[i % len(COLORS)],
            "dynasty_id": meta["dynasty_id"],
            "span": meta["span"],
            "tags": meta["tags"],
            "description": f"《{meta['title']}》，{meta['author']}撰，{meta['year']}年成书。二十四史之一纪传体正史。",
        }
    return manifest


def main(books: Iterable[str] | None = None) -> None:
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    if MANIFEST_PATH.exists():
        try:
            existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest.update(existing)
        except Exception:
            pass

    # 抓取首页书目
    print("Fetching book list ...")
    home = fetch_page("index.html")
    home_links = extract_links(home)
    book_dirs = []
    for href, label in home_links:
        if not href.endswith(".html") or href.startswith(("http", "javascript")):
            continue
        if "-白话" in href:
            continue
        # href 形如 史记/史记.html
        book_dirs.append(href)
    print("books on site:", len(book_dirs))

    wanted = set(books) if books else set(BOOK_META.keys())

    for href in book_dirs:
        dir_name = href.split("/")[0]
        # 通过目录名匹配到 book_id
        bids = [bid for bid, meta in BOOK_META.items() if meta["title"] == dir_name]
        if not bids:
            print(f"  skip unmatched dir: {dir_name}")
            continue
        bid = bids[0]
        if bid not in wanted:
            print(f"  skip {bid} (not in requested set)")
            continue
        target = BOOKS_DIR / manifest[bid]["file"]
        if target.exists() and target.stat().st_size > 1024:
            print(f"  {bid} already downloaded ({target.stat().st_size} bytes)")
            continue
        print(f"Downloading {bid} ...")
        try:
            text = book_text_from_index(href)
        except Exception as e:
            print(f"  FAILED {bid}: {e}")
            continue
        target.write_text(text, encoding="utf-8")
        print(f"  written {target} ({len(text)} chars)")

    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Manifest saved to", MANIFEST_PATH)


if __name__ == "__main__":
    import sys
    # 允许 python download_books.py shiji,hanshu 只下载几本
    requested = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    main(requested)
