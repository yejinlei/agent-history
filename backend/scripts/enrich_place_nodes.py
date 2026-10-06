# -*- coding: utf-8 -*-
"""建立 Place 节点：把「图上事件实际用过的地点」变成图里的地点实体。

背景：图内 Place 节点只有 9 条（都城/遗址的手工种子），而 13.8 万事件各自用
place 文本字段记着地名，坐标散在别处。这里补齐这一层。

候选范围（宁缺毋滥）
--------------------
只收录**被图上事件引用到**的 CBDB 地址条目——判定逻辑与 enrich_event_coords.py
完全一致（同名 + 繁简归一 + 事件年份落在地名年代区间），因此每个新建 Place
背后都至少有 1 条真实事件支撑，不会出现孤立无依据的地点节点。

字段落法
--------
Place 表结构固定：id / name / ancient / modern / lon / lat / aliases / external_ids
/ provenance / narrative / ca / ua / version。
Kuzu 0.11 不支持 ALTER ADD COLUMN，**没法给 Place 加「起止年」列**，所以地名条目
的年代信息只进数仓证据表 place_nodes，图库里用 external_ids.cbdb 反查。

用法：python scripts/enrich_place_nodes.py [--apply]
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
KUZU_DB = str(config.DATA_DIR / "kg" / "kuzu_db")
DUCKDB_PATH = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
CAND = Path(config.DATA_DIR) / "core" / "normalized" / "place_node_candidates.jsonl"


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def build_place_index():
    """addr_id → 详情；名字(简体) → [(addr_id, 起止年, 坐标)]。"""
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)
    info: dict[str, dict] = {}
    xy_by_addr: dict[str, tuple] = {}
    # 注意：ADDR_CODES 里没有「今地名」这一列（pres_loc 只存在于 gis_info 侧），
    # 所以 Place.modern 先留空，将来要补可 join ADDRESSES/CHGIS 再填。
    for cid, chn, pyname, admin, fy, ly, x, y, alt in con.execute(
            "SELECT c_addr_id, c_name_chn, c_name, c_admin_type, "
            "c_firstyear, c_lastyear, x_coord, y_coord, c_alt_names "
            "FROM ADDR_CODES"):
        if not chn or chn.startswith("["):
            continue
        info[str(cid)] = {"name_chn": chn, "pyname": pyname or "",
                          "modern": "", "admin": admin or "",
                          "beg": int(fy) if str(fy).lstrip("-").isdigit() else None,
                          "end": int(ly) if str(ly).lstrip("-").isdigit() else None,
                          "alt": alt or ""}
        if x and y:
            try:
                if 73 <= float(x) <= 136 and 3 <= float(y) <= 54:
                    xy_by_addr[str(cid)] = (round(float(x), 5), round(float(y), 5))
            except Exception:
                pass
    # 坐标兜底：ADDRESSES 表补
    for cid, x, y in con.execute(
            "SELECT c_addr_id, x_coord, y_coord FROM ADDRESSES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        if str(cid) in xy_by_addr:
            continue
        try:
            if 73 <= float(x) <= 136 and 3 <= float(y) <= 54:
                xy_by_addr[str(cid)] = (round(float(x), 5), round(float(y), 5))
        except Exception:
            pass
    con.close()
    return info, xy_by_addr


def load_event_addr_index():
    """复用事件坐标脚本的口径：名字+年份 → addr_id（这里只要 addr_id 集合）。"""
    sys_modules = sys.path
    from scripts.enrich_event_coords import build_index, match_event  # noqa: E402
    return build_index(), match_event


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--relaxed-gap", type=int, default=0,
                    help="与 enrich_event_coords 同口径；>0 时把放宽档涉及的地点也收进来")
    a = ap.parse_args()

    log("构建地名词典…")
    info, xy_by_addr = build_place_index()
    log(f"CBDB 地址 {len(info):,} 条，其中有坐标 {len(xy_by_addr):,}")

    idx, match_fn = load_event_addr_index()
    log(f"事件侧词典 {len(idx):,} 个名字")

    # 读写共用同一个 Database：Kuzu 是单写者模型（文件锁），同进程里先开只读
    # 再开写会报 Could not set lock；Python 对象析构有延迟，光赋 None 释放不掉
    db = kuzu.Database(KUZU_DB)
    c = kuzu.Connection(db)
    existing: set[str] = set()
    existing_names: set[str] = set()
    q = c.execute("MATCH (n:Place) RETURN n.id, n.name")
    while q.has_next():
        pid, pname = q.get_next()
        existing.add(pid)
        if pname:
            existing_names.add(zh_convert(str(pname).strip(), "zh-cn"))
    log(f"图内现有 Place {len(existing)} 个：{sorted(existing)[:10]}")
    log(f"  现有地名（用于防重）：{sorted(existing_names)}")

    ref: Counter = Counter()
    q = c.execute("MATCH (n:Event) RETURN n.place, n.year")
    while q.has_next():
        place, year = q.get_next()
        place = (place or "").strip()
        if not place:
            continue
        # 与时事事件坐标同一条口径：判定必须一致，否则会造出「事件有坐标、
        # 却找不到对应 Place 节点」的半截状态
        lv, rec = match_fn(idx, place, year, a.relaxed_gap)
        if rec is None or not (lv.startswith("ok_")
                               or lv.startswith("relaxed_")):
            continue
        ref[rec[5]] += 1
    c = None                      # 释放读连接即可，db 要留给写阶段复用
    log(f"图上事件引用到的 CBDB 地址 {len(ref):,} 个，累计引用 {sum(ref.values()):,} 次")

    rows = []
    no_xy = dup_id = dup_name = 0
    agg: dict[tuple, dict] = {}
    for addr_id, cnt in ref.items():
        if f"place:{addr_id}" in existing:
            dup_id += 1
            continue
        d = info.get(addr_id)
        xy = xy_by_addr.get(addr_id)
        if not d or not xy:
            no_xy += 1
            continue
        name0 = zh_convert(d["name_chn"], "zh-cn")
        # 与已有种子节点同名的一律跳过：changan / luoyang / nanjing 这 9 条手工
        # 种子优先保留，同名再建只会让前端出现两个「长安」
        if name0 in existing_names:
            dup_name += 1
            continue
        aliases = []
        for nm in [d["pyname"]] + [x.strip() for x in re.split(
                r"[;,/、，]", d["alt"]) if x.strip() and not x.startswith("[")]:
            nm = nm.strip()
            if not nm:
                continue
            s = zh_convert(nm, "zh-cn")
            for cand in (nm, s):
                if cand and cand not in aliases:
                    aliases.append(cand)
        # 同名同坐标的多个 CBDB 条目（长安就有 618~907 / 703~747 / 748~959 三条，
        # 坐标一模一样）必须并成一个节点，否则前端会出现三个「长安」
        key = (name0, xy[0], xy[1])
        have = agg.get(key)
        if have is not None:
            have["ref_events"] += cnt
            bs = [v for v in (have["beg"], d["beg"]) if v is not None]
            es = [v for v in (have["end"], d["end"]) if v is not None]
            have["beg"] = min(bs) if bs else None
            have["end"] = max(es) if es else None
            for al in aliases:
                if al not in have["_aliases"]:
                    have["_aliases"].append(al)
            have["_addr_ids"].append(addr_id)
            if d["admin"] and d["admin"] not in have["_admins"]:
                have["_admins"].append(d["admin"])
            continue
        agg[key] = {
            "id": f"place:{addr_id}", "name": name0,
            "ancient": d["name_chn"], "modern": d["modern"],
            "lon": xy[0], "lat": xy[1],
            "aliases": "", "external_ids": "", "provenance": "cbdb",
            "cbdb_addr_id": addr_id, "admin": d["admin"],
            "beg": d["beg"], "end": d["end"], "ref_events": cnt,
            "_aliases": aliases, "_addr_ids": [addr_id],
            "_admins": [d["admin"]] if d["admin"] else [],
        }

    merged = sum(len(v["_addr_ids"]) - 1 for v in agg.values())
    rows = list(agg.values())
    for r in rows:
        addr_ids = r.pop("_addr_ids")
        r.pop("_admins", None)
        als = r.pop("_aliases")
        r["aliases"] = json.dumps(als, ensure_ascii=False)
        r["external_ids"] = json.dumps(
            # external_ids 这一列在图库里是 STRING（存 JSON 文本），而 Place 模型
            # 按 dict[str, str] 解析——cbdb_all 必须是字符串，写列表会让服务启动时
            # pydantic 校验失败（实测：服务直接崩在 Store.__init__ 读 Place）
            {"cbdb": addr_ids[0], "cbdb_all": ",".join(str(a) for a in addr_ids),
             "admin": r["admin"]},
            ensure_ascii=False)
    log(f"同名同坐标合并掉 {merged:,} 个条目")

    log(f"可建 Place 节点 {len(rows):,} 个；跳过：id 已存在 {dup_id}、"
        f"与种子同名 {dup_name}、无坐标 {no_xy}")
    if not rows:
        return 0

    CAND.parent.mkdir(parents=True, exist_ok=True)
    with open(CAND, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"候选清单：{CAND}")

    top = sorted(rows, key=lambda r: -r["ref_events"])
    log("\n-- 引用最多的 15 个地点 --")
    for r in top[:15]:
        span = f"{r['beg']}~{r['end']}" if r["beg"] is not None else "年代不详"
        log(f"  {r['name']:<12} 《{r['ancient']}》{r['admin']:<6} "
            f"{r['lon']:>8.2f},{r['lat']:>7.2f}  {span:<14} 被 {r['ref_events']:,} 次事件引用")
    log("\n-- 只被 1 次事件引用的占比（衡量是否太碎）--")
    one = sum(1 for r in rows if r["ref_events"] == 1)
    log(f"  {one:,} / {len(rows):,} = {one / len(rows) * 100:.1f}%")

    if not a.apply:
        log("\n干跑结束，未写入。确认后加 --apply。")
        return 0

    # ---------------- 写入 ----------------
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    wcon = kuzu.Connection(db)      # 与读阶段共用同一个 Database
    con2 = duckdb.connect(DUCKDB_PATH)
    con2.execute("""
        CREATE TABLE IF NOT EXISTS place_nodes (
            place_id VARCHAR, name VARCHAR, ancient VARCHAR, modern VARCHAR,
            lon DOUBLE, lat DOUBLE, aliases VARCHAR, external_ids VARCHAR,
            cbdb_addr_id VARCHAR, admin VARCHAR, beg_year INTEGER,
            end_year INTEGER, ref_events INTEGER, batch VARCHAR, ts TIMESTAMP
        )""")
    done = {r[0] for r in con2.execute("SELECT place_id FROM place_nodes").fetchall()}
    todo = [r for r in rows if r["id"] not in done]
    log(f"\n待写 {len(todo):,} 个 Place 节点（已跳过 {len(done):,}）")
    n = 0
    try:
        for r in todo:
            now = datetime.now().isoformat()
            wcon.execute(
                "CREATE (n:Place {id:$id, name:$name, ancient:$anc, modern:$mod, "
                "lon:$lon, lat:$lat, aliases:$aliases, n_student:$ns, n_scholar:$nc, "
                "n_fan:$nf, external_ids:$ext, provenance:$prov, "
                "ca:$ca, ua:$ua, version:$ver})",
                {"id": r["id"], "name": r["name"], "anc": r["ancient"],
                 "mod": r["modern"], "lon": r["lon"], "lat": r["lat"],
                 "aliases": r["aliases"], "ns": "", "nc": "", "nf": "",
                 "ext": r["external_ids"], "prov": r["provenance"],
                 "ca": now, "ua": now, "ver": 1})
            con2.execute(
                "INSERT INTO place_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [r["id"], r["name"], r["ancient"], r["modern"], r["lon"], r["lat"],
                 r["aliases"], r["external_ids"], r["cbdb_addr_id"], r["admin"],
                 r["beg"], r["end"], r["ref_events"], batch, datetime.now()])
            n += 1
            if n % 1000 == 0:
                log(f"  已写 {n:,} 个")
    except Exception as e:
        log(f"✗ 中断于第 {n} 个：{type(e).__name__}: {e}")
        con2.close()
        return 1
    total = wcon.execute("MATCH (n:Place) RETURN count(*)").get_next()[0]
    log(f"✓ 写入 {n:,} 个 Place 节点；图库现有 Place {total:,} 个")
    con2.close()
    wcon = None
    db = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
