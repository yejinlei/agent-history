# -*- coding: utf-8 -*-
import json, urllib.request
url = "http://127.0.0.1:8000/api/books"
with urllib.request.urlopen(url, timeout=30) as r:
    data = json.loads(r.read().decode('utf-8'))
print("books count:", len(data["books"]))
for b in data["books"]:
    print(b["id"], b["title"], "mounted=" + str(b["mounted"]), "extracts=" + str(len(b["extracts"])))
