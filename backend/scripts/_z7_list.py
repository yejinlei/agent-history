# -*- coding: utf-8 -*-
"""生成 z7 精选清单：maps.json 总图（时期/全图/在线历史地图/形势）+ seed.py 全部朝代主图。"""
import json
import re
from pathlib import Path

base = Path(__file__).resolve().parent.parent
groups = json.loads((base / "app/data/maps.json").read_text(encoding="utf-8"))
keys = ("时期", "全图", "在线历史地图", "形势")
tiles = set()
for ms in groups.values():
    for m in ms:
        if any(k in m["title"] for k in keys):
            t = m["tile"]
            tiles.add(t if t.startswith("m") else "m" + t)
seed = (base / "app/data/seed.py").read_text(encoding="utf-8")
tiles |= set(re.findall(r'map_tile="(mp\w+)"', seed))
out = sorted(tiles)
print(len(out))
print(" ".join(out))
