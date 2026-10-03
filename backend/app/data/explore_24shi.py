# -*- coding: utf-8 -*-
import urllib.request
url = "http://www.24-shi.com/book_Download/bookTxt.aspx?bookName=%E8%B5%84%E6%B2%BB%E9%80%9A%E9%89%B4"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
try:
    data = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    print(data[:1000])
except Exception as e:
    print(type(e).__name__, e)
