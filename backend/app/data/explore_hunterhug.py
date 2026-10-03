# -*- coding: utf-8 -*-
import urllib.request, re, urllib.parse
from pathlib import Path

def fetch(url: str) -> str:
    print("fetch", url)
    r = urllib.request.urlopen(url, timeout=30)
    return r.read().decode("utf-8")

def to_url(fragment: str) -> str:
    # 只对 path 中的非 ASCII 编码
    return "https://hunterhug.github.io/china-history/" + "/".join(urllib.parse.quote(p) for p in fragment.split("/"))

def extract_links(page: str) -> list[tuple[str, str]]:
    links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S)
    out = []
    for href, label in links:
        label = re.sub(r'<[^>]+>', '', label).strip()
        out.append((href, label))
    return out

for book_dir in ("晋书", "后汉书", "三国志"):
    idx = to_url(f"{book_dir}/{book_dir}.html")
    page = fetch(idx)
    links = extract_links(page)
    print(f"--- {book_dir} total links", len(links))
    orig = [(h,l) for h,l in links if '原文' in l or '原文' in h]
    print("original links", len(orig))
    for h,l in orig[:10]:
        print(h, "->", l)
    print()
