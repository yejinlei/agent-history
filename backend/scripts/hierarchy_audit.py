# -*- coding: utf-8 -*-
"""用 CBDB 自己的隶属关系做坐标体检：子政区坐标应落在父政区附近。

为什么不用 CHGIS 交叉验证
--------------------------
试过了（`crosscheck_coords.py`）：CHGIS 是清代地名体系，辽金时期的北方地名
（顺州、广宁、柳城、惠州、锦州…）会被大量误报——比如《高昌》CBDB 给的是吐鲁番
（对的），CHGIS 的「高昌」却在两千公里外，报警的其实是 CHGIS。

这一版改用 CBDB 内部的 `ADDR_BELONGS_DATA`（谁隶属于谁）：同一个政区树里，
子节点的坐标不该离父节点太远。这是同源，但来自另一张表、另一套关系，足以
揪出《益州》被填到吉林那种孤立错误。

注意路/道级父节点幅员很大，距离天然偏大，所以阈值给得宽松（400 km），
本脚本**只出报告**，是否清理由人工复核后决定。
"""
from __future__ import annotations

import math
import sqlite3
import sys
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
THRESHOLD_KM = float(sys.argv[1]) if len(sys.argv) > 1 else 400.0


def hav(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def main() -> int:
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    xy: dict[str, tuple[float, float]] = {}
    name: dict[str, str] = {}
    for aid, x, y, nm in con.execute(
            "SELECT c_addr_id, x_coord, y_coord, c_name_chn FROM ADDR_CODES"):
        try:
            xy[str(aid)] = (float(x), float(y))
            name[str(aid)] = nm or ""
        except Exception:
            continue
    parent: dict[str, list[str]] = {}
    for child, par in con.execute(
            "SELECT c_addr_id, c_belongs_to FROM ADDR_BELONGS_DATA"):
        parent.setdefault(str(child), []).append(str(par))
    con.close()
    print(f"CBDB 地址坐标 {len(xy):,} 条；隶属关系 {sum(len(v) for v in parent.values()):,} 条")

    duck = duckdb.connect(DUCK, read_only=True)
    adopted = duck.execute(
        "SELECT cbdb_addr_id, addr_name, place, lon, lat, count(*) FROM event_coords "
        "GROUP BY 1,2,3,4,5 ORDER BY 6 DESC").fetchall()
    duck.close()
    print(f"已采纳的地址条目 {len(adopted):,} 个")

    checked = flagged = no_parent = 0
    rows = []
    for aid, aname, place, lon, lat, cnt in adopted:
        pars = [p for p in parent.get(str(aid), []) if p in xy]
        if not pars:
            no_parent += 1
            continue
        checked += 1
        dists = sorted((hav(lon, lat, *xy[p]), p) for p in pars)
        best = dists[0]
        if best[0] > THRESHOLD_KM:
            flagged += 1
            rows.append((round(best[0]), aid, place, aname, lon, lat,
                         name.get(best[1], "?"), xy[best[1]], cnt))

    print(f"\n有父节点可核的 {checked:,} 个（{no_parent:,} 个在 CBDB 里没有带坐标的上级）")
    print(f"  **离最近父节点 >{THRESHOLD_KM:.0f} km：{flagged} 个**")

    rows.sort(key=lambda r: (-r[0], -r[8]))
    print(f"\n=== 疑似坐标有误（按偏差降序）===")
    print(f"{'km':>6} {'图上place':<10} {'词条':<10} {'CBDB坐标':<22} "
          f"{'最近父地名':<10} {'父坐标':<22} 事件数")
    for r in rows[:40]:
        print(f"{r[0]:>6} {r[2]:<10} {str(r[3]):<10} "
              f"{str(r[4]) + ',' + str(r[5]):<22} {str(r[6]):<10} "
              f"{str(r[7][0]) + ',' + str(r[7][1]):<22} {r[8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
