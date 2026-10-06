"""干跑：CBDB 行政地名（ADDR_CODES）能否给图里的事件配上坐标（只读）。

CHGIS 已被证伪（只有 1820/1911/1990 三个切片，缺先秦汉唐地名）。
CBDB 的 ADDR_CODES / ADDRESSES 带 c_firstyear / c_lastyear 年代区间 +
c_alt_names 别名 + 层级归属，时代与 26 史对口。这里验证三件事：

1. 年代覆盖：能不能盖住图上从西周到清的事件年份
2. 关键古地名：益州 / 邺 / 朔方 / 云中 / 蓟 / 晋阳 / 建康 到底有没有
3. 真刀真枪：图里 16,909 个 place 按「同名 + 事件年份落在地名年代区间」匹配，
   覆盖率与歧义率是多少
"""
from __future__ import annotations

import re
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
from app import config

import kuzu

DB = Path(r"C:\agent-history-data\core\raw\cbdb\CBDB_20240208_sqlite.db")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

print("=" * 74)
print("1. CBDB 地名字典")
print("=" * 74)
cols = [c[1] for c in con.execute("PRAGMA table_info(ADDR_CODES)")]
print("ADDR_CODES 列:", cols)
n_addr = con.execute("SELECT count(*) FROM ADDR_CODES").fetchone()[0]
n_xy = con.execute(
    "SELECT count(*) FROM ADDR_CODES WHERE x_coord IS NOT NULL AND "
    "trim(x_coord) <> '' AND y_coord IS NOT NULL AND trim(y_coord) <> ''").fetchone()[0]
n_alt = con.execute(
    "SELECT count(*) FROM ADDR_CODES WHERE c_alt_names IS NOT NULL AND "
    "trim(c_alt_names) <> ''").fetchone()[0]
n_yr = con.execute(
    "SELECT count(*) FROM ADDR_CODES WHERE c_firstyear IS NOT NULL AND "
    "c_lastyear IS NOT NULL").fetchone()[0]
print(f"行数 {n_addr:,}；有坐标 {n_xy:,}（{n_xy / n_addr * 100:.1f}%）；"
      f"有别名 {n_alt:,}（{n_alt / n_addr * 100:.1f}%）；有起止年 {n_yr:,}")

mn, mx = con.execute(
    "SELECT min(c_firstyear), max(c_lastyear) FROM ADDR_CODES "
    "WHERE c_firstyear IS NOT NULL").fetchone()
print(f"年代跨度：最早 {mn} → 最晚 {mx}")

print("\n-- 行政类型分布（前 10）--")
for r in con.execute(
        "SELECT c_admin_type, count(*) n FROM ADDR_CODES "
        "GROUP BY 1 ORDER BY n DESC LIMIT 10"):
    print(f"  {str(r[0]):<14} {r[1]:,}")

print("\n-- 别名格式样例 --")
for r in con.execute(
        "SELECT c_name_chn, c_alt_names FROM ADDR_CODES "
        "WHERE c_alt_names IS NOT NULL AND trim(c_alt_names) <> '' LIMIT 5"):
    print(f"  {r[0]}  →  {r[1][:90]}")

# ---------- 建索引：名字 → [(起年, 止年, 经, 纬, 类型, id)] ----------
def ok_xy(x, y) -> bool:
    try:
        return 73 <= float(x) <= 136 and 3 <= float(y) <= 54
    except Exception:
        return False


index: dict[str, list[tuple]] = defaultdict(list)
from zhconv import convert as zh_convert      # 繁→简，纯 Python 实现

