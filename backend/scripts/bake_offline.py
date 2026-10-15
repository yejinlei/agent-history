# -*- coding: utf-8 -*-
"""离线演示版烘焙：把 Kuzu 图库的关键子图导出成 JSON 供前端静态托管。

产物（写入 frontend/public/demo/）：
    schema.json           图谱 schema（标签、关系类型、计数）
    hub.json              Top-K 度中心节点的一跳合并邻域
    subgraphs/<id>.json   每个 hub 节点的独立一跳邻域（供点击展开）
    search.json           按标签分组的搜索索引 [{id, label, name, sub}]

用法：
    python scripts/bake_offline.py --hub-limit 30 --edges 1500
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 让脚本直接 `python scripts/bake_offline.py` 也能 import app.*
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.store import store  # noqa: E402


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    size_kb = path.stat().st_size / 1024
    print(f"  -> {path.relative_to(ROOT.parent)}  ({size_kb:.1f} KB)")


def _node_map(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _neighbours_by_source(edges: list[dict]) -> dict[str, list[dict]]:
    """把 edges 按 source 归组，供 subgraphs/<id>.json 使用。"""
    out: dict[str, list[dict]] = {}
    for e in edges:
        out.setdefault(e["source"], []).append(e)
    return out


def _collect_hub_ids(subgraph: dict, limit: int, per_hub_edges: int) -> list[str]:
    """复现 kg_hub 的 hub 排序逻辑（Top-K 按度数），返回 hub 节点 id 列表。"""
    # kg_hub 的返回里没有区分 hub / 邻居，我们从 edges 统计度数取 top-K
    deg: dict[str, int] = {}
    for e in subgraph["edges"]:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    # 度数降序取前 K 个
    return [nid for nid, _ in sorted(deg.items(), key=lambda x: -x[1])[:limit]]


def _one_hop(node_id: str, limit_edges: int) -> dict | None:
    """取单个节点的一跳邻域，返回 GraphExplorer 可吃下的格式。"""
    g = store.kg_ego(node_id, depth=1, limit_edges=limit_edges)
    if g is None:
        return None
    return g


def _search_index(labels: list[str], limit_per_label: int = 300) -> dict[str, list[dict]]:
    """按标签取一批节点作为搜索索引；覆盖不到全量但覆盖搜索常用场景。"""
    out: dict[str, list[dict]] = {}
    for lbl in labels:
        try:
            rows = store.kg_nodes(lbl, "", limit=limit_per_label)
            out[lbl] = rows
        except Exception as e:
            print(f"  ! {lbl}: {e}")
            out[lbl] = []
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "frontend" / "public" / "demo"))
    ap.add_argument("--hub-limit", type=int, default=30)
    ap.add_argument("--edges", type=int, default=1500)
    ap.add_argument("--per-hub-edges", type=int, default=80)
    ap.add_argument("--search-per-label", type=int, default=300)
    ap.add_argument("--subgraph-limit", type=int, default=200,
                    help="每个 hub 节点的一跳子图上限")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"[bake] out_dir={out_dir}")
    print(f"[bake] Kuzu loaded={store.kuzu_loaded if hasattr(store, 'kuzu_loaded') else '?'}")

    # 1) schema
    print("[1/4] schema ...")
    schema = store.kg_schema()
    _dump(out_dir / "schema.json", schema)

    # 2) hub 图（合并邻域）
    print(f"[2/4] hub limit={args.hub_limit} edges={args.edges} ...")
    hub = store.kg_hub(
        limit=args.hub_limit,
        limit_edges=args.edges,
        per_hub_edges=args.per_hub_edges,
    )
    _dump(out_dir / "hub.json", hub)

    # 3) 每个 hub 节点的独立一跳邻域
    print("[3/4] per-hub subgraphs ...")
    hub_ids = _collect_hub_ids(hub, limit=args.hub_limit, per_hub_edges=args.per_hub_edges)
    print(f"  detected hub ids: {hub_ids}")
    sg_dir = out_dir / "subgraphs"
    sg_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for nid in hub_ids:
        g = _one_hop(nid, limit_edges=args.subgraph_limit)
        if g is None:
            print(f"  ! missing {nid}")
            continue
        # 文件名做 URL-safe 转义
        safe = "".join(ch if ch.isalnum() else "_" for ch in nid)
        _dump(sg_dir / f"{safe}.json", g)
        written += 1
    print(f"  wrote {written}/{len(hub_ids)} subgraphs")

    # 4) 搜索索引
    print(f"[4/4] search index (per-label={args.search_per_label}) ...")
    labels = ["Dynasty", "Event", "Person", "Place", "Book",
              "Institution", "Artifact", "Concept"]
    idx = _search_index(labels, limit_per_label=args.search_per_label)
    _dump(out_dir / "search.json", idx)

    # 5) 摘要清单（前端可选加载、便于调试）
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hub_limit": args.hub_limit,
        "edges_cap": args.edges,
        "per_hub_edges": args.per_hub_edges,
        "hub_ids": hub_ids,
        "counts": {
            "schema_tables": len(schema.get("tables", [])),
            "hub_nodes": len(hub["nodes"]),
            "hub_edges": len(hub["edges"]),
            "subgraphs": written,
            "search_per_label": {k: len(v) for k, v in idx.items()},
        },
    }
    _dump(out_dir / "manifest.json", manifest)

    dt = (time.time() - t0) * 1000
    print(f"[bake] done in {dt:.0f} ms")


if __name__ == "__main__":
    main()
