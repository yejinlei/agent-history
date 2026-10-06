# -*- coding: utf-8 -*-
"""把 Place.external_ids 里的列表值改成逗号串——服务因此起不来，必须先修。

背景
----
Place 的 `external_ids` 在图库里是 STRING（存 JSON 文本），而 `Place` 模型声明为
`dict[str, str]`。写节点时把 `cbdb_all` 存成了列表：
    {"cbdb": "15099", "cbdb_all": ["15099", "3520", "12278"], "admin": "Xian"}
服务启动时 store 把它 json.loads 后交给 pydantic，**直接崩在 Place 校验**（不是
Place 节点不重要，是 Store.__init__ 会遍历全部 Place，一个坏就把整个服务拉不起来）。

正确做法不是放宽 schema——那样 store 回写 Kuzu 时又会因为列是 STRING 而炸。
所以：写的人改成逗号串，已经写进去的用本脚本改回来。

用法：python scripts/repair_place_external_ids.py [--apply]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import kuzu                                               # noqa: E402

KUZU = str(config.DATA_DIR / "kg" / "kuzu_db")


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def _parse(raw):
    """图库返回这一列时可能是 dict 也可能是 JSON 文本，两种都吃。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    db = kuzu.Database(KUZU)
    c = kuzu.Connection(db)
    q = c.execute("MATCH (n:Place) RETURN n.id, n.external_ids")
    todo: list[tuple[str, dict]] = []
    total = 0
    while q.has_next():
        pid, raw = q.get_next()
        total += 1
        d = _parse(raw)
        if not d:
            continue
        bad = {k: v for k, v in d.items()
               if isinstance(v, (list, tuple))}
        if bad:
            fixed = dict(d)
            for k, v in bad.items():
                fixed[k] = ",".join(str(x) for x in v)
            todo.append((pid, fixed))
    log(f"Place 节点 {total:,} 个，其中 external_ids 含列表值的 {len(todo):,} 个")
    for pid, d in todo[:5]:
        log(f"  {pid} → {json.dumps(d, ensure_ascii=False)[:100]}")

    if not apply:
        log("干跑结束，未改动。确认后加 --apply。")
        return 0

    n = 0
    for pid, d in todo:
        c.execute("MATCH (n:Place) WHERE n.id = $id "
                  "SET n.external_ids = $v",
                  {"id": pid, "v": json.dumps(d, ensure_ascii=False)})
        n += 1
        if n % 500 == 0:
            log(f"  已修 {n:,} 个")
    log(f"✓ 修复 {n:,} 个节点")

    # 自检：按 Place 模型再验一遍，确保服务能起来
    from app.schema.ontology import Place           # noqa: E402
    bad_n = 0
    q2 = c.execute("MATCH (n:Place) RETURN n.id, n.name, n.ancient, n.modern, "
                   "n.lon, n.lat, n.aliases, n.external_ids, n.provenance")
    while q2.has_next():
        row = q2.get_next()
        data = dict(zip(("id", "name", "ancient", "modern", "lon", "lat",
                         "aliases", "external_ids", "provenance"), row))
        # 与 store._load_kuzu 完全同口径：缺值用默认空容器兜底
        blanks = {"aliases": "[]", "external_ids": "{}", "provenance": "{}"}
        try:
            Place(id=data["id"], name=data["name"], ancient=data["ancient"],
                  modern=data["modern"], lon=data["lon"], lat=data["lat"],
                  aliases=json.loads(data["aliases"] or "[]"),
                  external_ids=json.loads(data["external_ids"] or "{}"),
                  provenance=json.loads(data["provenance"] or "{}")
                  if isinstance(data["provenance"], str) else
                  (data["provenance"] or {}),
                  created_at="", updated_at="", version=1)
        except Exception as e:
            if bad_n < 3:
                log(f"  ✗ {data.get('id')}: {str(e)[:120]}")
            bad_n += 1
    log(f"自检：按 Place 模型校验 {total:,} 个节点，失败 {bad_n} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