XY_FROM = {"own": 0, "addresses": 0}
# ADDR_CODES 自身没坐标时，用 ADDRESSES 表的坐标兜
addr_xy: dict[str, tuple] = {}
for r in con.execute(
        "SELECT c_addr_id, x_coord, y_coord FROM ADDRESSES "
        "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
    addr_xy[str(r[0])] = (r[1], r[2])

for r in con.execute(
        "SELECT c_addr_id, c_name, c_name_chn, c_firstyear, c_lastyear, "
        "c_admin_type, x_coord, y_coord, c_alt_names FROM ADDR_CODES"):
    aid, py, cn, fy, ly, admin, x, y, alt = r
    if cn and cn.startswith("["):        # [Unknown]/[Missing Data] 占位
        continue
    if not x or not y:
        axy = addr_xy.get(str(aid))
        if axy:
            x, y = axy
            XY_FROM["addresses"] += 1
    if not (x and y) or not ok_xy(x, y):
        continue
    rec = (int(fy) if str(fy).lstrip("-").isdigit() else None,
           int(ly) if str(ly).lstrip("-").isdigit() else None,
           round(float(x), 5), round(float(y), 5), admin or "", str(aid))
    names = {cn.strip()} if cn else set()
    if py and py.strip():
        names.add(py.strip())
    if alt:
        for piece in re.split(r"[;,/、，]", alt):
            piece = piece.strip()
            if piece and not piece.startswith("["):
                names.add(piece)
                if piece != alt.strip():
                    pass
    for nm in names:
        if nm:
            # CBDB 的地名几乎全是繁体（長安 / 洛陽 / 京師），图里的 place 是简体；
            # 两边统一到简体，同名才对得上——之前长安/洛阳空手而归就是栽在这。
            index[zh_convert(nm, "zh-cn")].append(rec)
print(f"\n名字索引 {len(index):,} 个不同名字；"
      f"坐标来源：自身 {n_xy - XY_FROM['addresses']:,} + "
      f"ADDRESSES 补充 {XY_FROM['addresses']:,}")

print()
print("=" * 74)
print("2. CHGIS 里没有的那些古地名，CBDB 有没有")
print("=" * 74)
for w in ["益州", "邺", "朔方", "云中", "蓟", "晋阳", "建康", "长安",
          "洛阳", "成都", "京师", "开封", "大都", "中都", "幽州"]:
    cands = index.get(w, [])
    if not cands:
        print(f"  {w:<5} 无")
        continue
    fys = sorted({str(c[0]) for c in cands})
    print(f"  {w:<5} {len(cands):>4} 条；"
          f"坐标样例 {cands[0][2]}, {cands[0][3]}；起年 {fys[:7]}")

print()
print("=" * 74)
print("3. 真刀真枪：图里事件的 place + year 匹配")
print("=" * 74)
db = kuzu.Database(str(config.DATA_DIR / "kg" / "kuzu_db"), read_only=True)
c = kuzu.Connection(db)
names = Counter()
years: dict[str, list[int]] = defaultdict(list)
q = c.execute("MATCH (n:Event) RETURN n.place, n.year")
while q.has_next():
    p, y = q.get_next()
    p = (p or "").strip()
    if not p:
        continue
    names[p] += 1
    if isinstance(y, int):
        years[p].append(y)
ev_total = sum(names.values())
print(f"有 place 的事件 {ev_total:,}；不同 place {len(names):,}")


def match(place: str, ys: list[int]):
    """(档位, 条目, 候选数)：只认「同名 + 年代落在区间」这类强证据。"""
    cands = index.get(zh_convert(place, "zh-cn"), [])
    if not cands:
        return "miss", None, 0
    if len(cands) == 1:
        return ("L1_unique", cands[0], 1) if True else ("x", None, 0)
    # 年代窗口过滤
    if ys:
        inwin = []
        for rec in cands:
            fy, ly = rec[0], rec[1]
            if fy is None or ly is None:
                continue
            # 事件年份只要有一个落在区间，就算这条地名适用
            if any(fy <= y <= ly for y in ys):
                inwin.append(rec)
        if inwin:
            lons = [r[2] for r in inwin]
            lats = [r[3] for r in inwin]
            span = max(max(lons) - min(lons), max(lats) - min(lats))
            # 年代命中多条时，坐标若几乎重合（≤0.5°，约 50 km）其实是同一个地方
            # 的不同朝代条目；跨得远才是真歧义（如「益州」被标到 125°E 吉林）
            if span <= 0.5:
                return "L3a_same_place", inwin[0], len(inwin)
            return "L3b_real_ambig", None, len(inwin)
        return "L4_out_of_window", None, len(cands)
    return "L4_no_year", None, len(cands)


stat = Counter()
ev_stat = Counter()
samples = defaultdict(list)
for place, cnt in names.items():
    lv, rec, nc = match(place, years.get(place, []))
    stat[lv] += 1
    ev_stat[lv] += cnt
    if len(samples[lv]) < 6:
        ys = years.get(place, [])
        samples[lv].append((place, cnt, nc, min(ys) if ys else None,
                            max(ys) if ys else None,
                            f"{rec[2]},{rec[3]}" if rec else "-"))

print(f"\n{'档位':<18}{'地名数':>10}{'占比':>8}{'覆盖事件':>10}{'占比':>8}")
for k in ("L1_unique", "L2_year", "L3a_same_place", "L3b_real_ambig",
          "L4_out_of_window", "L4_no_year", "miss"):
    if stat[k]:
        print(f"{k:<18}{stat[k]:>10,}{stat[k] / sum(stat.values()) * 100:>7.1f}%"
              f"{ev_stat[k]:>10,}{ev_stat[k] / ev_total * 100:>7.1f}%")

safe = ev_stat["L1_unique"] + ev_stat["L2_year"] + ev_stat["L3a_same_place"]
print(f"\n>> 可安全采用的事件数：{safe:,} / {ev_total:,}"
      f"（{safe / ev_total * 100:.1f}%）"
      f"   真歧义弃用：{ev_stat['L3b_real_ambig']:,}"
      f"   年代外弃用：{ev_stat['L4_out_of_window']:,}")

print()
print("=" * 74)
print("4. 逐事件匹配（每个事件用自己的年份挑候选，而非地名整体的年份并集）")
print("=" * 74)


def match_event(place: str, year: int | None):
    """(档位, 条目)：只认强证据，宁缺毋滥。"""
    cands = index.get(zh_convert(place, "zh-cn"), [])
    if not cands:
        return "miss", None
    if year is not None:
        inwin = [r for r in cands
                 if r[0] is not None and r[1] is not None and r[0] <= year <= r[1]]
        if not inwin:
            return "out_of_window", None
        pool, tag = inwin, "year"
    else:
        pool, tag = cands, "noyear"
    if len(pool) == 1:
        return f"ok_{tag}_unique", pool[0]
    lons = [r[2] for r in pool]
    lats = [r[3] for r in pool]
    span = max(max(lons) - min(lons), max(lats) - min(lats))
    if span <= 0.5:
        # 坐标几乎重合：同一地方的不同朝代条目
        return f"ok_{tag}_same_place", pool[0]
    # 同名异地且都已落在年代窗口：按行政级别兜一次（府/州 > 县 > 其它）
    rank = {"Du": 0, "Fu": 1, "Zhou": 2, "Jun": 3, "Xian": 4, "xian": 4,
            "County": 5, "Wei": 6, "Pu": 7}
    best = min(pool, key=lambda r: rank.get(r[4], 9))
    top = [r for r in pool if rank.get(r[4], 9) == rank.get(best[4], 9)]
    if len(top) == 1:
        return f"ok_{tag}_by_rank", best
    return "ambig_unresolved", None


ev2 = Counter()
n_with_place = 0
q = c.execute("MATCH (n:Event) RETURN n.place, n.year")
samples2 = defaultdict(list)
while q.has_next():
    p, y = q.get_next()
    p = (p or "").strip()
    if not p:
        continue
    n_with_place += 1
    lv, rec = match_event(p, y)
    ev2[lv] += 1
    if len(samples2[lv]) < 5 and rec:
        samples2[lv].append((p, y, f"{rec[2]},{rec[3]}", rec[4], rec[0], rec[1]))

ok = sum(v for k, v in ev2.items() if k.startswith("ok_"))
print(f"有 place 的事件 {n_with_place:,}；可采用 {ok:,}（{ok / n_with_place * 100:.1f}%）")
print(f"\n{'档位':<22}{'事件数':>10}{'占比':>8}")
for k, v in ev2.most_common():
    print(f"{k:<22}{v:>10,}{v / n_with_place * 100:>7.1f}%")
print("\n-- 命中样例（地名 / 事件年 / 坐标 / 行政类型 / 地名条目年代）--")
for lv in ("ok_year_unique", "ok_year_same_place", "ok_year_by_rank",
           "ok_noyear_unique"):
    if not samples2[lv]:
        continue
    print(f"  [{lv}]")
    for s in samples2[lv]:
        print(f"    {s[0]:<10} {str(s[1]):>6} → {s[2]:<22} {s[3]:<8} "
              f"条目年代 {s[4]}~{s[5]}")

print("\n-- 各档样例（地名 / 事件数 / 候选数 / 事件年份 / 选中坐标）--")
for lv in ("L1_unique", "L2_year", "L3a_same_place", "L3b_real_ambig",
           "L4_out_of_window", "miss"):
    if not samples[lv]:
        continue
    print(f"  [{lv}]")
    for s in samples[lv]:
        print(f"    {s[0]:<10} 事件 {s[1]:>5} 候选 {s[2]:>3} "
              f"年份 {s[3]}~{s[4]} → {s[5]}")
