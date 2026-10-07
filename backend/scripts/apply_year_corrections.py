"""把 event_year_corrections 里的年份订正同步到图库。

补年复核（repair_event_years.py --recheck）只在 DuckDB 里改抽取结果与证据，
已经写进 Kuzu 的 Event 节点不会跟着变：年份订正了要改，年份撤掉的要连节点删掉
（它们是当年因为有了年份才被挂载的）。

用法
----
    python scripts/apply_year_corrections.py            # 干跑，只报数
    python scripts/apply_year_corrections.py --apply
"""
from __future__ import annotations

import argparse
import gc
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config                                     # noqa: E402

import duckdb                                              # noqa: E402
import kuzu                                                # noqa: E402

BATCH = 400


def load_corrections() -> list[tuple]:
    con = duckdb.connect(str(config.DATA_DIR / "warehouse"
                             / "chronosino.duckdb"), read_only=True)
    rows = con.execute(
        "SELECT book_id, name, old_year, new_year, how "
        "FROM event_year_corrections").fetchall()
    con.close()
    del con
    gc.collect()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--buffer-pool-mb", type=int, default=256)
    a = ap.parse_args()

    rows = load_corrections()
    upd = [r for r in rows if r[3] is not None]
    dele = [r for r in rows if r[3] is None]
    print(f"订正 {len(rows):,} 条：改年份 {len(upd):,}、撤掉 {len(dele):,}"
          f"{'' if a.apply else '（干跑）'}")

    # 只开 Kuzu，不开 DuckDB：这台机器提交内存只剩一两 GB，同时开两个库
    # 会在 Kuzu 的 VirtualAlloc 阶段段错误退出。
    db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"),
                       buffer_pool_size=a.buffer_pool_mb * 1024 ** 2)
    con = kuzu.Connection(db)

    def count(q: str, p: dict) -> int:
        r = con.execute(q, p)
        return r.get_next()[0] if r.has_next() else 0

    n_upd = n_del = miss = 0
    for i, (bid, name, old, new, how) in enumerate(upd):
        # Kuzu 要求参数与语句一一对应，多传一个就会报 Parameter not found
        p = {"b": bid, "nm": name, "old": old}
        c = count("MATCH (n:Event) WHERE n.from_book=$b AND n.name=$nm "
                  "AND n.year=$old RETURN count(n)", p)
        if not c:
            miss += 1
            continue
        if a.apply:
            con.execute("MATCH (n:Event) WHERE n.from_book=$b AND n.name=$nm "
                        "AND n.year=$old SET n.year=$new",
                        {**p, "new": new})
        n_upd += c
        if (i + 1) % BATCH == 0:
            con = None
            gc.collect()
            con = kuzu.Connection(db)
            print(f"  改年份进度 {i + 1}/{len(upd)}")

    for i, (bid, name, old, _new, how) in enumerate(dele):
        p = {"b": bid, "nm": name, "old": old}
        c = count("MATCH (n:Event) WHERE n.from_book=$b AND n.name=$nm "
                  "AND n.year=$old RETURN count(n)", p)
        if not c:
            miss += 1
            continue
        if a.apply:
            con.execute("MATCH (n:Event) WHERE n.from_book=$b AND n.name=$nm "
                        "AND n.year=$old DETACH DELETE n", p)
        n_del += c
        if (i + 1) % BATCH == 0:
            con = None
            gc.collect()
            con = kuzu.Connection(db)
            print(f"  删除进度 {i + 1}/{len(dele)}")

    print(f"{'✓ 已' if a.apply else '拟'}改年份的节点 {n_upd:,}；"
          f"{'已' if a.apply else '拟'}删除 {n_del:,}；"
          f"图上找不到匹配 {miss:,} 条（多半从未挂载）")
    if a.apply:
        r = con.execute("MATCH (n:Event) RETURN count(n)")
        print(f"自检：图库现有 Event {r.get_next()[0]:,}  "
              f"（{datetime.now():%H:%M:%S}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
