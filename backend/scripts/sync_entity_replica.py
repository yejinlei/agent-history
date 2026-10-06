# -*- coding: utf-8 -*-
"""把图库里的实体投影全量重灌进 DuckDB 副本（含新补的坐标）。

为什么需要它
------------
时间窗 / 空间窗 / 全文检索走的是数仓 `entities` 副本，不是 Kuzu 本身。
而 Store 起服时的同步策略是：
    full = (warehouse.entity_count() == 0)     # 只有副本为空才全量重灌
直接改过 Kuzu 的坐标以后，副本非空 → 走增量 → **新坐标不会流进副本**，
地图上依旧查不到点。所以这类"绕过 Store 直接改图库"的批量操作之后，
必须跑一次本脚本手动全量重灌。

用法（服务必须停着：Kuzu 要独占读，DuckDB 要独占写）
--------------------------------------------
    python scripts/sync_entity_replica.py            # 干跑，只统计
    python scripts/sync_entity_replica.py --apply    # 真重灌
"""
from __future__ import annotations

import argparse
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
from app.warehouse import warehouse

import kuzu


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def as_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    log("只读打开图库…")
    db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
    c = kuzu.Connection(db)

    rows: list[dict] = []
    q = c.execute(
        "MATCH (n:Event) RETURN n.id, n.name, n.year, n.dynasty_id, "
        "n.lon, n.lat, n.from_book")
    n_evt = 0
    n_evt_xy = 0
    while q.has_next():
        eid, name, year, did, lon, lat, fb = q.get_next()
        n_evt += 1
        if lon and lat and lon != 0:
            n_evt_xy += 1
        rows.append({"entity_id": eid, "kind": "event", "name": name,
                     "year": as_int(year), "dynasty_id": did, "place_id": None,
                     "lon": lon, "lat": lat, "from_book": fb, "confidence": None})
    log(f"事件 {n_evt:,} 条（有坐标 {n_evt_xy:,}）")

    q = c.execute(
        "MATCH (n:Person) RETURN n.id, n.name, n.years, n.dynasty_id, "
        "n.lon, n.lat, n.from_book")
    n_per = n_per_xy = 0
    while q.has_next():
        pid, name, years, did, lon, lat, fb = q.get_next()
        n_per += 1
        if lon and lat and lon != 0:
            n_per_xy += 1
        rows.append({"entity_id": pid, "kind": "person", "name": name,
                     "year": as_int(years[0] if years else None),
                     "dynasty_id": did, "place_id": None,
                     "lon": lon, "lat": lat, "from_book": fb, "confidence": None})
    log(f"人物 {n_per:,} 条（有坐标 {n_per_xy:,}）")

    q = c.execute("MATCH (n:Place) RETURN n.id, n.name, n.lon, n.lat")
    n_pl = 0
    while q.has_next():
        plid, name, lon, lat = q.get_next()
        n_pl += 1
        rows.append({"entity_id": plid, "kind": "place", "name": name,
                     "year": None, "dynasty_id": None, "place_id": plid,
                     "lon": lon, "lat": lat, "from_book": None,
                     "confidence": None})
    log(f"地点 {n_pl:,} 条")
    c = None
    db = None

    log(f"合计待写入 {len(rows):,} 行")
    if not rows:
        log("图库实体为空，不重灌（避免把副本擦干净）")
        return 0

    before = warehouse.entity_count()
    xy_before = 0
    try:
        xy_before = warehouse.query(
            "SELECT count(*) FROM entities WHERE lon IS NOT NULL AND lon <> 0"
        )[0][0]
    except Exception:
        pass
    log(f"重灌前：副本 {before:,} 行，其中有坐标 {xy_before:,} 行")

    if not a.apply:
        log("干跑结束，未改动副本。确认后加 --apply。")
        return 0

    n = warehouse.replace_entities(rows)
    after = warehouse.entity_count()
    xy_after = warehouse.query(
        "SELECT count(*) FROM entities WHERE lon IS NOT NULL AND lon <> 0")[0][0]
    log(f"✓ 重灌完成：写入 {n:,} 行；副本现 {after:,} 行，有坐标 {xy_after:,} 行")

    # 抽查：随便取几条有坐标的事件核对图库副本一致性
    sample = warehouse.query(
        "SELECT entity_id, name, year, lon, lat FROM entities "
        "WHERE kind = 'event' AND lon IS NOT NULL AND lon <> 0 LIMIT 8")
    log("-- 副本抽查 8 条 --")
    for r in sample:
        log(f"   {r[0][:30]:<30} {r[1][:10]:<10} {str(r[2]):>6} → "
            f"{r[3]:.2f},{r[4]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
