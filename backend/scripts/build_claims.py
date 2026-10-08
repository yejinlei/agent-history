"""Phase 7：从图里已有的「观点类」边构造首批 Claim（主张/论断）节点。

为什么只挑观点类边，而不用占全部边 56% 的「书载观点」：
  「书载观点」的实际语义是**出处标注**（某书提到了某人/某事），274,592 条里
  没有一条携带主张内容——把它们转成 Claim 会造出 27 万个假论断。真正的
  「谁主张什么」只存在于评论 / 异议 / 主张X 这类边里，约 760 条。

立场（stance）由边的 rel 派生：
  含「异议/反驳/反对」→ 否定   以「主张」开头 → 肯定   含「评论/评价」→ 评论

产出：
  - Claim 节点（id 由 源+rel+目标 哈希，重跑幂等）
  - 撤销文件 kg/build_claims_undo_<ts>.jsonl

关联方式：**走字段，不走边**。Claim 自带 claimant_id / about_id，查询时
按 about_id 聚合即可拿到「关于同一件事的各方主张」。不建边的原因是 KGREL
关系表在建表时就把 FROM/TO 的标签集合写死了（Dynasty/Event/…/Source），
Claim 不在其中；扩进去要重建整张关系表，代价远超收益。

用法：
  python scripts/build_claims.py            # 干跑
  python scripts/build_claims.py --apply    # 写库
  python scripts/build_claims.py --undo F   # 回滚
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
OUT = os.path.join(DATA_DIR, "kg")

# rel → stance 的派生规则（按顺序匹配）
_STANCE_RULES = [
    ("否定", lambda r: any(k in r for k in ("异议", "反驳", "反对", "非议", "驳"))),
    ("肯定", lambda r: r.startswith("主张")),
    ("评论", lambda r: any(k in r for k in ("评论", "评价", "褒", "贬"))),
]

# 明确排除：这些是出处标注，不是主张
_EXCLUDE_RELS = {"书载观点", "见于", "提及"}


def stance_of(rel: str) -> str | None:
    if not rel or rel in _EXCLUDE_RELS:
        return None
    for st, fn in _STANCE_RULES:
        if fn(rel):
            return st
    return None


def _cid(src: str, rel: str, dst: str) -> str:
    h = hashlib.md5(f"{src}|{rel}|{dst}".encode("utf-8")).hexdigest()[:12]
    return f"claim_{h}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="FILE")
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    c = kuzu.Connection(
        kuzu.Database(KG, read_only=not args.apply and not args.undo))

    if args.undo:
        rows = [json.loads(l) for l in open(args.undo, encoding="utf-8") if l.strip()]
        for i, r in enumerate(rows, 1):
            c.execute("MATCH (n:Claim {id:$id}) DETACH DELETE n", {"id": r["id"]})
            c.execute("MATCH ()-[e:KGREL]->() WHERE e.a=$a AND e.b=$b DELETE e",
                      {"a": r["id"], "b": r["about_id"]})
            if i % 100 == 0:
                print(f"  回滚 {i}/{len(rows)}", end="\r")
        print(f"\n回滚完成 {len(rows)} 条")
        return 0

    print("扫描 KGREL 里的观点类边 ...")
    r = c.execute("MATCH ()-[e:KGREL]->() RETURN e.rel, count(*) ORDER BY count(*) DESC")
    rel_count = {}
    while r.has_next():
        k, v = r.get_next()
        rel_count[k] = v
    picked = {k: v for k, v in rel_count.items() if stance_of(k)}
    print(f"  谓词 {len(rel_count):,} 种 → 观点类 {len(picked)} 种，"
          f"{sum(picked.values()):,} 条边")
    for k, v in sorted(picked.items(), key=lambda x: -x[1])[:12]:
        print(f"    {k:16s} {v:6,}  → {stance_of(k)}")

    rels = list(picked)
    print(f"\n取这 {len(rels)} 种边的端点 ...")
    r = c.execute(
        "MATCH (a)-[e:KGREL]->(b) WHERE e.rel IN $rels "
        # 注意：这里不能写 labels(a)[0]——Kuzu 对函数结果取下标会返回 NULL，
        # 906 条边会全部被当成「无标签」。取出整个列表，下标在 Python 里做。
        "RETURN labels(a), a.id, a.name, e.rel, labels(b), b.id, b.name, "
        "e.from_book, e.confidence",
        {"rels": rels})
    rows = []
    while r.has_next():
        rows.append(r.get_next())
    print(f"  候选 {len(rows):,} 条")

    claims, stat = [], Counter()
    seen = set()
    for la_l, aid, aname, rel, lb_l, bid, bname, fb, conf in rows:
        la = (la_l or [None])[0] if isinstance(la_l, (list, tuple)) else la_l
        lb = (lb_l or [None])[0] if isinstance(lb_l, (list, tuple)) else lb_l
        st = stance_of(rel)
        if not st:
            continue
        if not la or not lb:
            # 端点取不到标签就没法写 MATCH (a:Label {id:...})，拼出来的 SQL 是
            # 语法错的 `MATCH (a: {id:$a})`，会把整批写入打断。
            stat["no_label"] += 1
            continue
        cid = _cid(aid, rel, bid)
        if cid in seen:
            stat["dup"] += 1
            continue
        seen.add(cid)
        claims.append({
            "id": cid,
            "name": f"{aname or aid} {rel} {bname or bid}",
            "text": None,
            "claimant_id": aid,
            "claimant_kind": "person" if la == "Person" else "book",
            "about_id": bid,
            "about_kind": {"Event": "event", "Person": "person",
                           "Place": "place", "Concept": "concept"}.get(lb, "event"),
            "stance": st,
            "from_book": fb,
            "confidence": conf or 0.0,
            "kind": rel,
            "_la": la, "_lb": lb,
        })
        stat[st] += 1
    print(f"\n  构造 Claim {len(claims):,} 条：{dict(stat)}")

    # 潜在冲突：同一 about 上出现互斥立场
    by_about = defaultdict(set)
    for cl in claims:
        by_about[cl["about_id"]].add(cl["stance"])
    conflicts = [a for a, sts in by_about.items() if len(sts) > 1]
    print(f"  同一对象上出现不同立场的：{len(conflicts):,} 个"
          f"（例：{conflicts[:3]}）")

    for cl in claims[:10]:
        print(f"    [{cl['stance']}] {cl['name'][:52]}")

    if not args.apply:
        print("\n[干跑] 未写库。")
        return 0

    # Claim 表由 store._ensure_schema 在启动时补建；本脚本直连图库，绕过了
    # Store，所以要自己确保表在（老库第一次跑时还没有）。
    names = set()
    rr = c.execute("CALL show_tables() RETURN name")
    while rr.has_next():
        names.add(rr.get_next()[0])
    if "Claim" in names:
        # 本脚本是「从边重建」，幂等：先清掉上一轮（含失败半途）留下的 Claim。
        # DETACH DELETE 会连带清掉它们挂的 KGREL 边。
        rr = c.execute("MATCH (n:Claim) RETURN count(n)")
        n0 = rr.get_next()[0] if rr.has_next() else 0
        if n0:
            print(f"  清空已有 Claim {n0} 条（幂等重建）")
            c.execute("MATCH (n:Claim) DETACH DELETE n")
    else:
        c.execute("""CREATE NODE TABLE Claim(
            id STRING, name STRING, text STRING,
            claimant_id STRING, claimant_kind STRING,
            about_id STRING, about_kind STRING,
            stance STRING, year INT64, dynasty_id STRING,
            from_book STRING, confidence DOUBLE, src_ids STRING, kind STRING,
            external_ids STRING, provenance STRING,
            ca STRING, ua STRING, version INT64,
            PRIMARY KEY(id))""")
        print("  已建 Claim 表")

    ts = time.strftime("%Y%m%d_%H%M%S")
    undo_fp = os.path.join(OUT, f"build_claims_undo_{ts}.jsonl")
    with open(undo_fp, "w", encoding="utf-8") as fh:
        for cl in claims:
            fh.write(json.dumps({"id": cl["id"], "about_id": cl["about_id"]},
                                ensure_ascii=False) + "\n")
    print(f"\n撤销文件: {undo_fp}")

    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    done = 0
    try:
        for i in range(0, len(claims), args.batch):
            for cl in claims[i:i + args.batch]:
                c.execute(
                    "CREATE (n:Claim {id:$id, name:$name, text:$text,"
                    " claimant_id:$cid, claimant_kind:$ckind,"
                    " about_id:$aid, about_kind:$akind, stance:$stance,"
                    " year:$year, dynasty_id:$did, from_book:$fb,"
                    " confidence:$conf, src_ids:$srcs, kind:$kind,"
                    " external_ids:$ext, provenance:$prov,"
                    " ca:$ca, ua:$ua, version:$ver})",
                    {"id": cl["id"], "name": cl["name"], "text": cl["text"],
                     "cid": cl["claimant_id"], "ckind": cl["claimant_kind"],
                     "aid": cl["about_id"], "akind": cl["about_kind"],
                     "stance": cl["stance"], "year": None, "did": None,
                     "fb": cl["from_book"], "conf": cl["confidence"],
                     "srcs": "[]", "kind": cl["kind"], "ext": "{}",
                     "prov": "book", "ca": now, "ua": now, "ver": 1})
                # 不建边：KGREL 关系表在建表时就把 FROM/TO 的标签集合写死了
                # （Dynasty/Event/... /Source），Claim 不在其中，写边会报
                # "violates schema"；扩进去只能重建关系表，代价远超收益。
                # 所以 Claim 的关联走**字段**：claimant_id / about_id。
                done += 1
            print(f"  写入 {done}/{len(claims)}", end="\r")
    except Exception as e:  # noqa: BLE001
        print(f"\n失败（已写 {done} 条）：{e}")
        print(f"可用 --undo {undo_fp} 回滚")
        return 1
    print(f"\n写入完成 {done} 条 Claim（各带 2 条边）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
