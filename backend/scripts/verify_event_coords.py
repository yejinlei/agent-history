# -*- coding: utf-8 -*-
"""写入后的验证：图库里的坐标是否与证据表一致，并抽查坐标合理性。

用法：python scripts/verify_event_coords.py [抽样条数]
"""
from __future__ import annotations

import random
import sys
from datetime import datetime
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

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
con = duckdb.connect(str(config.DATA_DIR / "warehouse" / "chronosino.duckdb"),
                     read_only=True)
ev = con.execute(
    "SELECT event_id, place, year, lon, lat, cbdb_addr_id, how "
    "FROM event_coords").fetchall()
print(f"证据表 {len(ev):,} 行")

db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)
tot = c.execute("MATCH (n:Event) RETURN count(*)").get_next()[0]
with_xy = c.execute(
    "MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
    "RETURN count(*)").get_next()[0]
print(f"图库事件 {tot:,} 条；有坐标 {with_xy:,} 条（{with_xy / tot * 100:.1f}%）")

# 1) 全量一致性：证据表每行去图库核对
bad = 0
checked = 0
for eid, place, year, lon, lat, aid, how in ev:
    r = c.execute("MATCH (n:Event) WHERE n.id = $id RETURN n.lon, n.lat",
                  {"id": eid})
    if not r.has_next():
        bad += 1
        print(f"  ✗ 事件不存在：{eid}")
        continue
    glon, glat = r.get_next()
    checked += 1
    if glon is None or abs(glon - lon) > 1e-6 or abs(glat - lat) > 1e-6:
        bad += 1
        if bad <= 5:
            print(f"  ✗ 不一致 {eid}《{place}》证据({lon},{lat}) 图库({glon},{glat})")
print(f"一致性核对：检查 {checked:,} 行，不一致 {bad} 行")

# 2) 越界检查：坐标必须在中国范围内
oob = c.execute(
    "MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 AND "
    "(n.lon < 73 OR n.lon > 136 OR n.lat < 3 OR n.lat > 54) "
    "RETURN count(*)").get_next()[0]
print(f"越界坐标：{oob} 条")

# 3) 抽查打印
random.seed(datetime.now().second)
print(f"\n-- 随机抽查 {N} 条 --")
for eid, place, year, lon, lat, aid, how in random.sample(ev, min(N, len(ev))):
    print(f"  {str(eid)[:34]:<34} 《{place}》{str(year):>6} → "
          f"{lon:>8.2f},{lat:>7.2f}  [{how}]")
print("\n验证完成。")
