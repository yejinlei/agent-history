# -*- coding: utf-8 -*-
"""考题：用一组「史上有明确答案」的名人籍贯，横向测不同取值策略的准确率。

背景：CBDB 的 c_index_addr（Basic Affiliation）在诸葛亮身上是成都（那是他在蜀汉
的执政地，真正籍贯是琅琊阳都＝山东沂南），在曹操身上是洛阳（真正籍贯沛国谯＝
安徽亳州）。直接写会把核心人物标错。本脚本用黄金集定量比出最优策略，再决定写不写。
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
from zhconv import convert as zh_convert

DB = Path(r"C:\agent-history-data\core\raw\cbdb\CBDB_20240208_sqlite.db")

# （人的名字, 期望籍贯名, 期望经度, 期望纬度）—— 取史书通说，容差 120 km
GOLDEN = [
    ("曹操", "沛国谯县(亳州)", 115.78, 33.87),
    ("诸葛亮", "琅琊阳都(沂南)", 118.40, 35.55),
    ("岳飞", "相州汤阴", 114.35, 35.93),
    ("王安石", "临川(抚州)", 116.30, 27.95),
    ("司马迁", "夏阳(韩城)", 110.45, 35.48),
    ("杜甫", "巩县(巩义)", 112.98, 34.75),
    ("苏轼", "眉州眉山", 103.83, 30.05),
    ("曾国藩", "湘乡(双峰)", 112.20, 27.48),
    ("张居正", "江陵(荆州)", 112.24, 30.35),
    ("朱元璋", "濠州钟离(凤阳)", 117.56, 32.87),
    ("李白", "绵州昌隆(江油)", 104.75, 31.78),
    ("欧阳修", "庐陵(吉安)", 114.99, 27.12),
    ("司马光", "陕州夏县", 111.22, 35.14),
    ("范仲淹", "苏州吴县", 120.62, 31.31),
]

# 取值策略：地址类型的优先级，越靠前越优先
STRATEGIES = {
    "A_index_first": None,                    # 直接用 BIOG_MAIN.c_index_addr
    "B_ancestral_only": [5, 7, 8],            # 只要祖籍/本貫/出生地
    "C_home_then_index": [5, 8, 7, 6, 16, 3, 1],
    "D_index_then_all": [1, 5, 7, 6, 16, 8],  # 基本地址优先
}


def haversine(lon1, lat1, lon2, lat2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    addr_xy: dict[str, tuple] = {}
    addr_name: dict[str, str] = {}
    for cid, nm, x, y in con.execute(
            "SELECT c_addr_id, c_name_chn, x_coord, y_coord FROM ADDR_CODES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        try:
            fx, fy = float(x), float(y)
        except Exception:
            continue
        if 73 <= fx <= 136 and 3 <= fy <= 54:
            addr_xy[str(cid)] = (fx, fy)
            addr_name[str(cid)] = nm
    for cid, nm, x, y in con.execute(
            "SELECT c_addr_id, c_name_chn, x_coord, y_coord FROM ADDRESSES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        if str(cid) not in addr_xy:
            try:
                fx, fy = float(x), float(y)
            except Exception:
                continue
            if 73 <= fx <= 136 and 3 <= fy <= 54:
                addr_xy[str(cid)] = (fx, fy)
                addr_name[str(cid)] = nm

    print(f"{'人物':<10}{'期望籍贯':<20}", end="")
    for s in STRATEGIES:
        print(f"{s:<18}", end="")
    print()
    print("-" * (30 + 18 * len(STRATEGIES)))

    score = {s: 0 for s in STRATEGIES}
    tested = 0
    for name, want_name, wlon, wlat in GOLDEN:
        # CBDB 里是繁体（諸葛亮/岳飛/蘇軾）， myeloidCBDB 得两边都查
        # CBDB 里存的是繁体（諸葛亮 / 岳飛 / 蘇軾），必须繁简两边都查
        forms = {name, zh_convert(name, "zh-hant"), zh_convert(name, "zh-tw")}
        pids: list = []
        for nm in forms:
            for r in con.execute(
                    "SELECT c_personid FROM BIOG_MAIN WHERE c_name_chn = ?", (nm,)):
                if r[0] not in pids:
                    pids.append(r[0])
        if not pids:
            print(f"{name:<10}{want_name:<20}{'（CBDB 无此人）'}", end="")
            for _ in STRATEGIES:
                print(f"{'-':<18}", end="")
            print()
            continue
        tested += 1
        print(f"{name:<10}{want_name:<20}", end="")
        for s, order in STRATEGIES.items():
            got = None
            if order is None:
                r = con.execute(
                    "SELECT c_index_addr_id FROM BIOG_MAIN WHERE c_personid = ?",
                    (pids[0],)).fetchone()
                if r and r[0] and str(r[0]) in addr_xy:
                    got = addr_xy[str(r[0])]
            else:
                for t in order:
                    r = con.execute(
                        "SELECT c_addr_id FROM BIOG_ADDR_DATA "
                        "WHERE c_personid = ? AND c_addr_type = ?",
                        (pids[0], t)).fetchone()
                    if r and str(r[0]) in addr_xy:
                        got = addr_xy[str(r[0])]
                        break
            if got is None:
                print(f"{'无解':<18}", end="")
                continue
            d = haversine(got[0], got[1], wlon, wlat)
            mark = "✓" if d <= 120 else "✗"
            if d <= 120:
                score[s] += 1
            print(f"{mark}{d:>7.0f}km{'':<8}", end="")
        print()

    print("-" * (30 + 18 * len(STRATEGIES)))
    print(f"{'准确率':<30}", end="")
    for s in STRATEGIES:
        print(f"{score[s]}/{tested} ({score[s] / max(tested, 1) * 100:.0f}%){'':<6}",
              end="")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
