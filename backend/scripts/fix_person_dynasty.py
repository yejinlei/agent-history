# -*- coding: utf-8 -*-
"""修 Person 的 dynasty_id：从「沿用书的 hint」改成「按生卒年推导」。

问题（PLAN 的 P5 只修了一半）：
    Event 的 dynasty_id 已经按事件年份推导了（实测《资治通鉴》的事件分布在
    tang / beisong / donghan 等多个朝代，正确）；但 **Person 没有年份字段可用**，
    入库时直接沿用了书籍的 dynasty_hint，于是：

      《资治通鉴》16,145 个人物 → 全是 beisong（因为司马光是北宋人）
        但该书涵盖战国到五代，张骞（西汉）、曹操（东汉）、诸葛亮（蜀）都被标成北宋
      《三国志》985 个人物 → 全是 jin（陈寿是晋人）

    实测：Person 68,316 个里，除主干种子外几乎每台书 100% 单一朝代。

修法：
    有生卒年 → 用**生年**（缺失则用卒年）在 Dynasty.span_s..span_e 里定位，
                多个朝代都覆盖时取跨度最小的那个（最具体：三国时期优先命中
                魏/蜀/吴，而不是笼统的「三国」）。
    无生卒年 → 置 NULL。**错误的朝代比「未知」更误导**，宁可空着。

主干种子（无 from_book 的那批）不动：它们的朝代是手工核定的。

**试过但放弃的一条路**：用已锚定 CBDB 的人物的「指数年」补回朝代。
Dynast 表有 84 个朝代、区间大量重叠，按「取跨度最小的匹配」会命中割据
小政权——实测郑和(1418)→beiyuan 北元、宋太祖(927)→houtang 后唐、
岳飞(1103)→beisong、成吉思汗(1162)→xiliao，全是错的。表里也没有
「正统/割据」标记可依（grp 只是分组名）。于是放弃，保持 NULL。

用法：
python scripts/fix_person_dynasty.py            # 干跑
python scripts/fix_person_dynasty.py --apply    # 写库
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
OUT = os.path.join(DATA_DIR, "kg")


def year_of(yrs) -> int | None:
    """从 years（可能是 JSON 字符串、列表、元组）里取出生年。"""
    if yrs is None:
        return None
    if isinstance(yrs, str):
        try:
            yrs = json.loads(yrs)
        except json.JSONDecodeError:
            return None
    if not isinstance(yrs, (list, tuple)):
        return None
    for v in yrs:
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n != 0:          # 0 = 未知（与 Event.year 的约定一致）
            return n
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=200)

    args = ap.parse_args()

    c = kuzu.Connection(kuzu.Database(KG, read_only=not args.apply))

    # 朝代区间（按跨度排序，便于取最具体的）
    r = c.execute("MATCH (d:Dynasty) RETURN d.id, d.name, d.span_s, d.span_e")
    dyns = []
    while r.has_next():
        did, nm, s, e = r.get_next()
        if s is None or e is None:
            continue
        dyns.append((did, nm, int(s), int(e)))
    print(f"Dynasty 带区间的 {len(dyns)} 个")

    def dynasty_for(y: int) -> str | None:
        cands = [d for d in dyns if d[2] <= y <= d[3]]
        if not cands:
            return None
        # 跨度最小 = 最具体
        return min(cands, key=lambda d: d[3] - d[2])[0]

    # 用 from_book 而不是 provenance 判断是不是主干：provenance 在入库时常常
    # 没写（store 的默认值是 'seed'），结果书抽取的人物也全是 'seed'，
    # 按它过滤会把 5 万多个该修的人物全跳过去。
    r = c.execute(
        "MATCH (p:Person) RETURN p.id, p.name, p.years, p.dynasty_id, p.from_book")
    plan, stat = [], Counter()
    while r.has_next():
        pid, nm, yrs, did, fb = r.get_next()
        if not fb:
            stat["skip_seed"] += 1      # 主干：没有 from_book，朝代是手工核定的
            continue
        y = year_of(yrs)
        new = dynasty_for(y) if y is not None else None
        if new == did:
            stat["unchanged"] += 1
            continue
        stat["to_null" if new is None else "to_fix"] += 1
        plan.append({"id": pid, "old": did, "new": new, "name": nm,
                     "year": y})

    print(f"\n待改 {len(plan):,} 个：{dict(stat)}")
    print(f"  → 有年份重推 {stat['to_fix']:,}；无年份置空 {stat['to_null']:,}")
    for p in plan[:12]:
        print(f"   {str(p['name'])[:12]:14s} 生年 {p['year']}  "
              f"{p['old']} → {p['new']}")
    if not args.apply:
        print("\n[干跑] 未写库。")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    undo = os.path.join(OUT, f"fix_person_dynasty_undo_{ts}.jsonl")
    with open(undo, "w", encoding="utf-8") as fh:
        for p in plan:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\n撤销文件: {undo}")

    done = 0
    try:
        for i in range(0, len(plan), args.batch):
            for p in plan[i:i + args.batch]:
                c.execute("MATCH (p:Person {id:$i}) SET p.dynasty_id=$d",
                          {"i": p["id"], "d": p["new"]})
                done += 1
            print(f"  写入 {done:,}/{len(plan):,}", end="\r")
    except Exception as e:  # noqa: BLE001
        print(f"\n失败（已写 {done:,}）：{e}\n可用撤销文件 {undo} 回滚")
        return 1
    print(f"\n完成 {done:,} 个（撤销文件 {undo}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
