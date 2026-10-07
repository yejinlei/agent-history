"""从籍贯抽取结果里随机抽样，供人工核对精度（黄金集被重复实体打乱了，改用抽样）。"""
import json
import random
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import kuzu                                               # noqa: E402

P = Path(config.DATA_DIR) / "core" / "normalized" / "person_hometown_candidates.jsonl"
n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
rows = [json.loads(l) for l in P.open(encoding="utf-8")]
print(f"候选 {len(rows):,} 条")

db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)
name_of = {}
q = c.execute("MATCH (n:Person) RETURN n.id, n.name")
while q.has_next():
    i, nm = q.get_next()
    name_of[i] = nm

random.seed(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
print(f"\n=== 随机 {n} 条（人物 / 抓到的籍贯短语 / 解到的词条 / 坐标 / 方式）===")
for r in random.sample(rows, min(n, len(rows))):
    nm = name_of.get(r["person_id"], r["person_id"])
    print(f"  {nm:<10} 《{r['place_phrase']}》 → 《{r['addr_name']}》"
          f"{r['lon']:.2f},{r['lat']:.2f}  [{r['how']}] {r['votes']}次")
