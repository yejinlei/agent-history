"""把图谱里的自由措辞谓词归一到受控谓词表（app/ontology/predicates.py）。

干跑先看覆盖率和归得对不对：
    python scripts/normalize_predicates.py
确认后再写库：
    python scripts/normalize_predicates.py --apply

写库做两件事：① 原始谓词与频次备份进 DuckDB 的 predicate_map（可据此还原）；
② 就地改 Kuzu 的 e.rel。改的是原值，因为关系表加不了列（这一版 Kuzu 的
ALTER 不支持），而现有查询都是按 e.rel 过滤的——就地改才能让它们直接受益。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config                                     # noqa: E402
from app.ontology.predicates import CANON_RELS, normalize   # noqa: E402
import duckdb                                              # noqa: E402
import kuzu                                                # noqa: E402

DUCKDB_PATH = config.DATA_DIR / "warehouse" / "chronosino.duckdb"
KUZU_PATH = config.DATA_DIR / "kg" / "kuzu_db"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_predicates(c: kuzu.Connection) -> Counter:
    cnt: Counter = Counter()
    r = c.execute("MATCH ()-[e:KGREL]->() RETURN e.rel, count(*)")
    while r.has_next():
        rel, n = r.get_next()
        cnt[rel or ""] = n
    return cnt


def ensure_map_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS predicate_map (
            rel_raw VARCHAR, rel_norm VARCHAR, n BIGINT,
            batch VARCHAR, ts TIMESTAMP)""")
    # 就地改 rel 是不可逆的：这里把每条边的原始谓词也留一份，便于还原
    con.execute("""CREATE TABLE IF NOT EXISTS kgrel_rel_backup (
            batch VARCHAR, src_id VARCHAR, dst_id VARCHAR, rel_raw VARCHAR)""")


def backup_edges(c: kuzu.Connection,
                 con: duckdb.DuckDBPyConnection, batch: str) -> int:
    """把改之前的每条边（两端 id + 原始 rel）落一份到 DuckDB。

    实测慢得离谱：Kuzu 扫关系表要回表读节点 id，10 分钟才出 2 万条，49 万条
    得跑几个小时。所以默认不做（--full-backup 才做），常规备份用 predicate_map
    就够——它记着每种原谓词归到了哪、各有多少条。
    """
    n = 0
    buf: list[tuple[str, str, str, str]] = []
    r = c.execute("MATCH (a)-[e:KGREL]->(b) RETURN a.id, b.id, e.rel")
    while r.has_next():
        a_id, b_id, rel = r.get_next()
        buf.append((batch, a_id, b_id, rel))
        if len(buf) >= 20000:
            con.executemany("INSERT INTO kgrel_rel_backup VALUES (?,?,?,?)",
                            buf)
            n += len(buf)
            buf.clear()
    if buf:
        con.executemany("INSERT INTO kgrel_rel_backup VALUES (?,?,?,?)", buf)
        n += len(buf)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库")
    ap.add_argument("--min-count", type=int, default=2,
                    help="低于该频次的谓词不动（默认 2，只出现过一次的不值得猜）")
    ap.add_argument("--full-backup", action="store_true",
                    help="额外逐边备份原始谓词（很慢，默认只备份种类级映射）")
    a = ap.parse_args()

    db = kuzu.Database(str(KUZU_PATH), buffer_pool_size=512 * 1024 ** 2)
    c = kuzu.Connection(db)
    cnt = load_predicates(c)
    total = sum(cnt.values())
    log("边总数 %s，谓词 %s 种" % (f"{total:,}", f"{len(cnt):,}"))

    mapping: dict[str, str] = {}          # rel_raw -> rel_norm
    unmapped: Counter = Counter()
    by_canon: dict[str, Counter] = defaultdict(Counter)
    n_canon = n_mapped = n_skip = 0
    for rel, n in cnt.items():
        if rel in CANON_RELS:             # 本来就是受控谓词，不动
            n_canon += n
            by_canon[rel][rel] += n
            continue
        if n < a.min_count:               # 只出现过一次，不值得猜
            n_skip += n
            continue
        norm = normalize(rel)
        if norm is None:
            unmapped[rel] += n
            continue
        mapping[rel] = norm
        n_mapped += n
        by_canon[norm][rel] += n

    log("\n主干（本来就是受控谓词）%s 条（%.1f%%）"
        % (f"{n_canon:,}", 100 * n_canon / total))
    log("长尾归一 %s 条（%.1f%%）；低频未动 %s 条（%.1f%%）；"
        "认不出 %s 条（%.1f%%）"
        % (f"{n_mapped:,}", 100 * n_mapped / total,
           f"{n_skip:,}", 100 * n_skip / total,
           f"{sum(unmapped.values()):,}", 100 * sum(unmapped.values()) / total))
    if unmapped:
        out = Path(r"C:\agent-history-data\unmapped.tsv")
        out.write_text("\n".join("%d\t%s" % (n, r)
                                 for r, n in unmapped.most_common()),
                       encoding="utf-8")
        log("认不出的 %d 种已写到 %s" % (len(unmapped), out))

    log("\n归一后各类规模（前 30）：")
    for canon, words in sorted(by_canon.items(),
                               key=lambda kv: -sum(kv[1].values()))[:30]:
        n = sum(words.values())
        sample = "、".join(w for w, _ in words.most_common(6))
        log("   %-6s %7s  ← %s" % (canon, f"{n:,}", sample))

    if unmapped:
        log("\n认不出的高频词（前 20，可据此补词典）：")
        for rel, n in unmapped.most_common(20):
            log("   %-16s %5d" % (rel, n))

    if not a.apply:
        log("\n干跑结束，未写库。加 --apply 生效。")
        return 0

    # ---- 备份 ----
    con = duckdb.connect(str(DUCKDB_PATH))
    ensure_map_table(con)
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    for rel, norm in mapping.items():
        con.execute("INSERT INTO predicate_map VALUES (?,?,?,?,?)",
                    [rel, norm, cnt[rel], batch, datetime.now()])
    n_edge = backup_edges(c, con, batch) if a.full_backup else 0
    con.close()
    log("\n已备份 %s 条映射到 predicate_map（batch %s%s）"
        % (f"{len(mapping):,}", batch,
           f"，另备份 {n_edge:,} 条边" if n_edge else ""))

    # ---- 就地改 Kuzu ----
    groups: dict[str, list[str]] = defaultdict(list)
    for rel, norm in mapping.items():
        groups[norm].append(rel)
    # 注意：这里是就地改，Kuzu 的关系表在这一版加不了列（ALTER 不支持），
    # 而现有查询都按 e.rel 过滤——就地改才能让它们直接受益。代价是不可逆，
    # 所以先把映射写进 predicate_map。
    # 另外实测：批量 SET 偶发触发 Kuzu 内部断言（csr_node_group.cpp
    # KU_UNREACHABLE），进程直接被拖死。第一批成功过、另一批崩过，看不出规律，
    # 所以批大小取小（100），崩了就停——宁可少归一几条，也不要赌库。
    changed = 0
    for norm, rels in groups.items():
        for i in range(0, len(rels), 100):
            chunk = rels[i:i + 100]
            c.execute(
                "MATCH ()-[e:KGREL]->() WHERE e.rel IN $rels SET e.rel=$norm",
                {"rels": chunk, "norm": norm})
            changed += 1
    log("已改写 %d 批（受控谓词 %d 类、原谓词 %s 种）"
        % (changed, len(groups), f"{len(mapping):,}"))

    after = load_predicates(c)
    log("改写后谓词种类 %s（原 %s）" % (f"{len(after):,}", f"{len(cnt):,}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
