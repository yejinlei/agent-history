"""给缺坐标的 Event 补坐标（CBDB 地址表按年代匹配出来的候选）。

数据来源：scripts/extract_core.py 派生的
  core/normalized/event_coord_candidates_relaxed200.jsonl  —— 匹配半径 ≤200km（精度较高）
  core/normalized/event_coord_candidates_relaxed500.jsonl  —— 匹配半径 ≤500km（精度较低，默认不用）

写入原则：
  - 只补 lon/lat 当前为空的事件，**不覆盖已有坐标**（已有的是更上游的结果）；
  - 200 与 500 两份重叠时以 200 为准；
  - 两个文件里 old_lon/old_lat 非空的条目说明本来就有坐标，跳过（不重复写）；
  - 写库前导出撤销文件（记录被写入的 event_id 及其原值，撤销=还原）。

用法：
  python scripts/fill_event_coords.py                 # 干跑
  python scripts/fill_event_coords.py --apply         # 写库（默认只用 ≤200 那份）
  python scripts/fill_event_coords.py --apply --include-500   # 连 ≤500 那份一起
  python scripts/fill_event_coords.py --undo FILE     # 还原
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
NORM = os.path.join(DATA_DIR, "core", "normalized")
F200 = os.path.join(NORM, "event_coord_candidates_relaxed200.jsonl")
F500 = os.path.join(NORM, "event_coord_candidates_relaxed500.jsonl")
OUT = os.path.join(DATA_DIR, "kg")
DUCKDB_PATH = os.path.join(DATA_DIR, "warehouse", "chronosino.duckdb")


def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        print(f"[warn] 候选文件不存在：{path}")
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-500", dest="inc500", action="store_true",
                    help="同时应用 ≤500km 那份（精度较低）")
    ap.add_argument("--undo", metavar="FILE")
    ap.add_argument("--batch", type=int, default=50,
                    help="写库批大小（Kuzu 批量 SET 偶发内部断言，取小值更稳）")
    args = ap.parse_args()

    c = kuzu.Connection(
        kuzu.Database(KG, read_only=not args.apply and not args.undo))

    if args.undo:
        rows = [json.loads(l) for l in open(args.undo, encoding="utf-8") if l.strip()]
        for i, r in enumerate(rows, 1):
            c.execute("MATCH (e:Event {id:$id}) SET e.lon=$lon, e.lat=$lat",
                      {"id": r["event_id"], "lon": r["old_lon"], "lat": r["old_lat"]})
            if i % 200 == 0:
                print(f"  回滚 {i}/{len(rows)}", end="\r")
        print(f"\n回滚完成 {len(rows)} 条")
        return 0

    print("读取 Event 现有坐标 ...")
    res = c.execute("MATCH (e:Event) RETURN e.id, e.lon, e.lat")
    cur = {}
    while res.has_next():
        eid, lon, lat = res.get_next()
        cur[eid] = (lon, lat)
    total = len(cur)
    have = sum(1 for lon, _ in cur.values() if lon is not None)
    print(f"Event {total:,}；已有坐标 {have:,}（{have/total*100:.1f}%）；"
          f"缺坐标 {total-have:,}")

    # 候选：200 优先，500 兜底
    plan: dict[str, dict] = {}
    stat = Counter()
    for path, tag in ((F200, "200"), (F500, "500")):
        if tag == "500" and not args.inc500:
            continue
        for d in _load(path):
            eid = d.get("event_id")
            if not eid or eid in plan:
                stat["dup" if eid in plan else "no_id"] += 1
                continue
            if d.get("lon") is None or d.get("lat") is None:
                stat["no_cand_xy"] += 1
                continue
            if d.get("old_lon") is not None:
                stat["already_in_cand"] += 1   # 候选文件里就标了原本有坐标
                continue
            if eid not in cur:
                stat["not_in_graph"] += 1
                continue
            lon0, lat0 = cur[eid]
            if lon0 is not None:
                stat["already_has_xy"] += 1
                continue
            plan[eid] = {"event_id": eid, "lon": float(d["lon"]),
                         "lat": float(d["lat"]), "old_lon": lon0,
                         "old_lat": lat0, "how": d.get("how", ""),
                         "place": d.get("place", ""), "src": tag}
            stat["plan_" + tag] += 1

    print("\n== 统计 ==")
    for k in sorted(stat):
        print(f"   {k:18s} {stat[k]:8,}")
    n_plan = len(plan)
    print(f"   待写入            {n_plan:8,}")
    how = Counter(p["how"] for p in plan.values())
    print(f"   匹配方式：{dict(how)}")
    after = have + n_plan
    print(f"   写入后覆盖率 {after/total*100:.1f}%（原 {have/total*100:.1f}%）")
    for p in list(plan.values())[:8]:
        print(f"   {p['place'] or '(无地名)':12s} -> {p['lon']:.3f},{p['lat']:.3f} "
              f"[{p['how']}/{p['src']}]")

    if not args.apply:
        print("\n[干跑] 未写库。加 --apply 生效。")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    undo_fp = os.path.join(OUT, f"fill_coords_undo_{ts}.jsonl")
    with open(undo_fp, "w", encoding="utf-8") as fh:
        for p in plan.values():
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\n撤销文件: {undo_fp}")

    items = list(plan.values())
    done = 0
    try:
        for i in range(0, len(items), args.batch):
            chunk = items[i:i + args.batch]
            for p in chunk:
                c.execute("MATCH (e:Event {id:$id}) SET e.lon=$lon, e.lat=$lat",
                          {"id": p["event_id"], "lon": p["lon"], "lat": p["lat"]})
            done += len(chunk)
            print(f"  写入 {done}/{len(items)}", end="\r")
    except Exception as e:  # noqa: BLE001
        print(f"\n批次失败（已写 {done} 条）：{e}")
        print(f"已写入部分可用 --undo {undo_fp} 还原（撤销文件含全部计划条目）")
        return 1
    print(f"\n写入完成 {done} 条")

    # 同步 DuckDB 副本
    try:
        import duckdb
        con = duckdb.connect(DUCKDB_PATH)
        con.executemany(
            "UPDATE entities SET lon=?, lat=? WHERE entity_id=? AND lon IS NULL",
            [(p["lon"], p["lat"], p["event_id"]) for p in items])
        n = con.execute(
            "SELECT count(*) FROM entities WHERE kind='event' AND lon IS NULL"
        ).fetchone()[0]
        print(f"数仓副本已同步；剩余无坐标事件 {n:,}")
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 数仓副本同步失败（图库已写入，可重跑同步）：{e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
