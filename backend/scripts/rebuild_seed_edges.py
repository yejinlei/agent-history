# -*- coding: utf-8 -*-
"""重建本体种子边（语义关系）：把 seed.EDGES / ontology_extra 的边接回图库。

背景：图库以 Kuzu 为主存储后，若内存 state 重建时种子边端点用的是「拼音 id」
（如 xiangyu）而库里节点是《史记》挂载产生的「中文名 id」（项羽），
写边时端点检索不到 → 全部判为悬空，KGREL 里只剩挂载产生的「书载观点」边，
图谱失去语义。本脚本把端点按 id → 名称 → 名称分段 逐级解析，
解析成功即写回 KGREL，解析不了的仍留在 dangling_edges.json 旁车文件。

用法（Kuzu 是单写者，**必须先停后端服务**）：
    python scripts/rebuild_seed_edges.py            # 干跑：只统计，不写库
    python scripts/rebuild_seed_edges.py --apply    # 写库（全量重写 Kuzu）
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

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.data import seed                     # noqa: E402
from app.schema.ontology import REL_PAIRS     # noqa: E402
from app.store import store                   # noqa: E402

_TABLES = ("dynasties", "events", "people", "places", "books",
           "institutions", "artifacts", "concepts")
_LABEL = {"dynasties": "Dynasty", "events": "Event", "people": "Person",
          "places": "Place", "books": "Book", "institutions": "Institution",
          "artifacts": "Artifact", "concepts": "Concept"}


def build_indexes():
    """id → label，name → (id, label)。"""
    label_of, name_of = {}, {}
    for attr in _TABLES:
        for o in getattr(store, attr, []):
            label_of[o.id] = _LABEL[attr]
            # Book 用 title，其余用 name
            nm = getattr(o, "name", None) or getattr(o, "title", None)
            if nm:
                name_of.setdefault(nm, (o.id, _LABEL[attr]))
    return label_of, name_of


# 种子边里引用了但在 seed 数据中**未定义**的 id（历史欠账）。
# 左边是边里的 id，右边是它实际指代的中文名；解析时先按中文名找已有节点，
# 找不到再用主干权威数据（CBDB 人物 / CHGIS 地名）补齐，绝不臆造。
_ALIAS = {
    # 人物
    "chenshe": "陈胜", "xiangyu": "项羽", "liubang": "刘邦",
    "huoqubing": "霍去病", "weiqing": "卫青", "zhouyu": "周瑜",
    "fujian": "苻坚", "xiean": "谢安", "yangdi": "隋炀帝",
    "guoziyi": "郭子仪", "tangxuanzong": "唐玄宗",
    "songhuizong": "宋徽宗", "qinhui": "秦桧", "mingchengzu": "明成祖",
    "linzexu": "林则徐", "lihongzhang": "李鸿章", "zhenghe": "郑和",
    "hanwudi": "汉武帝", "yuefei": "岳飞",
    # 事件 / 地点
    "daze": "大泽乡", "zhangqian_ev": "张骞出使", "mobei": "漠北",
    "jingnan_ru": "靖难", "humen_xiaoyan": "虎门销烟",
    "zhenghe_ev": "郑和下西洋", "yuefei_die": "岳飞",
    "mawei": "马嵬", "changan": "长安", "hongmen": "鸿门",
    "dayunhe": "大运河", "feishui": "淝水", "chibi": "赤壁",
    "jingkang": "靖康", "yaopian": "鸦片", "jiawu": "甲午",
}


def build_alias():
    """seed 拼音 id → seed 对象名称（作为连接到中文名节点的桥梁）。"""
    alias = dict(_ALIAS)
    for attr in ("PEOPLE", "EVENTS", "PLACES", "DYNASTIES"):
        for o in getattr(seed, attr, []):
            alias.setdefault(getattr(o, "id", ""), getattr(o, "name", ""))
    return alias


def build_core_index():
    """主干精简表索引：人物名 → CBDB 记录（用于补齐缺失端点）。"""
    idx: dict[str, list[dict]] = {}
    for f in sorted((Path(_BACKEND) / "app" / "data" / "core" / "normalized")
                    .glob("cbdb_persons*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                nm = (r.get("name") or "").strip()
                cb = (r.get("external_ids") or {}).get("cbdb")
                if nm and cb:
                    idx.setdefault(nm, []).append(r)
    return idx


def make_resolver(label_of, name_of, alias, core_idx=None):
    """逐级解析端点：id → 名称 → 名称分段 → 名称包含 → 主干数据补齐。

    core_idx 非空时，人物类端点若库里没有，会从 CBDB 主干新建一个
    provenance=core 的 Person（用的是权威数据，不是臆造）。
    """
    created: list[str] = []

    def resolve(tok: str):
        if tok in label_of:
            return tok, label_of[tok]
        nm = alias.get(tok) or ""
        if nm:
            if nm in name_of:
                return name_of[nm]
            # 名称分段逐个试：「秦始皇 嬴政」→ 先试全称再试「秦始皇」「嬴政」
            for part in nm.replace("·", " ").split():
                if part in name_of:
                    return name_of[part]
            # 名称包含匹配：「漠北」→「漠北之战」、「大泽乡」→「大泽乡起义」
            for k, v in name_of.items():
                if nm in k or (len(k) >= 2 and k in nm):
                    return v
        if tok in name_of:
            return name_of[tok]

        # 主干补齐：用 CBDB 权威人物把缺失端点补成 core 节点
        if core_idx:
            cands = core_idx.get(nm) or core_idx.get(tok) or []
            if len(cands) == 1:
                from app.schema.ontology import Narrative, Person
                c = cands[0]
                cb = (c.get("external_ids") or {}).get("cbdb")
                nid = f"cbdb:{cb}"
                if nid not in label_of:
                    born, dead = c.get("year_birth"), c.get("year_death")
                    store.people.append(Person(
                        id=nid, name=c["name"],
                        years=(str(born) if born else "",
                               str(dead) if dead else ""),
                        dynasty_id="", home="", lon=None, lat=None,
                        aliases=[],
                        narrative=Narrative(
                            student=c["name"],
                            scholar=f"{c['name']}。主干人物（CBDB {cb}），"
                                    "由本体语义边引用而补齐。",
                            fan=c["name"]),
                        external_ids={"cbdb": str(cb)},
                        provenance="core"))
                    label_of[nid] = "Person"
                    name_of.setdefault(c["name"], (nid, "Person"))
                    created.append(nid)
                return nid, "Person"
        return None, None

    return resolve, created


def collect_edges():
    """[(a, b, rel, conf, src_ids)]：seed 常量 + ontology_extra。"""
    out = [(a, b, rel, 0.8, []) for a, b, rel in seed.EDGES]
    p = Path(_BACKEND) / "app" / "data" / "ontology_extra.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        for row in data.get("edges", []):
            out.append((row[0], row[1], row[2],
                        row[3] if len(row) > 3 else 0.8,
                        row[4] if len(row) > 4 else []))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认干跑）")
    ap.add_argument("--no-core", action="store_true",
                    help="不用主干数据补齐缺失端点")
    a = ap.parse_args()

    label_of, name_of = build_indexes()
    core_idx = {} if a.no_core else build_core_index()
    if core_idx:
        print(f"主干人物索引      {len(core_idx):,} 个姓名（用于补齐缺失端点）")
    resolve, created = make_resolver(label_of, name_of, build_alias(), core_idx)
    existing = {(e["a"], e["b"], e["rel"]) for e in store.edges}

    resolved, unresolved, dup, bad_pair = [], [], 0, []
    for x, y, rel, conf, sids in collect_edges():
        ra, la = resolve(x)
        rb, lb = resolve(y)
        if not ra or not rb:
            unresolved.append({"a": x, "b": y, "rel": rel,
                               "miss": "a" if not ra else "b"})
            continue
        if (ra, rb, rel) in existing or (ra, rb, rel) in {
                (r["a"], r["b"], r["rel"]) for r in resolved}:
            dup += 1
            continue
        if (la, lb) not in REL_PAIRS:
            bad_pair.append({"a": ra, "b": rb, "rel": rel,
                             "pair": f"{la}→{lb}"})
            continue
        resolved.append({"a": ra, "b": rb, "rel": rel, "from_book": None,
                         "confidence": conf, "src_ids": sids})

    print(f"种子边总数        {len(collect_edges())}")
    print(f"可解析（将写入）  {len(resolved)}")
    print(f"已存在（跳过）    {dup}")
    print(f"组合不在关系目录  {len(bad_pair)}  {bad_pair[:3]}")
    print(f"主干补齐新建节点  {len(created)}")
    print(f"端点缺失（悬空）  {len(unresolved)}")
    for u in unresolved:
        print("   ", json.dumps(u, ensure_ascii=False))
    print(f"\n写库前 KGREL 边   {sum(1 for e in store.edges if not e.get('_'))}")

    if not a.apply:
        print("\n（干跑：加 --apply 才写库）")
        return 0

    # 只保留挂载产生的边（from_book 非空），种子边整体重放
    kept = [e for e in store.edges if e.get("from_book")]
    store.edges = kept + resolved
    store.save()
    # unresolved 写回旁车，供下次修复参考（save() 只记录本次写不进去的）
    dang = _BACKEND / "app" / "data" / "kg" / "dangling_edges.json"
    dang.write_text(json.dumps(unresolved, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"\n✓ 已写库：保留挂载边 {len(kept)} + 种子语义边 {len(resolved)}"
          f"；悬空 {len(unresolved)} 条写入 {dang.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
