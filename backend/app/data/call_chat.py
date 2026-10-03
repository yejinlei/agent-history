# -*- coding: utf-8 -*-
import json, urllib.request
url = "http://127.0.0.1:8000/api/chat"
payload = json.dumps({"question": "刘邦在哪里起兵？", "audience": "scholar"}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=120) as r:
    data = json.loads(r.read().decode('utf-8'))
print("answer:", data.get("answer"))
print("entity:", data.get("entity"))
print("evidence count:", len(data.get("evidence", [])))
for e in data.get("evidence", [])[:5]:
    print(" -", e["work"], e["text"][:80])
