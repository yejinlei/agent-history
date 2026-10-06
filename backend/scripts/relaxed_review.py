# -*- coding: utf-8 -*-
"""给「放宽年代窗口」那批候选做人工核对准备：分层抽样 + 导出全量清单。

分层的依据：事件年份与 CBDB 词条的年代区间差多少年。差距越大越可疑——
先秦事件挂到唐宋才有的行政条目上，位置未必错（中国地名位置稳定），
但政区治所搬过家就不一定了。所以按 gap 分层，每层单独看准确率。
"""
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import kuzu                                               # noqa: E402
from zhconv import convert as zh_convert                  # noqa: E402

CBDB = Path(config.DATA_DIR) / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db"
KUZU_DB = str(config.DATA_DIR / "kg" / "kuzu_db")
OUT = Path(config.DATA_DIR) / "core" / "normalized" / "relaxed_review.jsonl"

MAC = {"京师", "京城", "京都", "京阙", "京畿", "中都", "上都", "下都", "东京",
       "西京", "北都", "南都", "盛京", "兴京", "行在", "留都", "都城", "宸京"}


def log(m: str) -> None:
    print(m, flush=True)


def build_idx():
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    idx: dict[str, list[tuple]] = defaultdict(list)
    for aid, chn, fy, ly, admin, x, y in con.execute(
            "SELECT c_addr_id, c_name_chn, c_firstyear, c_lastyear, "
            "c_admin_type, x_coord, y_coord FROM ADDR_CODES "
            "WHERE c_name_chn IS NOT NULL AND trim(c_name_chn) <> ''"):
        try:
            fx, fyy = float(x), float(y)
        except Exception:
            continue
        if not (73 <= fx <= 136 and 3 <= fyy <= 54):
            continue
        rec = (int(fy) if str(fy).lstrip("-").isdigit() else None,
               int(ly) if str(ly).lstrip("-").isdigit() else None,
               round(fx, 5), round(fyy, 5), admin or "", str(aid), chn)
        idx[zh_convert(chn.strip(), "zh-cn")].append(rec)
    con.close()
    return idx


def main() -> int:
    db = kuzu.Database(KUZU_DB, read_only=True)
    c = kuzu.Connection(db)
    idx = build_idx()
    rows = []
    q = c.execute("MATCH (n:Event) RETURN n.id, n.name, n.place, n.year, "
                  "n.dynasty_id, n.lon, n.lat")
    n_with_place = 0
    while q.has_next():
        eid, ename, place, year, dyn, lon, lat = q.get_next()
        place = (place or "").strip()
        if not place or place in MAC:
            continue
        n_with_place += 1
        # 已经有坐标的（严格档写过）跳过：这只抽出被年代窗口挡掉的那批
        if lon is not None and lat is not None and lon != 0:
            continue
        cands = idx.get(zh_convert(place, "zh-cn"), [])
        if len(cands) != 1:
            continue
        fy, ly, lon2, lat2, admin, aid, cname = cands[0]
        if year is None or fy is None or ly is None:
            continue
        if fy <= year <= ly:
            continue                      # 严格档本就该命中（不该出现，留作哨兵）
        gap = fy - year if year < fy else year - ly
        rows.append({"event_id": eid, "event_name": ename, "place": place,
                     "year": year, "dynasty": dyn, "addr_id": aid,
                     "addr_name": cname, "admin": admin,
                     "addr_from": fy, "addr_to": ly, "gap": gap,
                     "lon": lon2, "lat": lat2})

    log(f"候选 {len(rows):,} 条（严格档未命中、词表里同名唯一）")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"已导出 {OUT}")

    buckets = Counter()
    for r in rows:
        g = r["gap"]
        b = ("≤50 年" if g <= 50 else "51~200 年" if g <= 200
             else "201~500 年" if g <= 500 else "501~1000 年" if g <= 1000
             else ">1000 年")
        buckets[b] += 1
    print("\n按 gap 分布：")
    for b in ("≤50 年", "51~200 年", "201~500 年", "501~1000 年", ">1000 年"):
        print(f"  {b:<12} {buckets.get(b, 0):>7,}")

    # 每层随机抽 6 条供人工核对
    order = {"≤50 年": 0, "51~200 年": 1, "201~500 年": 2,
             "501~1000 年": 3, ">1000 年": 4}
    by = defaultdict(list)
    for r in rows:
        g = r["gap"]
        b = ("≤50 年" if g <= 50 else "51~200 年" if g <= 200
             else "201~500 年" if g <= 500 else "501~1000 年" if g <= 1000
             else ">1000 年")
        by[b].append(r)
    random.seed(20261007)
    print("\n=== 分层抽样（每层 6 条，供按史地知识逐条核对）===")
    for b in sorted(by, key=lambda x: order[x]):
        print(f"\n[{b}]  共 {len(by[b]):,} 条")
        for r in random.sample(by[b], min(6, len(by[b]))):
            print(f"  {str(r['event_name'])[:22]:<24} place={r['place']:<8} "
                  f"年={r['year']:<6} 词条《{r['addr_name']}》{r['addr_from']}~{r['addr_to']}"
                  f" {r['admin']:<7} gap={r['gap']:<5} → {r['lon']},{r['lat']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
