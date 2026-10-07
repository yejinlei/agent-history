# -*- coding: utf-8 -*-
"""把已算好的提案直连写进图库（不加载 Store，绕开它的内存墙）。

为什么不能用 store.commit_book
------------------------------
Store 启动要把 13.9 万事件 + 5.3 万人物连同三套叙述文本常驻内存，再走差分落盘；
本机可用提交内存只剩 5 GB 出头，Kuzu 在 VirtualAlloc 阶段就报
`MEM_COMMIT failed ... error 1455`（把 KUZU_BUFFER_POOL_GB 从 6 压到 2 则直接
段错误退出 -1073741819）。同一台机器上，之前的 enrich_event_coords /
enrich_place_nodes / link_event_place 都是直连 Kuzu 写的，几万条没有任何问题。

所以这里沿用那条已被验证的路子：只读提案 + 直连写入 + 数仓留证据。代价是没有
Store 的实时本体闸门，因此脚本自己把关：
  - 只写 document 里还不存在的 id（幂等，跑几次都一样）
  - 每种节点一次 CREATE，写前必查重
  - 事件必须有年份才写（没年份的事件不知道该挂在哪一年）
  - 每条都在数仓 direct_mounts 留痕，按 batch 可整体核账

用法
----
    python scripts/mount_proposals_direct.py songshi            # 干跑
    python scripts/mount_proposals_direct.py songshi --apply    # 写入
    python scripts/mount_proposals_direct.py songshi --apply --limit 3000
    python scripts/mount_proposals_direct.py songshi --set-mounted
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                       # noqa: E402
from app.data.seed import DYNASTIES                          # noqa: E402
from app.ingestion.pipeline import proposals_from_json       # noqa: E402

import duckdb                                                # noqa: E402
import kuzu                                                  # noqa: E402

KUZU_DB = Path(config.DATA_DIR) / "kg" / "kuzu_db"
DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"
INGEST_DIR = Path(config.DATA_DIR) / "ingest"
NOW = datetime.now().isoformat()


def log(msg: str = "") -> None:
    print(msg, flush=True)


def dynasty_at(year: int, fallback: str = "") -> str:
    """按年份归朝，和 store.dynasty_at 同口径（跨朝通史必须按事件年份算）。"""
    if not year:
        return fallback
    for d in DYNASTIES:
        lo, hi = d.span
        if lo <= year <= hi:
            return d.id
    return fallback


def _ids(conn: kuzu.Connection, query: str, params: dict | None = None) -> set[str]:
    q = conn.execute(query, params or {})
    out = set()
    while q.has_next():
        out.add(q.get_next()[0])
    return out


def ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    # op 区分「建节点」与「补边」两种动作，两者幂等键不同：节点可能早就在图里，
    # 只缺一条归属边。
    con.execute("""CREATE TABLE IF NOT EXISTS direct_mounts (
            node_id   VARCHAR, kind VARCHAR, op VARCHAR, name VARCHAR,
            year INTEGER, dynasty_id VARCHAR, book_id VARCHAR,
            batch VARCHAR, ts TIMESTAMP)""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("book_id")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多写多少个节点")
    ap.add_argument("--set-mounted", action="store_true",
                    help="写完后把 Book.mounted 置真（仅在全部写完后用）")
    ap.add_argument("--buffer-pool-mb", type=int, default=256,
                    help="Kuzu 缓冲池；本机提交内存紧张，别调大")
    ap.add_argument("--artifact", default="", help="默认 data/ingest/<book>.json")
    a = ap.parse_args()

    art = Path(a.artifact) if a.artifact else INGEST_DIR / f"{a.book_id}.json"
    if not art.exists():
        log(f"没有离线产物：{art}\n先跑 python scripts/ingest_one.py {a.book_id}"
            f" --finalize-only --max-events 60000 --max-persons 60000 --no-store")
        return 1
    data = json.loads(art.read_text(encoding="utf-8"))
    proposals = proposals_from_json(data)[0]
    log(f"产物提案 {len(proposals):,} 条")

    db = kuzu.Database(str(KUZU_DB),
                       buffer_pool_size=int(a.buffer_pool_mb * 1024 ** 2))
    con = kuzu.Connection(db)

    book = con.execute(
        "MATCH (b:Book {id:$b}) RETURN b.id, b.title, b.level, b.dynasty_id, "
        "b.mounted", {"b": a.book_id})
    if not book.has_next():
        log(f"图库里没有这本书：{a.book_id}")
        return 1
    bid, title, level, bdyn, mounted = book.get_next()
    log(f"《{title}》 level={level} dynasty={bdyn} mounted={mounted}")

    ev_ids = _ids(con, "MATCH (n:Event) RETURN n.id")
    pe_ids = _ids(con, "MATCH (n:Person) RETURN n.id")
    linked = _ids(con,
                  "MATCH (b:Book {id:$b})-[e:KGREL {rel:'书载观点'}]->(n) "
                  "RETURN n.id", {"b": bid})
    log(f"现有 Event {len(ev_ids):,} / Person {len(pe_ids):,}；"
        f"本书已挂 {len(linked):,} 条")

    plan: list[dict] = []
    seen: set[str] = set()
    n_exist = n_noyear = 0
    for ext in proposals:
        kind = ext.kind
        if kind not in ("event", "person"):
            continue
        nid = ext.new_id
        if not nid or nid in seen:
            continue
        seen.add(nid)
        exists = nid in (ev_ids if kind == "event" else pe_ids)
        if exists:
            n_exist += 1                      # 节点已在，只补归属边
        elif kind == "event" and not ext.year:
            n_noyear += 1                     # 没有年份的事件不知道挂哪年，不写
            continue
        plan.append({"kind": kind, "id": nid, "name": ext.name or "",
                     "year": ext.year, "place": ext.place or "",
                     "lon": ext.lon, "lat": ext.lat,
                     "summary": ext.summary or "",
                     "confidence": float(ext.confidence or 0),
                     "years": ext.years or "",
                     "need_node": not exists,
                     "need_edge": nid not in linked})
    n_node = sum(1 for p in plan if p["need_node"])
    n_edgeplan = sum(1 for p in plan if p["need_edge"])
    by_kind: dict[str, int] = {}
    for p in plan:
        if p["need_node"]:
            by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    log(f"\n拟新建节点 {n_node:,}（事件 {by_kind.get('event', 0):,} / "
        f"人物 {by_kind.get('person', 0):,}）")
    log(f"  节点已在图内、只需补归属边 {n_exist:,}；"
        f"无年份的事件跳过 {n_noyear:,}")
    log(f"  合计待补『书载观点』边 {n_edgeplan:,} 条")

    log("\n-- 拟写入样例（前 10）--")
    for p in plan[:10]:
        log("   [%s] %-12s %s  %s" % (p["kind"], p["name"][:12],
                                      p["year"] if p["kind"] == "event" else
                                      (p["years"] or "-"), p["id"][:40]))

    if not a.apply:
        log("\n干跑结束，未写入。确认后加 --apply。")
        return 0

    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    dcon = duckdb.connect(str(DUCKDB_PATH))
    ensure_table(dcon)
    rows = dcon.execute(
        "SELECT node_id, op FROM direct_mounts WHERE book_id=?", [bid]).fetchall()
    done_node = {r[0] for r in rows if r[1] == "node"}
    done_edge = {r[0] for r in rows if r[1] == "edge"}
    todo = [p for p in plan
            if (p["need_node"] and p["id"] not in done_node)
            or (p["need_edge"] and p["id"] not in done_edge)]
    if a.limit:
        todo = todo[:a.limit]
    log(f"\n开始处理 {len(todo):,} 个（证据里已有 {len(done_node):,} 节点 / "
        f"{len(done_edge):,} 条边，幂等跳过）")
    if not todo:
        dcon.close()
        return 0

    # 读阶段的扫描会把页钉在池里，紧接着写就报
    # 「buffer pool is full and no memory could be freed」（实测单独一次写入正常，
    # 扫完十几万 id 再写第一次就炸）。这里丢掉读连接换新连接即可；注意**不能**
    # 二次打开 Database —— 旧对象因循环引用不会立刻析构，文件锁还在，
    # 再开库就报 Could not set lock on file。
    import gc
    con = None
    gc.collect()

    src = json.dumps([{"level": level, "work": f"《{title}》",
                       "translation": None, "confidence": None,
                       "from_book": bid}], ensure_ascii=False)
    n = n_edge = 0
    reopen_every = 1000
    wcon = kuzu.Connection(db)

    def _evidence(node_id: str, op: str, p: dict) -> None:
        dcon.execute(
            "INSERT INTO direct_mounts VALUES (?,?,?,?,?,?,?,?,?)",
            [node_id, p["kind"], op, p["name"], p["year"], bdyn, bid, batch,
             datetime.now()])

    try:
        for p in todo:
            if p["need_node"] and p["id"] not in done_node:
                if p["kind"] == "event":
                    wcon.execute(
                        "CREATE (n:Event {id:$id, name:$name, year:$year, "
                        "month:0, day:0, date_conf:'', dynasty_id:$dyn, "
                        "lon:$lon, lat:$lat, place:$place, actors:'[]', "
                        "n_student:$ns, n_scholar:$nc, n_fan:$nf, "
                        "significance:'', dispute:'', route:'', "
                        "route_label:'', from_book:$bk, book_sources:$bs, "
                        "external_ids:'{}', provenance:'', ca:$ca, ua:$ua, "
                        "version:1})",
                        {"id": p["id"], "name": p["name"],
                         "year": int(p["year"]),
                         "dyn": dynasty_at(int(p["year"]), bdyn or ""),
                         "lon": p["lon"], "lat": p["lat"], "place": p["place"],
                         "ns": p["summary"], "nc": p["summary"],
                         "nf": p["summary"], "bk": bid, "bs": src,
                         "ca": NOW, "ua": NOW})
                else:
                    wcon.execute(
                        "CREATE (n:Person {id:$id, name:$name, years:$years, "
                        "dynasty_id:$dyn, home:$home, lon:$lon, lat:$lat, "
                        "aliases:'', n_student:$ns, n_scholar:$nc, n_fan:$nf, "
                        "from_book:$bk, book_sources:$bs, external_ids:'{}', "
                        "provenance:'', ca:$ca, ua:$ua, version:1})",
                        {"id": p["id"], "name": p["name"], "years": p["years"],
                         "dyn": bdyn or "", "home": "（挂载书源实体）",
                         "lon": p["lon"], "lat": p["lat"],
                         "ns": p["summary"], "nc": p["summary"],
                         "nf": p["summary"], "bk": bid, "bs": src,
                         "ca": NOW, "ua": NOW})
                _evidence(p["id"], "node", p)
                n += 1
            if p["need_edge"] and p["id"] not in done_edge:
                lbl = "Event" if p["kind"] == "event" else "Person"
                wcon.execute(
                    "MATCH (b:Book {id:$b}), (n:" + lbl + " {id:$n}) "
                    "CREATE (b)-[:KGREL {rel:'书载观点', from_book:$b, "
                    "confidence:$c, src_ids:$s}]->(n)",
                    {"b": bid, "n": p["id"], "c": p["confidence"],
                     "s": json.dumps([bid], ensure_ascii=False)})
                _evidence(p["id"], "edge", p)
                n_edge += 1
            if (n + n_edge) % reopen_every == 0:
                # 必须整个进程退出才能真正释放 Kuzu 的文件锁、回收池内页，
                # 所以这里只 checkpoint 数仓；大批次请配合外层循环 + --limit 分轮跑
                dcon.execute("CHECKPOINT")
                log(f"  已写 {n:,} 个节点 / {n_edge:,} 条边")
    except Exception as e:
        log(f"✗ 中断于第 {n} 个：{type(e).__name__}: {e}")
        log("  已写入部分有证据记录，重跑自动跳过")
        dcon.close()
        return 1

    if a.set_mounted:
        wcon.execute("MATCH (b:Book {id:$b}) SET b.mounted = true", {"b": bid})
        log("  已把 Book.mounted 置真")

    chk = kuzu.Connection(db)
    ev = chk.execute("MATCH (n:Event) RETURN count(n)").get_next()[0]
    pe = chk.execute("MATCH (n:Person) RETURN count(n)").get_next()[0]
    bkev = chk.execute(
        "MATCH (n:Event) WHERE n.from_book=$b RETURN count(n)",
        {"b": bid}).get_next()[0]
    log(f"\n✓ 写入完成：新建节点 {n:,}、补边 {n_edge:,}"
        f"（batch={batch}）")
    log(f"自检：全图 Event {ev:,} / Person {pe:,}；本书《{title}》事件 {bkev:,}")
    dcon.close()
    wcon = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
