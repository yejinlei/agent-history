# -*- coding: utf-8 -*-
"""查「坐标撞车」：同一个坐标被多个不同地名共用，多半是 CBDB 抄串了。

起因：抽查发现《登州》被打到 112.08,32.68，而那个坐标同时属于《鄧州》（河南邓州）——
CBDB 里 8 条《登州》全用同一个坐标，说明它把「登州（山东蓬莱）」和「鄧州（河南邓州）」
并成了同一个点。既然查得到一处，就该把这类喂食全院，因为每个这样的错误都会
让若干事件被打到几百公里外。

方法：只看**已采纳**的地址条目（真正写进图库的那些），按坐标聚类，
凡是有两个及以上不同 grabs（地上地 Kumasi）名字共用一个坐标，就列出来人工看。

用法：python scripts/collision_audit.py
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import duckdb                                             # noqa: E402

CBDB = Path(config.DATA_DIR) / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db"
DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")


def main() -> int:
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    coord_to_names: dict[tuple[float, float], set[str]] = defaultdict(set)
    names_of_addr: dict[str, set[str]] = defaultdict(set)
    for aid, chn, x, y in con.execute(
            "SELECT c_addr_id, c_name_chn, x_coord, y_coord FROM ADDR_CODES "
            "WHERE x_coord IS NOT NULL AND y_coord IS NOT NULL"):
        try:
            key = (round(float(x), 4), round(float(y), 4))
        except Exception:
            continue
        nm = (chn or "").strip()
        if nm and not nm.startswith("["):
            coord_to_names[key].add(nm)
        names_of_addr[str(aid)].add(nm)
    con.close()

    duck = duckdb.connect(DUCK, read_only=True)
    adopted = duck.execute(
        "SELECT cbdb_addr_id, place, lon, lat, count(*) FROM event_coords "
        "GROUP BY 1,2,3,4 ORDER BY 5 DESC").fetchall()
    duck.close()
    print(f"已采纳 {len(adopted):,} 个地址条目")

    # 按坐标聚：同一个坐标 → 都采用了哪些图上地名
    by_coord: dict[tuple, list[tuple]] = defaultdict(list)
    for aid, place, lon, lat, cnt in adopted:
        by_coord[(round(lon, 4), round(lat, 4))].append((place, aid, cnt))

    print("\n=== 同一坐标被多个不同 adopted 地名共用 ===")
    n_hit = 0
    for key, items in sorted(by_coord.items(), key=lambda kv: -sum(i[2] for i in kv[1])):
        names = {i[0] for i in items}
        if len(names) < 2:
            continue
        n_hit += 1
        cb = coord_to_names.get(key, set())
        print(f"\n坐标 {key[0]},{key[1]}  涉及事件 {sum(i[2] for i in items)} 条")
        print(f"  图上的地名：{sorted(names)}")
        print(f"  CBDB 里同坐标的地名（{len(cb)} 个）：{sorted(cb)[:8]}"
              f"{' …' if len(cb) > 8 else ''}")
        for place, aid, cnt in sorted(items, key=lambda x: -x[2]):
            print(f"    {place:<10} {cnt:>4} 条  addr={aid}")
    print(f"\n共 {n_hit} 处坐标撞车")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
