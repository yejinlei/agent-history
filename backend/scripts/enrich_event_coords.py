# -*- coding: utf-8 -*-
"""按严格口径给事件补坐标（先干跑出候选，人工抽查后再 --apply）。

为什么先干跑
------------
坐标一旦写错很难靠肉眼发现——地图上"看起来有数据"比空白更危险。所以流程是：
    dry-run 出候选清单 → 人工抽查 → --apply 写入 → 再抽查
而且每个写入动作都会在 DuckDB 的 event_coords 表里留下证据（含写入前的旧值），
配合 scripts/rollback_event_coords.py 可一键还原。

匹配口径（宁缺毋滥）
--------------------
取 CBDB ADDR_CODES/ADDRESSES 做地名词典，两边统一到简体后按事件逐个匹配：
  ok_year_unique       同名只有一个候选，且事件年份落在它的年代区间
  ok_year_same_place   同名多个候选落在窗口，但坐标几乎重合（≤0.5°）
  ok_year_by_rank      仍多条时，按行政级别取最高且唯一的那条
  ok_noyear_unique     事件没有年份，但同名唯一
其余一律不写：out_of_window（年代不符）/ ambig_unresolved（同名异地真歧义）/ miss

为什么不写『坐标来源』到 Event 节点上
-------------------------------------
Kuzu 0.11 不支持 ALTER TABLE ADD COLUMN（实测 Invalid input <ADD COLUMN>），
存量库没法加列。证据因此落在数仓表 event_coords 里，图库只存坐标本身。

用法
----
    python scripts/enrich_event_coords.py                     # 干跑
    python scripts/enrich_event_coords.py --apply --limit 50  # 试写 50 条
    python scripts/enrich_event_coords.py --apply             # 全量写
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config

from zhconv import convert as zh_convert

import duckdb
import kuzu

CBDB = Path(config.DATA_DIR) / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db"
KUZU_DB = Path(config.DATA_DIR) / "kg" / "kuzu_db"
DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"
CAND = Path(config.DATA_DIR) / "core" / "normalized" / "event_coord_candidates.jsonl"

# 行政级别兜底时的优先级：都城/府 > 州 > 县 > 其它
_RANK = {"Du": 0, "Fu": 1, "Zhou": 2, "Jun": 3, "Xian": 4, "xian": 4,
         "County": 5, "Wei": 6, "Pu": 7, "Lu": 2}

# 「随朝代搬家」的都城通称：同一个词在 CBDB 里只有一条跨数百年的坐标，但历史上
# 它指的地方随定都而变，写进去必错。实证：
#   京师 305 条，CBDB 全标北京(116.37,39.93)，可 1368~1420 年的明初京师是南京；
#   中都  25 条，CBDB 标山东汶上(116.48,35.73)，可 1153 年金中都在北京。
# 这类词一律不写——宁缺毋滥。（「南京」「北京」经查年代集中且指向正确，保留）
_MOVABLE_CAPITAL = {
    "京师", "京城", "京都", "京阙", "京畿", "中都", "上都", "下都", "东京",
    "西京", "北都", "南都", "盛京", "兴京", "行在", "留都", "都城", "宸京",
}


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def build_index() -> dict[str, list[tuple]]:
    """名字（简体）→ [(起年, 止年, 经, 纬, 行政类型, addr_id, 原名)]。"""
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    addr_xy: dict[str, tuple] = {}
    for r in con.execute(
            "SELECT c_addr_id, x_coord, y_coord FROM ADDRESSES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        addr_xy[str(r[0])] = (r[1], r[2])

    idx: dict[str, list[tuple]] = defaultdict(list)
    for aid, pyname, chn, fy, ly, admin, x, y, alt in con.execute(
            "SELECT c_addr_id, c_name, c_name_chn, c_firstyear, c_lastyear, "
            "c_admin_type, x_coord, y_coord, c_alt_names FROM ADDR_CODES"):
        if chn and chn.startswith("["):
            continue
        if not x or not y:
            if str(aid) in addr_xy:
                x, y = addr_xy[str(aid)]
        try:
            fx, fyy = float(x), float(y)
        except Exception:
            continue
        if not (73 <= fx <= 136 and 3 <= fyy <= 54):     # 越界坐标丢弃
            continue
        rec = (int(fy) if str(fy).lstrip("-").isdigit() else None,
               int(ly) if str(ly).lstrip("-").isdigit() else None,
               round(fx, 5), round(fyy, 5), admin or "", str(aid), chn)
        names: set[str] = set()
        for nm in (chn, pyname):
            if nm and nm.strip() and not nm.strip().startswith("["):
                names.add(nm.strip())
        if alt:
            for piece in re.split(r"[;,/、，]", alt):
                piece = piece.strip()
                if piece and not piece.startswith("["):
                    names.add(piece)
        for nm in names:
            idx[zh_convert(nm, "zh-cn")].append(rec)
    con.close()
    return idx


def match_event(idx: dict[str, list[tuple]], place: str, year: int | None):
    """返回 (档位, 选中条目)。只认强证据。"""
    if place in _MOVABLE_CAPITAL:
        return "blocked_movable", None
    cands = idx.get(zh_convert(place, "zh-cn"), [])
    if not cands:
        return "miss", None
    if year is not None:
        pool = [r for r in cands
                if r[0] is not None and r[1] is not None and r[0] <= year <= r[1]]
        if not pool:
            return "out_of_window", None
        tag = "year"
    else:
        pool, tag = cands, "noyear"
    if len(pool) == 1:
        return f"ok_{tag}_unique", pool[0]
    lons = [r[2] for r in pool]
    lats = [r[3] for r in pool]
    span = max(max(lons) - min(lons), max(lats) - min(lats))
    if span <= 0.5:
        # 坐标几乎重合：同一地点的不同朝代条目，取年代中心最近的
        mid = (min(r[0] for r in pool if r[0] is not None),
               max(r[1] for r in pool if r[1] is not None))
        best = pool[0]
        return f"ok_{tag}_same_place", best
    best = min(pool, key=lambda r: _RANK.get(r[4], 9))
    top = [r for r in pool if _RANK.get(r[4], 9) == _RANK.get(best[4], 9)]
    if len(top) == 1:
        return f"ok_{tag}_by_rank", best
    return "ambig_unresolved", None


def ensure_evidence_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS event_coords (
            event_id       VARCHAR,
            place          VARCHAR,
            year           INTEGER,
            old_lon        DOUBLE,
            old_lat        DOUBLE,
            lon            DOUBLE,
            lat            DOUBLE,
            cbdb_addr_id   VARCHAR,
            addr_name      VARCHAR,
            addr_admin     VARCHAR,
            addr_from      INTEGER,
            addr_to        INTEGER,
            how            VARCHAR,
            batch          VARCHAR,
            ts             TIMESTAMP
        )""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多写多少条")
    a = ap.parse_args()

    log("构建 CBDB 地名词典…")
    idx = build_index()
    log(f"词典就绪：{len(idx):,} 个简体名字")

    # 读写共用同一个 Database：Kuzu 是单写者模型（文件锁），同一进程里先开只读
    # 再开写库会报 Could not set lock；服务已停，这里直接独占读写最省事
    db = kuzu.Database(str(KUZU_DB))
    c = kuzu.Connection(db)
    rows: list[tuple] = []
    q = c.execute("MATCH (n:Event) RETURN n.id, n.place, n.year, n.lon, n.lat")
    while q.has_next():
        eid, place, year, lon, lat = q.get_next()
        place = (place or "").strip()
        if not place:
            continue
        rows.append((eid, place, year, lon, lat))
    q = None
    log(f"待匹配事件 {len(rows):,} 条")

    hits: list[dict] = []
    stat: Counter = Counter()
    for eid, place, year, lon, lat in rows:
        lv, rec = match_event(idx, place, year)
        stat[lv] += 1
        if not lv.startswith("ok_") or rec is None:
            continue
        hits.append({
            "event_id": eid, "place": place, "year": year,
            "old_lon": lon, "old_lat": lat,
            "lon": rec[2], "lat": rec[3],
            "cbdb_addr_id": rec[5], "addr_name": rec[6], "addr_admin": rec[4],
            "addr_from": rec[0], "addr_to": rec[1], "how": lv,
        })

    total = sum(stat.values())
    ok = sum(stat[k] for k in stat if k.startswith("ok_"))
    log("匹配统计：")
    for k, v in stat.most_common():
        log(f"  {k:<22} {v:>8,}  {v / total * 100:>5.1f}%")
    log(f">> 可写入 {ok:,} 条（占 {ok / total * 100:.1f}%）")

    if not hits:
        log("没有命中，退出")
        return 0

    # 干跑：落候选清单，供人工抽查
    CAND.parent.mkdir(parents=True, exist_ok=True)
    with open(CAND, "w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    log(f"候选清单已写出：{CAND}（{len(hits):,} 行）")

    # 抽查：按档位各取几条，连同地名条目年代一起打印，供人工判断
    log("\n-- 抽查样本（按档位各 4 条）--")
    by_how: dict[str, list[dict]] = defaultdict(list)
    for h in hits:
        if len(by_how[h["how"]]) < 4:
            by_how[h["how"]].append(h)
    for how, hs in by_how.items():
        log(f"  [{how}]")
        for h in hs:
            log(f"    《{h['place']}》事件年 {h['year']} → "
                f"{h['lon']},{h['lat']}  "
                f"（词条《{h['addr_name']}》{h['addr_from']}~{h['addr_to']}"
                f" {h['addr_admin']}）")

    if not a.apply:
        log("\n干跑结束，未写入任何数据。确认无误后加 --apply。")
        return 0

    # ---------------- 写入 ----------------
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    con = duckdb.connect(str(DUCKDB_PATH))          # 读写：建证据表 + 记录
    ensure_evidence_table(con)
    done = {r[0] for r in con.execute(
        "SELECT event_id FROM event_coords").fetchall()}
    todo = [h for h in hits if h["event_id"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    log(f"\n开始写入：待写 {len(todo):,} 条（已有 {len(done):,} 条跳过，幂等）")
    if not todo:
        con.close()
        return 0

    wcon = kuzu.Connection(db)          # 与读阶段共用同一个 Database
    n = 0
    reopen_every = 2000
    try:
        for h in todo:
            wcon.execute(
                "MATCH (n:Event) WHERE n.id = $id SET n.lon = $lon, n.lat = $lat",
                {"id": h["event_id"], "lon": h["lon"], "lat": h["lat"]})
            con.execute(
                "INSERT INTO event_coords VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [h["event_id"], h["place"], h["year"], h["old_lon"], h["old_lat"],
                 h["lon"], h["lat"], h["cbdb_addr_id"], h["addr_name"],
                 h["addr_admin"], h["addr_from"], h["addr_to"], h["how"],
                 batch, datetime.now()])
            n += 1
            if n % reopen_every == 0:
                # 长时间批量 UPDATE 会把连接脏页堆住，定期重开（挂载时踩过同样的坑）
                wcon = None
                wcon = kuzu.Connection(db)
                log(f"  已写 {n:,} 条")
    except Exception as e:
        log(f"✗ 写入中断于第 {n} 条：{type(e).__name__}: {e}")
        log("  已写入的部分都有证据记录，重跑会自动跳过；"
            "要整体还原请跑 scripts/rollback_event_coords.py")
        con.close()
        return 1
    log(f"✓ 写入完成：Kuzu {n} 条坐标 + 证据 {n} 行（batch={batch}）")

    # 写后立即自检
    check = wcon.execute(
        "MATCH (n:Event) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
        "RETURN count(*)").get_next()[0]
    log(f"自检：图库内有坐标的事件 {check:,} 条")
    con.close()
    wcon = None
    db = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
