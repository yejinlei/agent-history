# -*- coding: utf-8 -*-
"""给已补坐标的事件连上 Place 节点：(Event)-[:发生于]->(Place)。

为什么要这一步
----------------
enrich_place_nodes.py 建了 2,037 个 Place 节点、enrich_event_coords.py 给 1.6 万条
事件补了坐标，但两者之间**没有任何边**——Place 是图上的孤岛（实测 Place 出入边
合计只有 11 条，全是手工种子）。而 `Event.lon/lat` 在 schema 里明确写着「由
(Event)-[:发生于]->(Place) 派生」，缺了这条边，空间层就只是散落的浮点数字，
没法回答「长安发生过哪些事」。

这里把两者连起来，且**不重算任何坐标**：判定完全沿用前两步已经落地的证据表。

place 的两种落点
----------------
1. 新落的 Place 节点（`place:<addr_id>`）：按 evidence 表 place_nodes 里的
   主 addr_id **以及 cbdb_all 别名数组**一起反查——只查主 id 会漏掉 2,498 条
   事件（实测：主 id 只能对上 10,524 条，展开别名数组后是 13,818 条）。
2. 早就存在的手工/种子 Place（拼音 id）：长安→changan、洛阳→luoyang、
   南京→nanjing。这三种各有上千条事件，但派生时被按名字归并掉了，
   所以 place_nodes 里没有它们。
   这里用**名字精确匹配 + 坐标一致性校验（≤0.5°）**兜住，防止同名异地接错。

安全
----
- 默认干跑；`--apply` 才写库，写入按 batch 记录，重跑自动跳过（幂等）
- 不覆盖任何已有边：已存在同 rel 同端点的边直接跳过
- 证据落数仓 `event_place_edges`，回滚脚本依据它删除同名 batch 的边

用法：python scripts/link_event_place.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
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
from zhconv import convert as zh_convert                      # noqa: E402

KUZU_DB = str(config.DATA_DIR / "kg" / "kuzu_db")
DUCKDB_PATH = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
BATCH = datetime.now().strftime("%Y%m%d-%H%M%S")

# 匹配档位 → 边置信度： exemptions 证据越硬，confidence 越高
_CONF = {
    "ok_year_unique": 0.95,
    "ok_year_same_place": 0.90,
    "ok_year_by_rank": 0.80,
    "ok_noyear_unique": 0.70,
}
# 种子 Place 是否可接受：CBDB 坐标与图上坐标的最大偏差（度）
_SEED_MAX_SHIFT = 0.5


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def build_addr_map(con) -> dict[str, tuple[str, str, float, float]]:
    """cbdb_addr_id → (place_id, name, lon, lat)：主 id 与 cbdb_all 别名都算。"""
    m: dict[str, tuple[str, str, float, float]] = {}
    for pid, nm, ext, lon, lat in con.execute(
            "SELECT place_id, name, external_ids, lon, lat "
            "FROM place_nodes").fetchall():
        d = json.loads(ext) if ext else {}
        all_ids = d.get("cbdb_all") or []
        if isinstance(all_ids, str):      # 存量里既有逗号串也有早期写过的列表
            all_ids = [x for x in all_ids.split(",") if x]
        ids = [d.get("cbdb")] + list(all_ids)
        for a in ids:
            if a:
                m[str(a)] = (pid, nm, lon, lat)
    return m


def build_seed_map(c: kuzu.Connection) -> dict[str, tuple[str, float, float]]:
    """已在图上的非派生 Place（手工/种子）按名字索引。"""
    m: dict[str, tuple[str, float, float]] = {}
    q = c.execute("MATCH (n:Place) WHERE NOT n.id STARTS WITH 'place:' "
                  "RETURN n.id, n.name, n.lon, n.lat")
    while q.has_next():
        i, nm, lon, lat = q.get_next()
        if nm:
            m[zh_convert(str(nm).strip(), "zh-cn")] = (i, lon or 0.0, lat or 0.0)
    return m


def existing_links(c: kuzu.Connection) -> set[tuple[str, str]]:
    """已存在的 Event→Place「发生于」边，避免重复写。"""
    out: set[tuple[str, str]] = set()
    q = c.execute("MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
                  "RETURN a.id, b.id")
    while q.has_next():
        a, b = q.get_next()
        out.add((a, b))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多写多少条（调试用）")
    args = ap.parse_args()

    duck = duckdb.connect(DUCKDB_PATH, read_only=True)
    addr_map = build_addr_map(duck)
    ev_rows = duck.execute(
        "SELECT event_id, place, year, lon, lat, cbdb_addr_id, addr_name, how "
        "FROM event_coords ORDER BY ts").fetchall()
    log(f"证据表 event_coords {len(ev_rows):,} 行；Place 别名映射 {len(addr_map):,} 条")

    db = kuzu.Database(KUZU_DB)
    c = kuzu.Connection(db)
    seed_map = build_seed_map(c)
    already = existing_links(c)
    log(f"图上种子 Place {len(seed_map)} 个；已有「发生于」边 {len(already):,} 条")

    # ---------- 干跑：算出每条边连向哪儿 ----------
    plan: list[tuple] = []
    planned: set[tuple[str, str]] = set()
    stat: Counter = Counter()
    unresolved: Counter = Counter()
    seed_shift: Counter = Counter()
    for eid, place, year, lon, lat, aid, aname, how in ev_rows:
        res = addr_map.get(str(aid))
        if res:
            pid, pname, plon, plat = res
            tag = "derived"
        else:
            # 落到既有种子 Place：同名 + 坐标吻合才算数
            hit = seed_map.get(zh_convert(str(place).strip(), "zh-cn"))
            if not hit:
                unresolved[str(place)] += 1
                stat["unresolved"] += 1
                continue
            pid, plon, plat = hit
            shift = max(abs((lon or 0) - plon), abs((lat or 0) - plat))
            if shift > _SEED_MAX_SHIFT:
                unresolved[str(place)] += 1
                stat["seed_too_far"] += 1
                continue
            seed_shift[str(place)] += 1
            tag = "seed"
        if (eid, pid) in already or (eid, pid) in planned:
            stat["skip_existing" if (eid, pid) in already
                 else "skip_dup_in_batch"] += 1
            continue
        planned.add((eid, pid))
        stat[f"link_{tag}"] += 1
        conf = _CONF.get(str(how), 0.7)
        plan.append((eid, pid, place, year, aid, aname, str(how), conf, tag))

    log("--- 干跑结果 ---")
    for k, v in stat.most_common():
        print(f"  {k:<18} {v:>8,}")
    print(f"  拟写入边合计        {len(plan):,}")
    if unresolved:
        print("  未解析地点（前 10）：",
              ", ".join(f"{k}×{v}" for k, v in unresolved.most_common(10)))
    print("\n-- 落到种子 Place 的（逐一确认坐标吻合）--")
    for k, v in seed_shift.most_common():
        print(f"  {k:<10} {v:>6,} 条 → {seed_map.get(zh_convert(k, 'zh-cn'), ('?',))[0]}")

    print("\n-- 拟写入样例（前 12）--")
    for p in plan[:12]:
        print(f"  {p[0]:<24} → {p[1]:<14} [{p[8]}] {p[2]}（{p[3]}）conf={p[7]}")

    if not args.apply:
        log("干跑结束（加 --apply 才写库）")
        duck.close()
        return 0

    # ---------- 写入 ----------
    duck.close()
    duck = duckdb.connect(DUCKDB_PATH)
    duck.execute("""
        CREATE TABLE IF NOT EXISTS event_place_edges (
            event_id VARCHAR, place_id VARCHAR, rel VARCHAR, place VARCHAR,
            year INTEGER, cbdb_addr_id VARCHAR, addr_name VARCHAR,
            how VARCHAR, confidence DOUBLE, via VARCHAR, batch VARCHAR,
            ts TIMESTAMP)
    """)
    written = 0
    wcon = kuzu.Connection(db)
    log(f"开始写入 {len(plan) if not args.limit else min(args.limit, len(plan)):,} 条边")
    for n, (eid, pid, place, year, aid, aname, how, conf, tag) in enumerate(plan, 1):
        if args.limit and n > args.limit:
            break
        try:
            wcon.execute(
                "MATCH (a:Event {id:$a}), (b:Place {id:$b}) "
                "CREATE (a)-[:KGREL {rel:$rel, from_book:NULL, "
                "confidence:$conf, src_ids:$sids}]->(b)",
                {"a": eid, "b": pid, "rel": "发生于", "conf": conf,
                 "sids": json.dumps([], ensure_ascii=False)})
        except Exception as e:
            log(f"  ✗ {eid} → {pid}: {type(e).__name__}: {e}")
            continue
        duck.execute(
            "INSERT INTO event_place_edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [eid, pid, "发生于", place, year, str(aid), aname, how, conf,
             tag, BATCH, datetime.now()])
        written += 1
        if written % 2000 == 0:
            duck.execute("CHECKPOINT")
            # 批量写边会堆脏页（挂载时踩过同样的坑），定期重开连接
            wcon = None
            wcon = kuzu.Connection(db)
            log(f"  已写 {written:,} 条")

    duck.execute("CHECKPOINT")
    duck.close()

    # ---------- 自检 ----------
    n_env = duckdb.connect(DUCKDB_PATH, read_only=True).execute(
        "SELECT count(*) FROM event_place_edges WHERE batch=?", [BATCH]).fetchone()[0]
    log(f"写入完成：{written:,} 条边，batch={BATCH}，证据表 {n_env:,} 行")
    gc = kuzu.Connection(db)
    total = gc.execute("MATCH (a:Event)-[r:KGREL {rel:'发生于'}]->(b:Place) "
                       "RETURN count(*)").get_next()[0]
    log(f"自检：图上 Event→Place「发生于」边合计 {total:,} 条"
        f"（写前已有 {len(already):,} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
