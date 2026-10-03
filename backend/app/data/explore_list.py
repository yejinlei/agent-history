# -*- coding: utf-8 -*-
import re, urllib.request
url = "https://hunterhug.github.io/china-history/index.html"
html = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S)
for href, label in links:
    label = re.sub(r'<[^>]+>', '', label).strip()
    if not href.endswith(".html") or "白话" in href:
        continue
    print(f"{label}\t{href}")
