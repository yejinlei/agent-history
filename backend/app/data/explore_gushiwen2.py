# -*- coding: utf-8 -*-
import re
import urllib.request

url = "https://www.gushiwen.cn/guwen/bookv_a0032c4fcf60.aspx"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")

# Find paragraph-like divs
for m in re.finditer(r'<div[^>]*class=["\']contson["\'][^>]*>(.*?)</div>', html, re.S):
    text = re.sub(r'<[^>]+>', '', m.group(1))
    text = text.replace('\n','').replace('\r','')
    print(text[:1200])
    print("\n---END---\n")
