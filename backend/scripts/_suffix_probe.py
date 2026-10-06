"""试「行政后缀归一化」能救回多少条无坐标事件。

现状：有地名的事件 88,305 条里约 40,062 条对不上 CBDB 词表。其中相当一部分是
书写形态差异——事件写「河东郡 / 河东道」，CBDB 词条叫「河東」；事件写「代」，
CBDB 叫「代州 / 代郡 / 代縣」。这里量化各种后缀玩法的增益，再决定是否落进主流程。
"""
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import kuzu                                               # noqa: E402
from zhconv import convert as zh_convert                  # noqa: E402

DB = str(config.DATA_DIR / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db")
SUFFIXES = ["郡", "縣", "县", "州", "府", "路", "道", "軍", "军", "城", "邑",
            "國", "国", "省", "鎮", "镇", "衛", "卫", "廳", "厅", "州城"]

# CBDB 姓名索引：简体地名 → [(addr_id, 起年, 终年, lon, lat, admin)]
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
index: dict[str, list[tuple]] = defaultdict(list)
for aid, chn, alt, fy, ly, x, y, adm in con.execute(
        "SELECT c_addr_id, c_name_chn, c_alt_names, c_firstyear, c_lastyear, "
        "x_coord, y_coord, c_admin_type FROM ADDR_CODES"):
    if x is None or y is None:
        continue
    try:
        fx, fy_ = float(x), float(y)
    except Exception:
        continue
    if not (73 <= fx <= 136 and 3 <= fy_ <= 54):
        continue
    names = [chn or ""]
    if alt:
        names += [a.strip() for a in str(alt).replace(";", " ").split() if a.strip()]
    rec = (str(aid), fy, ly, fx, fy_, adm)
    for nm in names:
        if nm and not nm.startswith("["):
            index[zh_convert(nm.strip(), "zh-cn")].append(rec)

import duckdb                                             # noqa: E402
DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
duck = duckdb.connect(DUCK, read_only=True)
have = {r[0] for r in duck.execute("SELECT event_id FROM event_coords").fetchall()}
duck.close()

db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)
missed: list[tuple] = []
q = c.execute("MATCH (n:Event) RETURN n.id, n.place, n.year")
while q.has_next():
    eid, place, year = q.get_next()
    place = (place or "").strip()
    if not place or eid in have:
        continue
    key = zh_convert(place, "zh-cn")
    if key in index:
        continue            # 本来就该轮到主流程，不算增量
    missed.append((eid, key, year))
print(f"当前没有坐标、且词表里查不到的事件：{len(missed):,} 条")


def variants(key: str) -> list[str]:
    out = []
    for s in SUFFIXES:
        if key.endswith(s) and len(key) > len(s):
            out.append(key[: -len(s)])            # 去后缀：河东郡 → 河东
        else:
            out.append(key + s)                   # 加后缀：代 → 代州
    return out


gain_events = Counter()
hit_map: dict[str, list] = defaultdict(list)
for eid, key, year in missed:
    for v in variants(key):
        recs = index.get(v)
        if not recs:
            continue
        hit_map[key].append((eid, v, recs))
        break
ev_gain = sum(len(v) for v in hit_map.values())
names_gain = len(hit_map)
print(f"后缀玩法能命中：{ev_gain:,} 条事件 / {names_gain:,} 个地名")

print("\n=== 按事件数排序的收益 top 25（图上写法 → 命中的词条 → 候选数）===")
for k, v in sorted(hit_map.items(), key=lambda kv: -len(kv[1]))[:25]:
    v0 = v[0]
    coords = sorted({(r[3], r[4]) for r in v0[2]})
    print(f"  {k:<10} {len(v):>5} 条  → 《{v0[1]}》 候选 {len(v0[2])} 条，"
          f"坐标 {len(coords)} 种：{coords[:3]}")

print("\n=== 仍然无解的地名 top 20（看看还有什么形态）===")
still = Counter(k for k, *_ in missed if k not in hit_map)
for k, n in still.most_common(20):
    print(f"  {k:<10} {n:>5} 条")
