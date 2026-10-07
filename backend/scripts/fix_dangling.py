"""修复主干种子里的悬空边（dangling_edges.json）。

根因：seed.py 的 EDGES 引用了一批当年只写在边里、从未定义实体的人物/事件 id
（liubang / xiangyu / chenshe / linzexu …）。这些人物后来由书籍抽取以「中文名」
作 id 建了节点，边却仍指旧英文 id，于是被 store 判为悬空转存旁车文件。

本脚本做三件事（均可回滚，写库前会导出原始旁车文件）：
  1. 把旧英文 id 重映射到库中已存在的同名节点；
  2. 对库中确实没有的 4 个（林则徐 / 李鸿章 / 张骞通西域 / 郑和下西洋）
     补建最小实体节点（只填可核实字段，不编造叙事）；
  3. 把这些边写入 Kuzu 的 KGREL，并清空旁车文件。

用法：
  python scripts/fix_dangling.py          # 干跑，只报映射与缺失
  python scripts/fix_dangling.py --apply  # 写库
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
DANG = os.path.join(DATA_DIR, "kg", "dangling_edges.json")

# 旧英文 id → 库中已存在的节点 id（以「中文名」作 id 的书籍抽取节点）
REMAP = {
    "chenshe": "陈胜",
    "xiangyu": "项羽",
    "liubang": "刘邦",
    "tangxuanzong": "唐玄宗",
    "mingchengzu": "明成祖",
    "qinhui": "秦桧",
    "huoqubing": "霍去病",
    "weiqing": "卫青",
    "zhouyu": "周瑜",
    "fujian": "苻坚",
    "xiean": "谢安",
    "yangdi": "隋炀",
    "guoziyi": "郭子仪",
    "songhuizong": "宋徽宗",
}

# 库中确实缺失、需要补建的最小节点（只填可核实字段）
NEW_NODES = [
    ("Person", {"id": "linzexu", "name": "林则徐", "years": ["1785", "1850"],
                "dynasty_id": "qing", "home": "福建侯官", "lon": 119.3, "lat": 26.1}),
    ("Person", {"id": "lihongzhang", "name": "李鸿章", "years": ["1823", "1901"],
                "dynasty_id": "qing", "home": "安徽合肥", "lon": 117.2, "lat": 31.8}),
    ("Event", {"id": "zhangqian_ev", "name": "张骞通西域", "year": -138,
               "dynasty_id": "xihan", "lon": 89.2, "lat": 42.9, "place": "西域"}),
    ("Event", {"id": "zhenghe_ev", "name": "郑和下西洋", "year": 1405,
               "dynasty_id": "ming", "lon": 118.1, "lat": 24.5, "place": "福建长乐"}),
]


def label_of(conn, nid: str) -> str | None:
    for lab in ("Person", "Event", "Place", "Dynasty", "Book",
                "Artifact", "Institution", "Concept"):
        r = conn.execute(f"MATCH (n:{lab} {{id:$i}}) RETURN n.id LIMIT 1", {"i": nid})
        if r.has_next():
            return lab
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = kuzu.Connection(kuzu.Database(KG, read_only=not args.apply))
    edges = json.loads(Path(DANG).read_text(encoding="utf-8"))
    print(f"旁车悬空边 {len(edges)} 条")

    # 1) 重映射
    fixed, still = [], []
    for e in edges:
        a = REMAP.get(e["a"], e["a"])
        b = REMAP.get(e["b"], e["b"])
        la, lb = label_of(conn, a), label_of(conn, b)
        if la and lb:
            fixed.append({**e, "a": a, "b": b, "la": la, "lb": lb})
        else:
            still.append((e, a, b, la, lb))
    print(f"重映射后已可解析: {len(fixed)}")
    print(f"仍缺失端点: {len(still)}")
    for e, a, b, la, lb in still:
        print(f"   {e['a']}(->{a},{la}) -{e['rel']}-> {e['b']}(->{b},{lb})")

    if not args.apply:
        print("\n[干跑] 未写库。")
        return 0

    # 回滚点
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = DANG + f".bak_{ts}"
    shutil.copy2(DANG, bak)
    print(f"已备份旁车文件: {bak}")

    # 2) 补建缺失节点
    for lab, props in NEW_NODES:
        if label_of(conn, props["id"]):
            print(f"  已存在，跳过: {props['id']}")
            continue
        cols = ", ".join(f"{k}: ${k}" for k in props)
        conn.execute(f"CREATE (n:{lab} {{{cols}}})", dict(props))
        print(f"  建节点 {lab} {props['id']} {props['name']}")

    # 3) 写边
    ok = 0
    for e in fixed:
        sql = (f"MATCH (a:{e['la']} {{id:$a}}), (b:{e['lb']} {{id:$b}}) "
               "CREATE (a)-[:KGREL {rel:$rel, from_book:$fb, confidence:$c, "
               "src_ids:$s}]->(b)")
        try:
            conn.execute(sql, {"a": e["a"], "b": e["b"], "rel": e["rel"],
                               "fb": e.get("from_book"), "c": e.get("confidence", 0.8),
                               "s": json.dumps(e.get("src_ids", []), ensure_ascii=False)})
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  写边失败 {e['a']}-{e['rel']}->{e['b']}: {exc}")
    print(f"写入 KGREL {ok}/{len(fixed)} 条")

    remain = [e for e, *_ in still]
    Path(DANG).write_text(json.dumps(remain, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    print(f"旁车文件剩余 {len(remain)} 条 -> {DANG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
