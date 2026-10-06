# -*- coding: utf-8 -*-
"""清掉「CBDB 自身坐标错误」的地址条目在图里留下的一切痕迹。

为什么需要它
------------
CBDB 有个别条目的坐标是错的，比如 addr 4042《益州》被打到吉林(125.49,44.86)，
而益州历来是成都。它一旦被 assimilation 采纳，会同时留下四样东西：
    Event.lon/lat  ·  数仓 event_coords 证据行  ·  Place 节点  ·  Event→Place 边
只清一样就会留下脏碎片，所以要整体一起退。

用法
----
    python scripts/repair_bad_coords.py                 # 干跑
    python scripts/repair_bad_coords.py --apply
    python scripts/repair_bad_coords.py --addr 4042 --apply     # 指定条目

名单在 app/geo_rules.py 的 BAD_ADDR；发现新的坏坐标先加进那里再跑本脚本。
前置条件：服务必须停着（Kuzu 单写者）。
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
from app import config                          # noqa: E402
from app.geo_rules import BAD_ADDR              # noqa: E402

import duckdb                                   # noqa: E402
import kuzu                                     # noqa: E402

KUZU = str(config.DATA_DIR / "kg" / "kuzu_db")
DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--addr", default="", help="只清这一个地址条目 id")
    a = ap.parse_args()

    addrs = [a.addr] if a.addr else sorted(BAD_ADDR)
    log(f"待清理的地址条目：{addrs}")

    con = duckdb.connect(DUCK)
    total = {"event": 0, "place": 0, "edge": 0, "evidence": 0}
    for aid in addrs:
        ev = con.execute(
            "SELECT event_id, place, old_lon, old_lat FROM event_coords "
            "WHERE cbdb_addr_id = ?", [aid]).fetchall()
        place_id = f"place:{aid}"
        nodes = con.execute(
            "SELECT place_id, name, lon, lat FROM place_nodes "
            "WHERE place_id = ? OR cbdb_addr_id = ?", [place_id, aid]).fetchall()
        edges = con.execute(
            "SELECT count(*) FROM event_place_edges WHERE place_id = ?",
            [place_id]).fetchone()[0]
        log(f"\n[{aid}] 事件坐标 {len(ev)} 条 / Place 节点 {len(nodes)} 个 / "
            f"连边证据 {edges} 条")
        for e in ev[:5]:
            log(f"    事件 {e[0]}（place={e[1]}）旧坐标 {e[2]},{e[3]}")
        for n in nodes[:5]:
            log(f"    节点 {n[0]} {n[1]} @ {n[2]},{n[3]}")
        if not a.apply:
            total["event"] += len(ev)
            total["place"] += len(nodes)
            total["edge"] += edges
            continue

        db = kuzu.Database(KUZU)
        c = kuzu.Connection(db)
        # 1) 先清边（Kuzu 删节点要求先把关系摘干净）
        c.execute(
            "MATCH ()-[r:KGREL]->(p:Place {id:$id}) DELETE r", {"id": place_id})
        c.execute(
            "MATCH (p:Place {id:$id})-[r:KGREL]->() DELETE r", {"id": place_id})
        # 2) 再删节点
        c.execute("MATCH (p:Place {id:$id}) DELETE p", {"id": place_id})
        # 3) 事件坐标退回旧值（原样还原，一般是 NULL）
        for eid, _, old_lon, old_lat in ev:
            c.execute(
                "MATCH (n:Event) WHERE n.id = $id "
                "SET n.lon = $lon, n.lat = $lat",
                {"id": eid, "lon": old_lon, "lat": old_lat})
        # 4) 清证据：连边 → Place 节点 → 事件坐标
        con.execute("DELETE FROM event_place_edges WHERE place_id = ?",
                    [place_id])
        con.execute("DELETE FROM place_nodes WHERE place_id = ? OR "
                    "cbdb_addr_id = ?", [place_id, aid])
        con.execute("DELETE FROM event_coords WHERE cbdb_addr_id = ?", [aid])
        con.execute("CHECKPOINT")
        # 5) 自检：重开连接确认真的没了
        c2 = kuzu.Connection(db)
        left_node = c2.execute(
            "MATCH (p:Place {id:$id}) RETURN count(*)",
            {"id": place_id}).get_next()[0]
        left_edge = c2.execute(
            "MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(p:Place {id:$id}) "
            "RETURN count(*)", {"id": place_id}).get_next()[0]
        log(f"  ✓ 已清理：残留节点 {left_node} / 残留边 {left_edge}")
        total["event"] += len(ev)
        total["place"] += len(nodes)
        total["edge"] += edges
        total["evidence"] += len(ev) + len(nodes) + edges
        del c2, c, db

    log(f"\n合计处理：事件坐标 {total['event']} 条 / Place 节点 {total['place']} 个"
        f" / 连边 {total['edge']} 条"
        + ("" if not a.apply else "（已写入并清理证据）"))
    if not a.apply:
        log("干跑结束，未改动任何数据。确认后加 --apply。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
