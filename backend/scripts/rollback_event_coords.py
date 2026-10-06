# -*- coding: utf-8 -*-
"""一键还原：把 enrich_event_coords.py 写进去的坐标退回原样。

依据是数仓里的证据表 event_coords（记录了写入前的 old_lon / old_lat），
所以**只有走过 enrich 的事件才会被还原**，其它事件不会被碰。

用法
----
    python scripts/rollback_event_coords.py                  # 干跑，看会还原多少
    python scripts/rollback_event_coords.py --apply          # 真的还原
    python scripts/rollback_event_coords.py --apply --batch 20261006-231049
    python scripts/rollback_event_coords.py --apply --drop-evidence   # 还原后清证据表

前置条件：服务必须停着（Kuzu 单写者，靠文件锁保证；在线回滚会拿到脏读）。
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

import duckdb
import kuzu

DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
KUZU = str(config.DATA_DIR / "kg" / "kuzu_db")


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", default="", help="只回滚指定批次")
    ap.add_argument("--drop-evidence", action="store_true",
                    help="还原后清空证据表（默认保留，留作审计）")
    a = ap.parse_args()

    con = duckdb.connect(DUCK)            # 读写：还原后要更新/清证据
    try:
        rows = con.execute("SELECT event_id, old_lon, old_lat FROM event_coords")
        rows = rows.fetchall()
    except Exception as e:
        log(f"读证据表失败（可能从未写入过坐标）：{e}")
        con.close()
        return 0
    if a.batch:
        rows = con.execute(
            "SELECT event_id, old_lon, old_lat FROM event_coords WHERE batch = ?",
            [a.batch]).fetchall()
    log(f"待还原 {len(rows):,} 条" + (f"（批次 {a.batch}）" if a.batch else ""))
    if not rows:
        con.close()
        return 0
    if not a.apply:
        log("干跑结束，未改动任何数据。确认后加 --apply。")
        con.close()
        return 0

    db = kuzu.Database(KUZU)              # 独占写
    c = kuzu.Connection(db)
    n = miss = 0
    try:
        for i, (eid, old_lon, old_lat) in enumerate(rows, 1):
            # 原值是 NULL 或 0 都要如实还原：0 代表"无坐标"的历史写法
            val_lon = None if old_lon is None else float(old_lon)
            val_lat = None if old_lat is None else float(old_lat)
            r = c.execute(
                "MATCH (n:Event) WHERE n.id = $id SET n.lon = $lon, n.lat = $lat "
                "RETURN n.id", {"id": eid, "lon": val_lon, "lat": val_lat})
            if r.has_next():
                n += 1
            else:
                miss += 1
            if i % 2000 == 0:
                c = None
                c = kuzu.Connection(db)
                log(f"  已还原 {i:,} 条")
    except Exception as e:
        log(f"✗ 还原中断于第 {n} 条：{type(e).__name__}: {e}")
        log("  证据表还在，重跑本脚本可继续（已还原的会被重新赋成同样的旧值，幂等）")
        con.close()
        return 1

    left = c.execute(
        "MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
        "RETURN count(*)").get_next()[0]
    log(f"✓ 还原 {n:,} 条（图库里找不到对应事件的 {miss} 条）；"
        f"图库现存有坐标事件 {left:,} 条")

    if a.drop_evidence:
        if a.batch:
            con.execute("DELETE FROM event_coords WHERE batch = ?", [a.batch])
        else:
            con.execute("DELETE FROM event_coords")
        log("证据表已清空")
    con.close()
    c = None
    db = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
