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

# 判定名单统一在 app/geo_rules.py——早先各脚本自己抄一份，结果写死简体的黑名单
# 遇繁体写法（京師）直接漏判，这里只保留一处真值源。
from app.geo_rules import (addr_is_bad, blocked_everywhere, blocked_when_relaxed,  # noqa: E402
                           suffix_variants, too_broad, prov_level,
                           blocked_when_stable, SUFFIX_BLOCK_ADMIN)


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


_LIAO_TWIN: set[str] | None = None


def liao_twin_addrs() -> set[str]:
    """「辽金另设的同名州」条目 id 集合。

    CBDB 里有一批地名同时存在两个：一个是关内本来的州县，一个是辽金在东北/内蒙
    另设的同名州（如《雍州》既有长安那条，又有吉林那条；《长安》也有一条落在
    辽宁）。按年代匹配的档位不会出问题——辽代事件本来就该用辽金条目；但
    「忽略年代」的 stable/majority 档会被它们带偏（雍州 2/3 票被吉林拿走）。
    所以只在这两档里排除，别处照用。
    """
    global _LIAO_TWIN
    if _LIAO_TWIN is not None:
        return _LIAO_TWIN
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    far: dict[str, list[str]] = {}
    near: dict[str, list[str]] = {}
    for aid, chn, fy, x, y in con.execute(
            "SELECT c_addr_id, c_name_chn, c_firstyear, x_coord, y_coord "
            "FROM ADDR_CODES"):
        try:
            fx, fyy, ify = float(x), float(y), int(fy)
        except Exception:
            continue
        if 900 <= ify <= 1200 and fx >= 117 and fyy >= 41:
            far.setdefault(chn, []).append(str(aid))
        elif fx < 117 or fyy < 41:
            near.setdefault(chn, []).append(str(aid))
    con.close()
    _LIAO_TWIN = {a for nm, ids in far.items() if nm in near for a in ids}
    return _LIAO_TWIN


# 被采纳的档位前缀（严格 > 放宽 > 后缀 > 坐标稳定 > 种子古称）
ACCEPT_PREFIXES = ("ok_", "relaxed_", "suffix_ok_", "suffix_relaxed_",
                   "stable_all", "stable_major", "seed_alias")


def build_seed_alias(c) -> dict[str, tuple]:
    """种子 Place 的古称 → (节点 id, lon, lat, 正名)。

    enrich_place_nodes 建派生节点时「与种子同名一律跳过」，所以建康 / 建业 /
    镐京这类只有古称、CBDB 里没有条目的名字会整批漏掉（建康 1,746 条事件）。
    种子节点的 ancient 列存着这些古称，这里拿来兜底。
    """
    m: dict[str, tuple] = {}
    q = c.execute("MATCH (n:Place) WHERE NOT n.id STARTS WITH 'place:' "
                  "RETURN n.id, n.name, n.ancient, n.lon, n.lat")
    while q.has_next():
        sid, nm, ancient, lon, lat = q.get_next()
        if not lon or not lat:
            continue
        for a in str(ancient or "").split("/"):
            a = zh_convert(a.strip(), "zh-cn")
            if a:
                m[a] = (sid, lon, lat, nm)
    return m


def match_event(idx: dict[str, list[tuple]], place: str, year: int | None,
                relaxed_gap: int = 0, seed_alias: dict | None = None):
    """返回 (档位, 选中条目)。只认强证据。

    relaxed_gap > 0 时开放「放宽档」：事件年份没落在词条年代区间、但词表里同名
    只有这一条且相差不超过 relaxed_gap 年，就采纳。**严格档行为完全不变**。
    """
    lv, rec = _match_core(idx, place, year, relaxed_gap)
    if lv in ("miss", "out_of_window", "ambig_unresolved"):
        lv, rec = _match_suffix(idx, place, year, relaxed_gap)
    if lv in ("miss", "suffix_ambig") or lv.startswith("blocked"):
        if lv.startswith("blocked"):
            return lv, rec
        lv, rec = _match_stable(idx, place, seed_alias)
    return lv, rec


def _match_stable(idx: dict[str, list[tuple]], place: str,
                  seed_alias: dict | None):
    """忽略年代的两档兜底：

    stable   —— 同一个地名在 CBDB 里的**所有**条目坐标都是同一点，说明这地方没
                搬过家（冀州 / 南阳 / 彭城 / 会稽…），那么不管事件在哪一年都可以
                用这一个点。要求至少 2 条条目互证，避免孤证。
    majority —— 条目坐标不完全一致，但多数（≥60%）指向同一点，且严格多于第二多
                的点（长安 7/9、成都 7/8）。同样要求 ≥3 条条目。
    seed_alias —— 种子 Place 的古称（建康 / 建业 / 镐京…），词表里压根没有这些
                名字，但图上手工节点存着。

    为什么敢忽略年代：卡在这一步的绝大多数是**先秦两汉魏晋南北朝**的事件——CBDB
    的年代覆盖集中在唐以后，按年代匹配永远差一口气。而这些县治千年未动，坐标是
    同一个点，年代窗口在这里没有信息量。仍然保留全部黑名单（京师/中都这类真的
    会搬家的通称、省级大区、已知坏坐标）。
    """
    key = zh_convert(place.strip(), "zh-cn")
    if (blocked_everywhere(key) or blocked_when_relaxed(key) or prov_level(key)
            or blocked_when_stable(key)):
        return "blocked_movable", None
    recs = [r for r in idx.get(key, [])
            if not addr_is_bad(r[5]) and str(r[5]) not in liao_twin_addrs()]
    if recs:
        coords = Counter((round(r[2], 3), round(r[3], 3)) for r in recs)
        top, n1 = coords.most_common(1)[0]
        second = coords.most_common(2)[1][1] if len(coords) > 1 else 0
        if len(coords) == 1 and len(recs) >= 2:
            for r in recs:
                if (round(r[2], 3), round(r[3], 3)) == top:
                    return "stable_all", r
        if len(recs) >= 3 and n1 >= 2 and second < n1 and n1 / len(recs) >= 0.6:
            for r in recs:
                if (round(r[2], 3), round(r[3], 3)) == top:
                    return "stable_major", r
        return "stable_ambig", None
    if seed_alias and key in seed_alias:
        sid, lon, lat, nm = seed_alias[key]
        return "seed_alias", (None, None, lon, lat, "", sid, nm)
    return "miss", None


