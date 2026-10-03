# -*- coding: utf-8 -*-
import urllib.request, re, urllib.parse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.data.download_books import fetch_page, extract_links, BOOK_META

home = fetch_page("index.html")
links = extract_links(home)
for href, label in links:
    if not href.endswith(".html") or "白话" in href:
        continue
    dir_name = href.split("/")[0]
    bids = [bid for bid, meta in BOOK_META.items() if meta["title"] in dir_name]
    print(repr(dir_name), repr(label), "->", bids)
