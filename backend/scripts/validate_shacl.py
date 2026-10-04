# -*- coding: utf-8 -*-
"""SHACL 体检：校验图库与抽取提案是否符合本体约束。

三种用法：
    python scripts/validate_shacl.py --all               # 全量体检（需停服）
    python scripts/validate_shacl.py --book shiji        # 校验某本书的离线产物
    python scripts/validate_shacl.py --export-shapes     # 只导出 shapes（给外部工具用）

检查项（app/ontology/shapes.py）：
  · 域/值域：关系只能从允许的源类指向允许的目标类（由 REL_PAIRS 自动生成）
  · 悬空边：KGREL 边的端点必须真实存在（有 rdf:type）
  · 名称：实体必须有 name
  · G2：来自书的实体必须带史料引文
  · G5：external_ids 命名空间必须在白名单内
  · provenance：取值必须是 core/seed/book/llm

退出码：0 通过 / 1 有违规 / 2 用法或环境问题
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量体检（**需停服**）")
    ap.add_argument("--book", help="校验某本书的离线产物（data/ingest/<book>.json）")
    ap.add_argument("--export-shapes", help="导出 shapes 到指定 ttl 文件")
    ap.add_argument("--limit", type=int, default=10, help="打印的违规样例数")
    a = ap.parse_args()

    from app.ontology.shapes import build_shapes
    from app.ontology.validate import (data_graph_from_proposals,
                                       data_graph_from_store, summarize,
                                       validate_store)

    if a.export_shapes:
        g = build_shapes()
        g.serialize(a.export_shapes, format="turtle")
        print(f"shapes → {a.export_shapes}（{len(g)} 三元组）")
        return 0

    if a.book:
        p = _BACKEND / "app" / "data" / "ingest" / f"{a.book}.json"
        if not p.exists():
            print(f"没有离线产物：{p}")
            return 2
        from app.ingestion.pipeline import proposals_from_json
        data = json.loads(p.read_text(encoding="utf-8"))
        proposals = proposals_from_json(data)[0]
        from app.ontology.validate import _run
        res = _run(data_graph_from_proposals(proposals), build_shapes())
        print(f"《{a.book}》提案 {len(proposals)} 条 → {summarize(res)}")
        for v in res["violations"][:a.limit]:
            print("  ", json.dumps(v, ensure_ascii=False)[:180])
        return 1 if res["violations"] else 0

    if a.all:
        from app.store import store
        res = validate_store(store)
        print(f"全量体检 → {summarize(res)}")
        for v in res["violations"][:a.limit]:
            print("  ", json.dumps(v, ensure_ascii=False)[:180])
        return 1 if res["violations"] else 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