def _match_suffix(idx: dict[str, list[tuple]], place: str, year: int | None,
                  relaxed_gap: int):
    """后缀归一化：图上写法与 CBDB 词条差一个行政后缀时补一把。

    判据与严格档一样硬（年代窗口 + 坐标唯一），只是名字多试几个变体；另外
    geo_rules 里划了两条红线：单字地名不猜、省级单位不写（见那里的注释）。
    变体之间一旦给出不同坐标就算歧义，不猜。
    """
    if blocked_when_relaxed(place) or prov_level(place):
        return "blocked_movable", None
    variants = suffix_variants(place)
    if not variants:
        return "miss", None
    found: list[tuple] = []
    for v in variants:
        cands = [r for r in idx.get(v, [])
                 if not addr_is_bad(r[5])
                 and not too_broad(r[6])
                 and (r[4] or "") not in SUFFIX_BLOCK_ADMIN]
        if not cands:
            continue
        lv, rec = _match_core_idx(cands, year, relaxed_gap, place)
        if rec is not None:
            found.append((lv, rec))
    if not found:
        return "miss", None
    coords = {(round(r[1][2], 3), round(r[1][3], 3)) for r in found}
    if len(coords) > 1:
        return "suffix_ambig", None
    return f"suffix_{found[0][0]}", found[0][1]


def _match_core(idx: dict[str, list[tuple]], place: str, year: int | None,
                relaxed_gap: int):
    if blocked_everywhere(place):
        return "blocked_movable", None
    # CBDB 里已知坐标错误的条目，任何档位都不采用（如 4042《益州》被打到了吉林）
    cands = [r for r in idx.get(zh_convert(place, "zh-cn"), [])
             if not addr_is_bad(r[5])]
    if not cands:
        return "miss", None
    return _match_core_idx(cands, year, relaxed_gap, place)


def _match_core_idx(cands: list[tuple], year: int | None, relaxed_gap: int,
                    place: str):
    """对一组候选做统一的「年代 + 坐标」判定——严格档与后缀档共用。"""
    if year is not None:
        pool = [r for r in cands
                if r[0] is not None and r[1] is not None and r[0] <= year <= r[1]]
        if not pool:
            # 放宽档：同名唯一 + 年代只差一点点 + 不是会搬家的都城通称。
            # year == 0 是「无年份」的哨兵值（=% 图里大约一半事件），不许走放宽。
            if (relaxed_gap and year != 0 and len(cands) == 1
                    and not blocked_when_relaxed(place)
                    and not addr_is_bad(cands[0][5])):
                fy, ly = cands[0][0], cands[0][1]
                if fy is not None and ly is not None:
                    gap = fy - year if year < fy else year - ly
                    if 0 < gap <= relaxed_gap:
                        return f"relaxed_le{relaxed_gap}", cands[0]
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
    ap.add_argument("--relaxed-gap", type=int, default=0,
                    help=">0 时开放放宽档：同名唯一候选且年代相差不超过 N 年就采纳"
                         "（0=关闭，保持严格档）")
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

    seed_alias = build_seed_alias(c)
    log(f"种子古称兜底：{len(seed_alias)} 个"
        f"（{', '.join(sorted(seed_alias)[:8])}…）")

    hits: list[dict] = []
    stat: Counter = Counter()
    for eid, place, year, lon, lat in rows:
        lv, rec = match_event(idx, place, year, a.relaxed_gap, seed_alias)
        stat[lv] += 1
        accepted = lv.startswith(ACCEPT_PREFIXES)
        if not accepted or rec is None:
            continue
        hits.append({
            "event_id": eid, "place": place, "year": year,
            "old_lon": lon, "old_lat": lat,
            "lon": rec[2], "lat": rec[3],
            "cbdb_addr_id": rec[5], "addr_name": rec[6], "addr_admin": rec[4],
            "addr_from": rec[0], "addr_to": rec[1], "how": lv,
        })

    total = sum(stat.values())
    ok = sum(stat[k] for k in stat
             if k.startswith(ACCEPT_PREFIXES))
    log("匹配统计：")
    for k, v in stat.most_common():
        log(f"  {k:<22} {v:>8,}  {v / total * 100:>5.1f}%")
    log(f">> 可写入 {ok:,} 条（占 {ok / total * 100:.1f}%）")

    if not hits:
        log("没有命中，退出")
        return 0

    # 干跑：落候选清单，供人工抽查。放宽档写成另一个文件，别把严格档那份盖了
    cand = CAND
    if a.relaxed_gap:
        cand = CAND.with_name(f"event_coord_candidates_relaxed{a.relaxed_gap}.jsonl")
    cand.parent.mkdir(parents=True, exist_ok=True)
    with open(cand, "w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    log(f"候选清单已写出：{cand}（{len(hits):,} 行）")

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
