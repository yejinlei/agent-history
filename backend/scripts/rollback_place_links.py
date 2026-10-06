# -*- coding: utf-8 -*-
"""一键还原：删掉 link_event_place.py 建立的 (Event)-[:发生于]->(Place) 边。

依据是数仓证据表 event_place_edges，所以**只删本脚本写过的边**——手工种子的
那 4 条老边（如 `yangshao_rise -发生于-> yangshao_v`）不在证据表里，不受影响。

用法
----
    python scripts/rollback_place_links.py                  # 干跑，看会删多少
    python scripts/rollback_place_links.py --apply          # 真的删
    python scripts/rollback_place_links.py --apply --batch 20261007-020918
    python scripts/rollback_place_links.py --apply --drop-evidence  # 删后清证据表

前置条件：服务必须停着（Kuzu 单写者，靠文件锁保证；在线回滚会拿到脏读）。
注意：批量删边会「毒化」连接（见 store.py 注释），所以每 2000 条重开一次连接。
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
from app import config                                        # noqa: E402

import duckdb                                                 # noqa: E402
import kuzu                                                   # noqa: E402

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

    con = duckdb.connect(DUCK)
    try:
        if a.batch:
            rows = con.execute(
                "SELECT event_id, place_id FROM event_place_edges "
                "WHERE batch = ?", [a.batch]).fetchall()
        else:
            rows = con.execute(
                "SELECT event_id, place_id FROM event_place_edges").fetchall()
    except Exception as e:
        log(f"读证据表失败（可能从未连过边）：{e}")
        con.close()
        return 0

    log(f"待删除 {len(rows):,} 条"
        + (f"（批次 {a.batch}）" if a.batch else "（全部批次）"))
    if not rows:
        con.close()
        return 0
    if not a.apply:
        for eid, pid in rows[:5]:
            log(f"  样例：{eid} → {pid}")
        log("干跑结束，未改动任何数据。确认后加 --apply。")
        con.close()
        return 0

    db = kuzu.Database(KUZU)
    c = kuzu.Connection(db)
    n = miss = 0
    for i, (eid, pid) in enumerate(rows, 1):
        try:
            c.execute(
                "MATCH (a:Event {id:$e})-[r:KGREL {rel:'发生于'}]->(b:Place {id:$p}) "
                "DELETE r", {"e": eid, "p": pid})
        except Exception as e:
            log(f"  ✗ {eid} → {pid}: {type(e).__name__}: {e}")
            miss += 1
            continue
        n += 1
        if i % 2000 == 0:
            c = None
            c = kuzu.Connection(db)   # 批量删边后重开，避免连接的内存态毒化
            log(f"  已删 {i:,} 条")

    left = (c := kuzu.Connection(db)).execute(
        "MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
        "RETURN count(*)").get_next()[0]
    log(f"✓ 删除 {n:,} 条（失败 {miss} 条）；"
        f"图上剩余 Event→Place「发生于」边 {left:,} 条")

    if a.drop_evidence:
        if a.batch:
            con.execute("DELETE FROM event_place_edges WHERE batch = ?", [a.batch])
        else:
            con.execute("DELETE FROM event_place_edges")
        log("证据表已清空")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
