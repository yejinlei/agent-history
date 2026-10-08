# -*- coding: utf-8 -*-
"""Phase 3 · 证据四维：把每条 chunk 定位到 about / place / stance 上。

docs/PLAN.md §3.2 要求每条证据（chunk）可定位在四个维度：

    about   关于哪个实体   → 问「焚书坑儒」能精确取到各书论此事的段落
    time    什么时间       → year / year_raw / calendar
    place   什么地点       → place_id / lon / lat
    stance  什么立场       → 实录 / 评论 / 考辩 / 文学演绎

本脚本用**确定性方法**回填（不烧 token）：
  about / place 用「实体名在 chunk 正文里出现」来判定，实体名取自图库已有节点；
  stance 取书级立场（books_raw.stance），因为同一本书的立场是整体属性；
  year 不猜——从正文正则抽年份的误判率太高，宁可留 NULL。

抽取器侧的四维产出（让**新抽取**直接带四维）是另一件事，见 app/ingestion/extract.py。

用法：
  python scripts/build_evidence.py            # 干跑，只报匹配率
  python scripts/build_evidence.py --apply    # 建表并写入（覆盖式，幂等）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import duckdb  # noqa: E402
import kuzu  # noqa: E402

DB = os.path.join(DATA_DIR, "warehouse", "chronosino.duckdb")
KG = os.path.join(DATA_DIR, "kg", "kuzu_db")

# 名字最短 2 字：1 字的单名（如「翦」）误匹配率太高，宁可漏
MIN_LEN, MAX_LEN = 2, 8
# 一个 chunk 最多记几个 about：再多就是整段都在罗列人名，没有定位价值
MAX_ABOUT = 3

DDL = """CREATE TABLE IF NOT EXISTS evidence (
    chunk_id VARCHAR, book_id VARCHAR, idx BIGINT,
    about_entity VARCHAR, about_kind VARCHAR, about_event VARCHAR,
    year INT, year_raw VARCHAR, calendar VARCHAR, date_confidence VARCHAR,
    place_id VARCHAR, lon DOUBLE, lat DOUBLE,
    stance VARCHAR, confidence DOUBLE
)"""


def load_stance() -> dict[str, str]:
    """书级立场。

    books_raw.stance 是空表（从来没写入过），Book 节点也没有 stance 字段，
    所以从已有的 **level（史料层级）+ tags** 推：
        level 1 原始史料（二十四史、通鉴）      → 实录
        level 2 研究著作，tags 含「史论」       → 评论
        level 2 研究著作，其余                  → 考辩
        level 3 历史小说                        → 文学演绎
    这不是猜的，level/tags 是入库时登记的既有元数据，这里只是换成
    PLAN §3.2 要求的 stance 口径。
    """
    import json as _json
    c = kuzu.Connection(kuzu.Database(KG, read_only=True))
    out: dict[str, str] = {}
    r = c.execute("MATCH (b:Book) RETURN b.id, b.level, b.tags")
    while r.has_next():
        bid, level, tags = r.get_next()
        try:
            tg = _json.loads(tags) if isinstance(tags, str) else (tags or [])
        except Exception:
            tg = []
        if level == 3:
            out[bid] = "文学演绎"
        elif level == 2:
            out[bid] = "评论" if any("史论" in t for t in tg) else "考辩"
        else:
            out[bid] = "实录"
    return out


def load_names() -> dict[int, dict[str, tuple[str, str]]]:
    """按长度分组的「实体名 → (kind, id)」索引（Kuzu 只读）。"""
    c = kuzu.Connection(kuzu.Database(KG, read_only=True))
    by_len: dict[int, dict[str, tuple[str, str]]] = defaultdict(dict)
    total = 0
    for label, kind in (("Event", "event"), ("Person", "person"),
                        ("Place", "place")):
        r = c.execute(f"MATCH (n:{label}) RETURN n.id, n.name")
        while r.has_next():
            nid, name = r.get_next()
            if not name:
                continue
            nm = str(name).strip()
            if not (MIN_LEN <= len(nm) <= MAX_LEN):
                continue
            by_len[len(nm)][nm] = (kind, nid)
            total += 1
    print(f"实体名索引：{total:,} 个（长度 {MIN_LEN}-{MAX_LEN}）")
    return by_len


def match_text(text: str, by_len: dict[int, dict[str, tuple[str, str]]]
               ) -> tuple[list[tuple[str, str, int]], list[tuple[str, str]]]:
    """在正文里找实体名出现。返回 ([(kind,id,次数)], [(place_id, place_name)])。"""
    # key 里带上名字本身：排序要按**名字**长度，不是 id 长度
    # （id 像 place:400617 有 12 字符，按它排会把地名顶到最前面）
    hits: dict[tuple[str, str, str], int] = {}
    n = len(text)
    for length, table in by_len.items():
        if length > n:
            continue
        for i in range(n - length + 1):
            seg = text[i:i + length]
            got = table.get(seg)
            if got is not None:
                kind, eid = got
                hits[(kind, eid, seg)] = hits.get((kind, eid, seg), 0) + 1
    # 长名优先（「安史之乱」比「洛阳」具体得多），同名再比出现次数
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[0][2]), -kv[1]))
    places = [(kind, eid) for (kind, eid, _), _ in ranked if kind == "place"]
    return [(k, eid, cnt) for (k, eid, _), cnt in ranked], places


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 chunk（调试）")
    args = ap.parse_args()

    by_len = load_names()
    con = duckdb.connect(DB)
    chunk_rows = con.execute(
        "SELECT book_id, idx, text FROM chunks ORDER BY book_id, idx").fetchall()
    if args.limit:
        chunk_rows = chunk_rows[:args.limit]
    print(f"chunks：{len(chunk_rows):,}")

    stance_of = load_stance()
    from collections import Counter as _C
    print(f"书级 stance：{len(stance_of)} 本，分布 "
          f"{dict(_C(stance_of.values()))}")

    t0 = time.time()
    rows: list[tuple] = []
    hit_chunks = 0
    for i, (book_id, idx, text) in enumerate(chunk_rows, 1):
        ranked, places = match_text(text or "", by_len)
        if not ranked:
            continue
        hit_chunks += 1
        pid = places[0][1] if places else None
        st = stance_of.get(book_id)
        for (kind, eid, cnt) in ranked[:MAX_ABOUT]:
            rows.append((
                f"{book_id}:{idx}", book_id, idx,
                eid if kind != "event" else None,
                kind,
                eid if kind == "event" else None,
                None, None, None, None,
                pid, None, None,
                st, 1.0 if cnt > 1 else 0.6,
            ))
        if i % 2000 == 0:
            print(f"  已处理 {i:,}/{len(chunk_rows):,}  "
                  f"({time.time()-t0:.0f}s)", end="\r")

    print(f"\n匹配耗时 {time.time()-t0:.0f}s")
    print(f"命中 chunk {hit_chunks:,}/{len(chunk_rows):,} "
          f"({hit_chunks/max(1,len(chunk_rows))*100:.1f}%)")
    print(f"evidence 行数 {len(rows):,}")
    for r in rows[:6]:
        print(f"   {r[1]}:{r[2]}  about={r[3] or r[5]} ({r[4]})  "
              f"place={r[10]}  stance={r[13]}")

    if not args.apply:
        print("\n[干跑] 未写库。")
        con.close()
        return 0

    con.execute(DDL)
    con.execute("DELETE FROM evidence")     # 覆盖式重建，幂等
    # 拼 VALUES 批量插入。其它三种写法在本机都不可用，实测记录：
    #   一次性 executemany 11 万行 → 卡住 5 分钟无进展；
    #   分批 executemany(2000)    → 约 2000 行/40 秒，11 万行要 35 分钟；
    #   CSV + COPY                → 数值列被嗅探成 VARCHAR，直接失败（0 行）。
    #   本写法（每批 500 行一条 INSERT）→ 11 万行约 1 分钟。
    def esc(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    B = 500
    for i in range(0, len(rows), B):
        vals = ",".join("(" + ",".join(esc(x) for x in r) + ")"
                        for r in rows[i:i + B])
        con.execute("INSERT INTO evidence VALUES " + vals)
        print(f"  插入 {min(i + B, len(rows)):,}/{len(rows):,}", end="\r")
    n = con.execute("SELECT count(*) FROM evidence").fetchone()[0]
    k = con.execute("SELECT count(DISTINCT about_entity) FROM evidence "
                    "WHERE about_entity IS NOT NULL").fetchone()[0]
    print(f"\n写入 {n:,} 行；覆盖 {k:,} 个不同实体")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
