"""按地名维度（而不是按事件）列出放宽档候选——同一个坏地名会成串重复，
抽样估不准真实错误率，必须逐名字看一遍。

用法：python scripts/_name_report.py <候选文件> [最小 gap]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import kuzu                                               # noqa: E402

BASE = Path(config.DATA_DIR) / "core" / "normalized"
fname = sys.argv[1] if len(sys.argv) > 1 else "event_coord_candidates_relaxed200.jsonl"
mingap = int(sys.argv[2]) if len(sys.argv) > 2 else 0
PREFIX = sys.argv[3] if len(sys.argv) > 3 else "relaxed_"

rows = [json.loads(l) for l in (BASE / fname).open(encoding="utf-8")]
sel = [r for r in rows if r["how"].startswith(PREFIX)]
if mingap:
    sel = [r for r in sel
           if max(0, r["addr_from"] - r["year"] if r["year"] < r["addr_from"]
                  else r["year"] - r["addr_to"]) >= mingap]

print(f"{fname}：放宽档事件 {len(sel):,} 条，涉及不同地名 "
      f"{len({r['place'] for r in sel}):,} 个")

db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)
name_of = {}
q = c.execute("MATCH (n:Event) RETURN n.id, n.name")
while q.has_next():
    i, nm = q.get_next()
    name_of[i] = nm

groups = defaultdict(list)
for r in sel:
    groups[r["place"]].append(r)

print("\n=== 逐个地名（按涉及事件数排序）===")
for place, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    r0 = rs[0]
    ys = sorted({x["year"] for x in rs})
    ex = name_of.get(rs[0]["event_id"], "?")
    print(f"  {place:<8} {len(rs):>4}条  → {r0['lon']:>9.3f},{r0['lat']:<8.3f} "
          f"词条《{r0['addr_name']}》{r0['addr_from']}~{r0['addr_to']} "
          f"{r0['addr_admin']:<8} 事件年 {ys[0]}~{ys[-1]}  例：{ex[:16]}")
