# -*- coding: utf-8 -*-
"""主干权威数据导入器：把 core/normalized/ 的精简表接进系统。

分两路（原理见 docs/GUIDE.md 1.4：按需物化，孤立节点的图价值为零）：
    --load  主干大表 → DuckDB（core_persons / core_places / core_anchors）
            53 万人物放列存：按姓名/朝代/年份秒级检索，不进图库
    --link  只把「已对齐」的实体写进 Kuzu：
            · Wikidata↔CBDB 交叉锚点（两个 id 都有，最可靠）
            · 与已挂载实体同名、且生卒/指数年不冲突的
            写入时 provenance="core"，external_ids 填锚点；
            冲突（同名但年代对不上）只进 needs_review，绝不自动合并。

用法（**必须先停服**，Kuzu/DuckDB 是单写者）：
    python scripts/import_core.py --load
    python scripts/import_core.py --link
    python scripts/import_core.py --load --link     # 一次做完
    python scripts/import_core.py --link --dry-run  # 只看会做什么，不写库
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

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_CORE = _BACKEND / "app" / "data" / "core"
_NORM = _CORE / "normalized"
_REVIEW = _CORE / "needs_review.jsonl"


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def _rows(name: str) -> list[dict]:
    """读分片 JSONL（cbdb_persons_001.jsonl …）。"""
    files = sorted(_NORM.glob(f"{name}*.jsonl"))
    out = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


# ------------------------------------------------------------------ --load
def load() -> int:
    import duckdb
    import glob

    db = glob.glob(str(_BACKEND / "app" / "data" / "warehouse" / "*.duckdb"))[0]
    con = duckdb.connect(db)
    log(f"DuckDB: {db}")

    plan = {
        "core_persons": ("cbdb_persons",
                         "id, name, year_birth, year_death, index_year,"
                         " dynasty, female, external_ids"),
        "core_places": ("chgis_places",
                        "id, name, modern, pinyin, kind, year_beg, year_end,"
                        " external_ids"),
        "core_anchors": ("wikidata_anchors",
                         "id, name, year_birth, year_death, external_ids,"
                         " source"),
    }
    for table, (src, cols) in plan.items():
        files = sorted(_NORM.glob(f"{src}*.jsonl"))
        if not files:
            log(f"{table}: 跳过（无 {src}*.jsonl）")
            continue
        pat = str(_NORM / f"{src}*.jsonl")
        con.execute(f"CREATE OR REPLACE TABLE {table} AS "
                    f"SELECT {cols} FROM read_json_auto('{pat}',"
                    f" format='newline_delimited')")
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log(f"{table}: {n:,} 行（来源 {len(files)} 个分片）")

    # 常用检索路径建索引（DuckDB 用 min-max 索引，这里额外建 ART 索引加速点查）
    for t, c in (("core_persons", "name"), ("core_places", "name"),
                 ("core_anchors", "name")):
        try:
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_{c} ON {t}({c})")
        except Exception as e:
            log(f"  索引 {t}.{c} 跳过：{e.__class__.__name__}")
    con.close()
    return 0


# ------------------------------------------------------------------ --link
YEAR_TOLERANCE = 25          # 生卒比对容差（年）


def _year_ok(a: int | None, b: int | None) -> bool:
    if not a or not b:
        return True           # 缺年份不判定冲突
    return abs(int(a) - int(b)) <= YEAR_TOLERANCE


def link(dry_run: bool = False) -> int:
    from app.schema.ontology import Narrative, Person
    from app.store import store

    anchors = _rows("wikidata_anchors")
    log(f"锚点行：{len(anchors):,}")

    by_name: dict[str, list] = {}
    for p in store.people:
        by_name.setdefault(p.name, []).append(p)

    created, enriched, conflict, skipped = 0, 0, 0, 0
    review: list[dict] = []

    # ---- 阶段 A：用 CBDB 53 万人物给已有实体补锚点（同名 + 生年容差）----
    # Wikidata 中国籍子集多是近现代人物；真正覆盖《史记》人物的是 CBDB。
    log("载入 CBDB 精简表建姓名索引…")
    cbdb_idx: dict[str, list[dict]] = {}
    for r in _rows("cbdb_persons"):
        nm = (r.get("name") or "").strip()
        cb = (r.get("external_ids") or {}).get("cbdb")
        if not nm or not cb:
            continue
        cbdb_idx.setdefault(nm, []).append(
            {"cbdb": cb, "yb": r.get("year_birth"), "yd": r.get("year_death")})
    log(f"CBDB 姓名索引：{len(cbdb_idx):,} 个不同姓名")

    cbdb_hit = 0
    for p in store.people:
        if (p.external_ids or {}).get("cbdb"):
            continue
        cands = cbdb_idx.get(p.name) or []
        if not cands:
            continue
        yb = p.years[0] if p.years else ""
        py = int(yb) if str(yb).lstrip("-").isdigit() else None
        ok = [c for c in cands if _year_ok(py, c["yb"])]
        if len(ok) == 1:
            if not dry_run:
                p.external_ids = dict(p.external_ids or {})
                p.external_ids["cbdb"] = ok[0]["cbdb"]
            cbdb_hit += 1
        elif len(ok) > 1:
            review.append({"reason": "CBDB 同名多候选（需人工选）",
                           "existing": {"id": p.id, "name": p.name,
                                        "years": list(p.years)},
                           "candidates": ok[:5]})
        else:
            review.append({"reason": "CBDB 同名但生年不符",
                           "existing": {"id": p.id, "name": p.name,
                                        "years": list(p.years)},
                           "candidates": cands[:5]})
    log(f"CBDB 补锚点 {cbdb_hit} · 歧义/冲突 {len(review)}")

    # ---- 阶段 B：Wikidata 锚点（补锚点 + 只有交叉锚点才新建主干节点）----
    for a in anchors:
        name = (a.get("name") or "").strip()
        ext = dict(a.get("external_ids") or {})
        if not name or not ext:
            skipped += 1
            continue
        cbdb = ext.get("cbdb")
        wd = ext.get("wikidata")

        hits = by_name.get(name, [])
        if hits:
            # 已存在同名实体：只补锚点，不覆盖任何既有事实（可废止推理）
            for p in hits:
                yb = p.years[0] if p.years else ""
                py = int(yb) if str(yb).lstrip("-").isdigit() else None
                if not _year_ok(py, a.get("year_birth")):
                    conflict += 1
                    review.append({
                        "reason": "同名但生年不符",
                        "existing": {"id": p.id, "name": p.name,
                                     "years": list(p.years),
                                     "provenance": p.provenance},
                        "anchor": {"name": name, "external_ids": ext,
                                   "year_birth": a.get("year_birth")},
                    })
                    continue
                if not dry_run and not p.external_ids:
                    p.external_ids = {k: v for k, v in ext.items() if v}
                    p.updated_at = datetime.now().astimezone().isoformat(
                        timespec="seconds")
                    enriched += 1
            continue

        # 没有同名实体：只有「CBDB 交叉锚点」才新建主干节点
        # （避免把 Wikidata 里成千上万无本地对应的人物灌进图库）
        if not cbdb:
            skipped += 1
            continue
        nid = f"cbdb:{cbdb}"
        if any(p.id == nid for p in store.people):
            skipped += 1
            continue
        if not dry_run:
            born = a.get("year_birth")
            dead = a.get("year_death")
            store.people.append(Person(
                id=nid, name=name,
                years=(str(born) if born else "", str(dead) if dead else ""),
                dynasty_id="", home="",
                lon=None, lat=None, aliases=[],
                narrative=Narrative(
                    student=f"{name}（CBDB {cbdb}）",
                    scholar=f"{name}。主干人物，锚定 CBDB {cbdb}"
                            + (f" / Wikidata {wd}" if wd else "")
                            + "；权威源事实，书籍记载与之冲突时并列待裁决。",
                    fan=f"{name}（来自中国历代人物传记资料库）"),
                external_ids={k: v for k, v in ext.items() if v},
                provenance="core"))
        created += 1

    log(f"新建主干人物 {created} · 补锚点 {enriched} · 冲突待裁决 {conflict}"
        f" · 跳过 {skipped}")

    if review:
        _REVIEW.parent.mkdir(parents=True, exist_ok=True)
        with open(_REVIEW, "w", encoding="utf-8") as f:
            for r in review:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"冲突清单 → {_REVIEW}（{len(review)} 条，需人工裁决，不自动合并）")

    if dry_run:
        log("干跑：未写库")
        return 0

    log("写入图库（全量重写，约 2–3 分钟）…")
    store.save()
    log(f"完成：人物 {len(store.people)}，provenance 分布 "
        f"{Counter(p.provenance for p in store.people).most_common()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--link", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.load or a.link):
        ap.print_help()
        return 0
    if a.load:
        load()
    if a.link:
        link(a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
