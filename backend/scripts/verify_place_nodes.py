# -*- coding: utf-8 -*-
"""验证新建的 Place 节点：座标齐整、没有重复、别名/ external_ids 可解析。"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config

import duckdb
import kuzu

db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)

rows = []
q = c.execute(
    "MATCH (n:Place) WHERE n.id STARTS WITH 'place:' "
    "RETURN n.id, n.name, n.ancient, n.lon, n.lat, n.external_ids, n.aliases, "
    "n.provenance")
while q.has_next():
    rows.append(q.get_next())

print(f"新建 Place {len(rows):,} 个")
bad_xy = [r for r in rows if not r[3] or not r[4] or r[3] == 0]
dup_id = [k for k, v in Counter(r[0] for r in rows).items() if v > 1]
seed = c.execute(
    "MATCH (n:Place) WHERE NOT n.id STARTS WITH 'place:' RETURN count(*)").get_next()[0]
seed_names = set()
q = c.execute("MATCH (n:Place) WHERE NOT n.id STARTS WITH 'place:' RETURN n.name")
while q.has_next():
    seed_names.add(q.get_next()[0])
dup_with_seed = [r for r in rows if r[1] in seed_names]

print(f"  缺坐标 {len(bad_xy)} 个；id 重复 {len(dup_id)} 个；"
      f"与种子同名 {len(dup_with_seed)} 个")
print(f"  原有种子 Place {seed} 个")

# external_ids / aliases 是否都能解析
bad_json = 0
for r in rows:
    for raw in (r[5], r[6]):
        try:
            json.loads(raw or "{}")
        except Exception:
            bad_json += 1
print(f"  JSON 列解析失败 {bad_json} 次")

con = duckdb.connect(str(config.DATA_DIR / "warehouse" / "chronosino.duckdb"),
                     read_only=True)
evidence = con.execute("SELECT count(*) FROM place_nodes").fetchone()[0]
replica = con.execute(
    "SELECT count(*) FROM entities WHERE kind='place'").fetchone()[0]
print(f"  证据表 place_nodes {evidence:,} 行；副本entities place {replica:,} 行")

random.seed(7)
print("\n-- 随机 10 个（名称 / 行政类型 / 坐标 / cbdb id）--")
for r in random.sample(rows, min(10, len(rows))):
    ext = json.loads(r[5] or "{}")
    admin = ext.get("admin", "")
    cbdb = ext.get("cbdb", "")
    print(f"  {r[1]:<10} {admin:<6} {r[3]:>8.2f},{r[4]:>7.2f}  cbdb={cbdb}")
print("\n验证完成。")
