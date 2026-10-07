# -*- coding: utf-8 -*-
"""按证据表把丢掉的坐标回填到 Event 节点。

什么时候会用到
--------------
Event 节点如果被重建（例如某本书卸载后重新挂载、或直连写入时按 id 覆盖），
节点上的 lon/lat 会跟着一起没了，但数仓 event_coords 里的证据（含写入前旧值）
还在。这时不需要重跑地名匹配，按证据表回填即可还原。

用法
----
    python scripts/restore_event_coords.py            # 干跑：列出待回填数量
    python scripts/restore_event_coords.py --apply    # 回填
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                       # noqa: E402

import duckdb                                                # noqa: E402
import kuzu                                                  # noqa: E402

KUZU_DB = Path(config.DATA_DIR) / "kg" / "kuzu_db"
DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    ap.add_argument("--buffer-pool-mb", type=int, default=256)
    a = ap.parse_args()

    dcon = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    ev_rows = dcon.execute(
        "SELECT event_id, lon, lat FROM event_coords "
        "WHERE lon IS NOT NULL AND lat IS NOT NULL").fetchall()
    dcon.close()
    ev = {r[0]: (r[1], r[2]) for r in ev_rows}
    log(f"证据表里有坐标的事件 {len(ev):,}")

    db = kuzu.Database(str(KUZU_DB),
                       buffer_pool_size=int(a.buffer_pool_mb * 1024 ** 2))
    con = kuzu.Connection(db)
    q = con.execute(
        "MATCH (n:Event) WHERE n.lon IS NULL AND n.place IS NOT NULL "
        "AND n.place <> '' RETURN n.id")
    missing = []
    while q.has_next():
        missing.append(q.get_next()[0])
    log(f"图库里缺坐标（有地名）的事件 {len(missing):,}")

    todo = [(i, *ev[i]) for i in missing if i in ev]
    log(f"→ 可按证据表回填 {len(todo):,}")
    if not a.apply:
        log("\n干跑结束，未写入。确认后加 --apply。")
        return 0
    if not todo:
        return 0

    wcon = kuzu.Connection(db)
    n = 0
    try:
        for i, lon, lat in todo:
            wcon.execute(
                "MATCH (n:Event) WHERE n.id = $id SET n.lon = $lon, n.lat = $lat",
                {"id": i, "lon": lon, "lat": lat})
            n += 1
            if n % 2000 == 0:
                wcon = None
                wcon = kuzu.Connection(db)
                log(f"  已回填 {n:,} 条")
    except Exception as e:
        log(f"✗ 中断于第 {n} 条：{type(e).__name__}: {e}")
        return 1
    chk = kuzu.Connection(db)
    tot = chk.execute("MATCH (n:Event) RETURN count(n)").get_next()[0]
    got = chk.execute(
        "MATCH (n:Event) WHERE n.lon IS NOT NULL RETURN count(n)").get_next()[0]
    log(f"\n✓ 回填完成：{n:,} 条")
    log(f"自检：Event {tot:,}，有坐标 {got:,}（{100 * got / tot:.1f}%）")
    wcon = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
