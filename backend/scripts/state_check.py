"""只读体检：列出图库 / 数仓当前状态（用于备份前后核对）。只读，不改任何数据。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kuzu                                    # noqa: E402
import duckdb                                  # noqa: E402
from app import config                         # noqa: E402

KUZU_DB = config.DATA_DIR / "kg" / "kuzu_db"
DUCK = config.DATA_DIR / "warehouse" / "chronosino.duckdb"

db = kuzu.Database(str(KUZU_DB), read_only=True)
c = kuzu.Connection(db)


def cnt(q: str):
    return c.execute(q).get_next()[0]


print("=== 图库 ===")
for tbl in ("Dynasty", "Event", "Person", "Place", "Book", "Institution",
            "Artifact", "Concept"):
    try:
        print(f"  {tbl:<12} {cnt(f'MATCH (n:{tbl}) RETURN count(*)'):>8,}")
    except Exception as e:
        print(f"  {tbl:<12} 读取失败 {type(e).__name__}")
print(f"  {'KGREL':<12} {cnt('MATCH ()-[r:KGREL]->() RETURN count(*)'):>8,}")

ev_all = cnt("MATCH (n:Event) RETURN count(*)")
ev_xy = cnt("MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
            "RETURN count(*)")
print(f"\n  Event 有坐标 {ev_xy:,} / {ev_all:,}"
      f"（{ev_xy / ev_all * 100:.1f}%）")
for lbl in ("Person", "Place"):
    a = cnt(f"MATCH (n:{lbl}) RETURN count(*)")
    xy = cnt(f"MATCH (n:{lbl}) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
             f"RETURN count(*)")
    print(f"  {lbl} 有坐标 {xy:,} / {a:,}（{xy / max(a,1) * 100:.1f}%）")

# Event 坐标 的来源分布（按 Place？没有该列，只能按是否已写判断）
print("\n=== 数仓表 ===")
con = duckdb.connect(str(DUCK), read_only=True)
rows = con.execute(
    "SELECT table_name, estimated_size FROM duckdb_tables() "
    "ORDER BY table_name").fetchall()
for name, size in rows:
    try:
        n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    except Exception:
        n = -1
    print(f"  {name:<28} {n:>10,} 行")
con.close()

# Place 节点现状
print("\n=== Place 节点明细 ===")
q = c.execute("MATCH (n:Place) RETURN n.id, n.name, n.lon, n.lat "
              "ORDER BY n.id LIMIT 20")
while q.has_next():
    i, nm, lo, la = q.get_next()
    print(f"  {str(i):<14} {str(nm):<10} {lo}, {la}")

# 已被写入坐标的 Event：按 place 值统计最常见者（感知分布）
print("\n=== 已补坐标的 Event：place 高频 top 10 ===")
samples = Counter()
q = c.execute("MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
              "RETURN n.place")
while q.has_next():
    samples[q.get_next()[0]] += 1
for k, v in samples.most_common(10):
    print(f"  {str(k):<12} {v:>6,}")
