# -*- coding: utf-8 -*-
"""校验 Event→Place「发生于」边：端点齐全、坐标与 Place 一致、并可抽查。

用法：python scripts/verify_place_links.py [抽样条数]
"""
from __future__ import annotations

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
from app import config                                        # noqa: E402

import kuzu                                                   # noqa: E402

KUZU_DB = str(config.DATA_DIR / "kg" / "kuzu_db")


def main() -> int:
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    db = kuzu.Database(KUZU_DB, read_only=True)
    c = kuzu.Connection(db)

    total = c.execute("MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
                      "RETURN count(*)").get_next()[0]
    print(f"图上 Event→Place「发生于」边：{total:,} 条")

    n_place = c.execute(
        "MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
        "RETURN count(DISTINCT b.id)").get_next()[0]
    n_event = c.execute(
        "MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
        "RETURN count(DISTINCT a.id)").get_next()[0]
    print(f"  涉及 Place {n_place:,} 个 / Event {n_event:,} 条")

    # 坐标一致性：边的派生语义要求 Event 坐标 == Place 坐标
    q = c.execute(
        "MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
        "RETURN a.id, a.place, a.year, a.lon, a.lat, b.id, b.name, b.lon, b.lat, "
        "r.confidence")
    rows = []
    while q.has_next():
        rows.append(q.get_next())

    ok = drift = noxy = 0
    drift_rows = []
    for aid, aplace, ayear, alon, alat, bid, bname, blon, blat, conf in rows:
        if alon is None or alat is None or blon is None or blat is None:
            noxy += 1
            continue
        d = max(abs(alon - blon), abs(alat - blat))
        if d <= 0.5:
            ok += 1
        else:
            drift += 1
            drift_rows.append((aid, aplace, alon, alat, bid, bname, blon, blat))
    print(f"\n坐标一致性：吻合 {ok:,} / 偏差>0.5° {drift:,} / 缺坐标 {noxy:,}")
    for r in drift_rows[:10]:
        print(f"  ⚠ {r[0]}（{r[1]}）{r[2]},{r[3]} ≠ {r[4]} {r[5]} {r[6]},{r[7]}")

    # 每个 Place 挂了多少事件：看头部垄断情况（正常应是都城独占前几）
    print("\n-- 事件最多的 Place top 10 --")
    cnt = Counter(r[5] for r in rows)          # r[5] = Place 的 id
    name_of = {r[5]: r[6] for r in rows}
    for pid, v in cnt.most_common(10):
        print(f"  {pid:<14} {name_of.get(pid, '?'):<10} {v:>6,} 条事件")

    print(f"\n-- 随机抽样 {n_sample} 条（事件 / 地点 / 事件年 / 坐标）--")
    random.seed(42)
    for aid, aplace, ayear, alon, alat, bid, bname, blon, blat, conf in \
            random.sample(rows, min(n_sample, len(rows))):
        print(f"  {str(aid):<24} {str(aplace):<8} {str(ayear):>6} → "
              f"{str(bid):<14} {bname:<8} {alon},{alat} conf={conf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
