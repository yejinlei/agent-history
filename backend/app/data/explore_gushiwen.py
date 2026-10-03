# -*- coding: utf-8 -*-
import re
import urllib.request

url = "https://www.gushiwen.cn/guwen/bookv_a0032c4fcf60.aspx"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
# find main text container pattern
# print a snippet
print(html[:2000])
print("\n---LINKS---\n")
for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S):
    href, label = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
    if "bookv" in href or "book.aspx" in href:
        print(href, "|", label[:30])
