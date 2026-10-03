# -*- coding: utf-8 -*-
import json, urllib.request
url = "http://127.0.0.1:8000/api/ontology"
with urllib.request.urlopen(url, timeout=30) as r:
    data = json.loads(r.read().decode('utf-8'))
print(json.dumps(data["stats"], ensure_ascii=False, indent=2))
print("relations:", data["relations"][:10])
