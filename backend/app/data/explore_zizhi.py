# -*- coding: utf-8 -*-
import re
import urllib.request

url = "https://hunterhug.github.io/china-history/index.html"
html = urllib.request.urlopen(url).read().decode("utf-8", errors="replace")
pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S)
for href, label in pattern.findall(html):
    label = re.sub(r'<[^>]+>', '', label).strip()
    if not href.startswith(("http", "javascript")) and href.endswith(".html") and "白话" not in href:
        print(f"{href} | {label}")
