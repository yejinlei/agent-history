"""清掉重复的 Event 节点。

由来：提案 id 是按序号生成的（mingshi_e0、e1…），补年、复核之后事件集合一变，
序号整体错位，同一件事换个 id 又被挂一遍。图上因此有近万组同书同名同年的节点。

做法：同组里留一个「信息最全」的（优先有坐标的，其次序号最小的），把缺的
坐标并过去，其余 DETACH DELETE。删完记得重跑 link_event_place.py 与挂载，
把「发生于」「书载观点」这些边按新的节点补回来。

用法
----
    python scripts/dedup_events.py            # 干跑，只报数
    python scripts/dedup_events.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config                                     # noqa: E402

import kuzu                                                # noqa: E402

BATCH = 500


def _seq(nid: str) -> int:
    m = re.search(r"_e(\d+)$", nid or "")
    return int(m.group(1)) if m else 10 ** 9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--buffer-pool-mb", type=int, default=512)
    a = ap.parse_args()

    db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"),
                       buffer_pool_size=a.buffer_pool_mb * 1024 ** 2)
    con = kuzu.Connection(db)

    q = con.execute(
        "MATCH (n:Event) WITH n.from_book AS b, n.name AS nm, n.year AS y, "
        "collect(n.id) AS ids, count(*) AS c WHERE c > 1 "
        "RETURN b, nm, y, ids ORDER BY c DESC")
    groups = []
    while q.has_next():
        groups.append(q.get_next())
    n_dup = sum(len(g[3]) - 1 for g in groups)
    print(f"重复组 {len(groups):,} 个，多余节点 {n_dup:,} 个"
          f"{'' if a.apply else '（干跑）'}")

    # 一次取出全部节点的坐标，避免逐组回查
    coord: dict[str, tuple] = {}
    q2 = con.execute("MATCH (n:Event) RETURN n.id, n.lon, n.lat, n.place")
    while q2.has_next():
        i, lon, lat, pl = q2.get_next()
        coord[i] = (lon, lat, pl)
    print(f"已读 {len(coord):,} 个节点的坐标")

    n_del = n_fill = 0
    for i, (b, nm, y, ids) in enumerate(groups):
        ids = sorted(ids, key=_seq)
        keeper = next((x for x in ids if coord.get(x, (None,) * 3)[0]
                       is not None), ids[0])
        klon, klat, kpl = coord.get(keeper, (None, None, None))
        if klon is None:
            src = next((x for x in ids
                        if coord.get(x, (None,) * 3)[0] is not None), None)
            if src and a.apply:
                slon, slat, spl = coord[src]
                con.execute(
                    "MATCH (n:Event {id:$id}) SET n.lon=$lon, n.lat=$lat, "
                    "n.place=$pl", {"id": keeper, "lon": slon, "lat": slat,
                                    "pl": spl or ""})
                n_fill += 1
        for dup in ids:
            if dup == keeper:
                continue
            if a.apply:
                con.execute("MATCH (n:Event {id:$id}) DETACH DELETE n",
                            {"id": dup})
            n_del += 1
        if a.apply and (i + 1) % BATCH == 0:
            q3 = con.execute("MATCH (n:Event) RETURN count(n)")
            print(f"  进度 {i + 1}/{len(groups)}，当前 Event {q3.get_next()[0]:,}")

    print(f"{'✓ 已' if a.apply else '拟'}删除重复节点 {n_del:,} 个；"
          f"补坐标 {n_fill:,} 条")
    if a.apply:
        q4 = con.execute("MATCH (n:Event) RETURN count(n)")
        print(f"自检：Event {q4.get_next()[0]:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
