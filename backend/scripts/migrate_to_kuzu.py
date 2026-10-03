# -*- coding: utf-8 -*-
"""一次性迁移：旧 JSON 本体 / seed → Kuzu 图库（backend/app/data/kg/kuzu_db）

用法：
    python backend/scripts/migrate_to_kuzu.py          # 幂等：库已存在则仅校验
    python backend/scripts/migrate_to_kuzu.py --force  # 删库重建（从 JSON/seed 重迁）

校验基准：84 政权 / 793 帝王 / 61 事件 / 13 人物 / 9 地点 / 67 边
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_KUZU_DB = BACKEND / "app" / "data" / "kg" / "kuzu_db"

if "--force" in sys.argv and _KUZU_DB.exists():
    if _KUZU_DB.is_dir():
        shutil.rmtree(_KUZU_DB)
    else:
        _KUZU_DB.unlink()
    print(f"[force] 已删除 {_KUZU_DB}")

from app.store import store  # noqa: E402  （导入即触发迁移/加载）

# ---------------- 数量校验 ----------------
n_dyn = len(store.dynasties)
n_emp = sum(len(d.emperors) for d in store.dynasties)
n_evt = len(store.events)
n_per = len(store.people)
n_pla = len(store.places)
n_edge = len(store.edges)
n_src = sum(len(v) for v in store.sources.values())
n_book = len(store.books)

print(f"Dynasty {n_dyn} | 帝王 {n_emp} | Event {n_evt} | Person {n_per} "
      f"| Place {n_pla} | Source 组 {len(store.sources)}（{n_src} 条）"
      f"| Book {n_book} | Edge {n_edge}")

expect = {"Dynasty": 84, "帝王": 793, "Event": 61, "Person": 13,
          "Place": 9, "Edge": 67}
actual = {"Dynasty": n_dyn, "帝王": n_emp, "Event": n_evt,
          "Person": n_per, "Place": n_pla, "Edge": n_edge}
bad = {k: (actual[k], v) for k, v in expect.items() if actual[k] != v}
print("基准校验:", "全部一致" if not bad else f"不一致 {bad}")

# ---------------- Kuzu 侧抽查 ----------------
r = store._x("MATCH (d:Dynasty) RETURN count(d) AS c")
print("Kuzu Dynasty 节点:", r.get_next()[0])
r = store._x("MATCH ()-[e:KGREL]->() RETURN count(e) AS c")
resolved = r.get_next()[0]
print(f"Kuzu KGREL 边: {resolved}（另有 {n_edge - resolved} 条悬空边"
      f"存 dangling_edges.json，端点未建节点）")

# 邻接抽查：edges_of 行为（含悬空边）
sample = store.events[0].id
mem = store.edges_of(sample)
print(f"邻接抽查 {sample}: {len(mem)} 条")

# 查询直出：唐朝起止
r = store._x("MATCH (d:Dynasty {id:'tang'}) RETURN d.name, d.span_s, d.span_e")
row = r.get_next()
print("抽查 tang:", row)
print("迁移完成 →", _KUZU_DB)
