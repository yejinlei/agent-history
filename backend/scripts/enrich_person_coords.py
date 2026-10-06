# -*- coding: utf-8 -*-
"""给人物补籍贯坐标（先干跑，人工抽查后 --apply）。

为什么不用已有的 Person.home
----------------------------
实测 61,355 个人物的 home **去重只有 14 个值**，且是自由描述文本
（「邯郸出生，咸阳执政」「沛国谯县（今安徽亳州）」），基本是早期手工备注，
不是可匹配的地名——这条路走不通。

改走 CBDB 人物侧
----------------
BIOG_MAIN（535,181 人）里的 c_index_addr_id + c_index_addr_type_code 就是各人的
「默认籍贯」，退而求其次取 BIOG_ADDR_DATA 里 c_addr_type=1（籍貫/基本地址）。
addr_id 再去 ADDR_CODES / ADDRESSES 取坐标——与事件坐标同一套经纬度。

难点是同名：CBDB 里「李广」可能有多人。消歧用两道证据：
  1. 生卒相容：CBDB 的 c_birthyear~c_deathyear 与图上 Person.years 有交集；
     没有生卒则退回 CBDB 朝代 c_dy 的起止年 与 图上 dynasty_id 的 span 有交集
  2. 坐标裁决：仍剩多人时，若籍贯坐标一致则采用，否则判为歧义不写

⚠ 当前状态：**干跑可以，不要 --apply**
-----------------------------------
黄金集评测（scripts/person_golden_test.py，13 位史有定论的名人）显示，哪怕挑出
最优取值策略也只有 **62% 准确率**：诸葛亮被标到成都（实为琅琊阳都）、曹操标到洛阳
（实为沛国谯县）、杜甫偏 312 km、李白偏 360 km——错的全是最显眼的核心人物。
每 3 个人就有 1 个错，宁缺毋滥，本脚本暂不写入，留待更权威的籍贯数据。
将来若拿到可信源，改这里 + 跑黄金集过 90% 再开闸。

用法
----
    python scripts/enrich_person_coords.py
    python scripts/enrich_person_coords.py --apply --limit 50
    python scripts/enrich_person_coords.py --apply
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
CAND = Path(config.DATA_DIR) / "core" / "normalized" / "person_coord_candidates.jsonl"

# 籍贯类地址类型：1=籍貫(基本地址) 5=祖籍 6=落籍 7=本貫 8=出生地 16=戶籍地 14=另一籍貫
# 按优先级排：基本籍贯最权威，出生地/祖籍兜底
_HOME_TYPES = [1, 5, 7, 6, 16, 14, 8]


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def load_cbdb():
    """返回 (名字索引, 地址坐标, 朝代年份, 人物生卒, 地址索引)。"""
    con = sqlite3.connect(f"file:{CBDB}?mode=ro", uri=True)

    # 地址坐标：ADDR_CODES 自身没有时用 ADDRESSES 兜
    addr_xy: dict[str, tuple] = {}
    for cid, x, y in con.execute(
            "SELECT c_addr_id, x_coord, y_coord FROM ADDR_CODES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        addr_xy[str(cid)] = (x, y)
    extra = 0
    for cid, x, y in con.execute(
            "SELECT c_addr_id, x_coord, y_coord FROM ADDRESSES "
            "WHERE x_coord IS NOT NULL AND trim(x_coord) <> ''"):
        if str(cid) not in addr_xy:
            addr_xy[str(cid)] = (x, y)
            extra += 1
    # 过滤越界
    addr_xy = {k: (round(float(v[0]), 5), round(float(v[1]), 5))
               for k, v in addr_xy.items()
               if 73 <= float(v[0]) <= 136 and 3 <= float(v[1]) <= 54}
    log(f"地址坐标 {len(addr_xy):,} 个（ADDRESSES 补充 {extra}）")

    dy_span: dict[str, tuple] = {}
    for d, s, e in con.execute("SELECT c_dy, c_start, c_end FROM DYNASTIES"):
        try:
            dy_span[str(d)] = (int(s), int(e))
        except Exception:
            pass

    # 人物：名字 → [(personid, birth, death, dy, index_addr_id)]
    by_name: dict[str, list[tuple]] = defaultdict(list)
    life: dict[str, tuple] = {}
    for r in con.execute(
            "SELECT c_personid, c_name_chn, c_name, c_birthyear, c_deathyear, "
            "c_dy, c_index_addr_id, c_index_addr_type_code FROM BIOG_MAIN"):
        pid, chn, pyname, by, dy_, dyid, idx_addr, idx_atype = r
        if not chn or chn.startswith("["):
            continue
        b = int(by) if str(by).lstrip("-").isdigit() and int(by) != 0 else None
        d = int(dy_) if str(dy_).lstrip("-").isdigit() and int(dy_) != 0 else None
        life[str(pid)] = (b, d, str(dyid) if dyid is not None else "")
        for nm in (chn, pyname):
            if not (nm and nm.strip()) or nm.strip().startswith("["):
                continue
            key = zh_convert(nm.strip(), "zh-cn")
            rec = (str(pid), b, d, str(dyid) if dyid is not None else "",
                   str(idx_addr) if idx_addr is not None else "",
                   str(idx_atype) if idx_atype is not None else "")
            seen_pid = {(x[0]) for x in by_name[key]}
            if rec[0] not in seen_pid:
                by_name[key].append(rec)

    # 籍贯地址（BIOG_ADDR_DATA）按类型建索引：personid → {type: addr_id}
    addr_by_person: dict[str, dict[int, str]] = defaultdict(dict)
    for pid, aid, atype in con.execute(
            "SELECT c_personid, c_addr_id, c_addr_type FROM BIOG_ADDR_DATA"):
        addr_by_person[str(pid)].setdefault(int(atype or -1), str(aid))
    con.close()
    return addr_xy, dy_span, by_name, life, addr_by_person


def resolve_addr(pid: str, idx_addr: str, idx_atype: str, addr_by_person,
                 addr_xy, dy_span, life):
    """取这个人的籍贯坐标（按类型优先级），返回 (lon, lat, how, addr_id)。

    c_index_addr_id 只有在它的 c_index_addr_type_code 属于籍贯类时才可信——
    CBDB 的「默认地址」有时是任职地/活动地：诸葛亮的默认地址是成都（任职地），
    而他的籍贯是琅琊阳都；曹操的默认地址是洛阳，籍贯却是沛国谯县。
    """
    cand_addrs: list[tuple[int, str, int]] = []
    try:
        at = int(idx_atype)
    except Exception:
        at = -1
    if idx_addr and idx_addr != "0" and at in _HOME_TYPES:
        cand_addrs.append((0, idx_addr, at))
    for rank, t in enumerate(_HOME_TYPES, start=1):
        a = addr_by_person.get(pid, {}).get(t)
        if a and a != "0":
            cand_addrs.append((rank, a, t))
    for rank, aid, t in cand_addrs:
        xy = addr_xy.get(aid)
        if xy:
            how = f"index_type{t}" if rank == 0 else f"addr_type{t}"
            return xy[0], xy[1], how, aid
    return None


def overlap(a: tuple, b: tuple) -> bool:
    """两个年份区间是否有交集；任一侧为空视为相容（不据此否定）。"""
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    addr_xy, dy_span, by_name, life, addr_by_person = load_cbdb()
    log(f"人名索引 {len(by_name):,} 个简体名字")

    # 图里的人物 + 朝代 span
    db = kuzu.Database(KUZU_DB)
    c = kuzu.Connection(db)
    dy_map: dict[str, tuple] = {}
    q = c.execute("MATCH (n:Dynasty) RETURN n.id, n.span_s, n.span_e")
    while q.has_next():
        did, s, e = q.get_next()
        try:
            dy_map[str(did)] = (int(s), int(e))
        except Exception:
            pass
    log(f"图内朝代 {len(dy_map)} 个")

    people = []
    q = c.execute(
        "MATCH (n:Person) RETURN n.id, n.name, n.years, n.aliases, n.dynasty_id, "
        "n.lon, n.lat")
    while q.has_next():
        pid, name, years, aliases, did, lon, lat = q.get_next()
        people.append((pid, name, years, aliases, did, lon, lat))
    q = None
    log(f"图内人物 {len(people):,} 名")
    c = None
    db = None

    hits = []
    stat: Counter = Counter()
    for pid, name, years, aliases, did, lon, lat in people:
        name = (name or "").strip()
        if not name:
            stat["no_name"] += 1
            continue
        # 名字候选：整体去空格 / 分段（处理「秦始皇 嬴政」这类带字的写法）
        variants = {zh_convert(name.replace(" ", ""), "zh-cn")}
        parts = [p for p in re.split(r"[\s]+", name) if p]
        for p in parts:
            variants.add(zh_convert(p, "zh-cn"))
        try:
            for al in json.loads(aliases or "[]"):
                if isinstance(al, str) and al.strip():
                    variants.add(zh_convert(al.strip(), "zh-cn"))
        except Exception:
            pass
        # 图 Homelander 的生卒
        ours: tuple | None = None
        try:
            ys = json.loads(years or "[]")
            if isinstance(ys, list) and len(ys) == 2:
                a = int(ys[0]) if str(ys[0]).lstrip("-").isdigit() else None
                b = int(ys[1]) if str(ys[1]).lstrip("-").isdigit() else None
                if a is not None or b is not None:
                    ours = (a if a is not None else (b - 80 if b else None),
                            b if b is not None else (a + 80 if a else None))
        except Exception:
            pass
        our_dy = dy_map.get(str(did or ""), None)

        cands: dict[str, tuple] = {}
        for v in variants:
            for (cbpid, cb, cd, cdy, idx, atyp) in by_name.get(v, []):
                cands.setdefault(cbpid, (cb, cd, cdy, idx, atyp))
        if not cands:
            stat["miss"] += 1
            continue
        # 第一道：生卒相容
        kept = []
        for cbpid, (cb, cd, cdy, idx, atyp) in cands.items():
            # 第一道：生卒相容（两边都有才算证据，缺一侧不否决）
            if ours is not None and (cb is not None or cd is not None):
                clo = cb if cb is not None else (cd - 80 if cd is not None else ours[0])
                chi = cd if cd is not None else (cb + 80 if cb is not None else ours[1])
                if not overlap(ours, (clo, chi)):
                    continue
            # 第二道：朝代相容——无论有没有生卒都要过这一关。
            # 图里把人物挂在某个朝代（如 xihan），CBDB 的 c_dy 也要能覆盖同一时期，
            # 否则就是隔了几百年的同名之人。
            if our_dy is not None and cdy in dy_span:
                if not overlap(our_dy, dy_span[cdy]):
                    continue
            kept.append((cbpid, idx, atyp))
        if not kept:
            stat["conflict_life"] += 1
            continue
        # 取籍贯坐标
        picked: list[dict] = []
        for cbpid, idx, atyp in kept:
            r = resolve_addr(cbpid, idx, atyp, addr_by_person, addr_xy,
                             dy_span, life)
            if r:
                picked.append({"cbpid": cbpid, "lon": r[0], "lat": r[1],
                               "how": r[2], "addr_id": r[3]})
        if not picked:
            stat["no_ancestral_xy"] += 1
            continue
        coords = {(p["lon"], p["lat"]) for p in picked}
        if len(coords) > 1:
            stat["ambig_coord"] += 1
            continue
        p0 = picked[0]
        tag = ("ok_unique" if len(kept) == 1 else "ok_same_coord")
        stat[tag] += 1
        hits.append({"person_id": pid, "person_name": name, "years": years,
                     "dynasty_id": did, "old_lon": lon, "old_lat": lat,
                     "lon": p0["lon"], "lat": p0["lat"], "how": tag,
                     "src": p0["how"], "cbdb_personid": p0["cbpid"],
                     "cbdb_addr_id": p0["addr_id"],
                     "n_cands": len(kept)})

    total = len(people)
    ok = sum(v for k, v in stat.items() if k.startswith("ok_"))
    log("匹配统计：")
    for k, v in stat.most_common():
        log(f"  {k:<18} {v:>8,}  {v / total * 100:>5.1f}%")
    log(f">> 可写入 {ok:,} 条（占全部人物 {ok / total * 100:.1f}%）")
    if not hits:
        log("没有命中")
        return 0

    CAND.parent.mkdir(parents=True, exist_ok=True)
    with open(CAND, "w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    log(f"候选清单：{CAND}（{len(hits):,} 行）")

    log("\n-- 抽查（每档 6 条）--")
    seen: dict[str, list] = defaultdict(list)
    for h in hits:
        if len(seen[h["how"]]) < 6:
            seen[h["how"]].append(h)
    for how, hs in seen.items():
        log(f"  [{how}]")
        for h in hs:
            log(f"    {h['person_name']:<12} ({h['dynasty_id']}, {h['years']}) → "
                f"{h['lon']:.2f},{h['lat']:.2f}  [{h['src']}] "
                f"cbdb人 {h['cbdb_personid']}")

    if not args.apply:
        log("\n干跑结束，未写入。确认后加 --apply。")
        return 0

    # ---------------- 写入 ----------------
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    con = duckdb.connect(DUCKDB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS person_coords (
            person_id      VARCHAR, person_name   VARCHAR,
            dynasty_id     VARCHAR, years         VARCHAR,
            old_lon        DOUBLE,  old_lat       DOUBLE,
            lon            DOUBLE,  lat           DOUBLE,
            cbdb_personid  VARCHAR, cbdb_addr_id  VARCHAR,
            how            VARCHAR, src           VARCHAR,
            n_cands        INTEGER, batch         VARCHAR, ts TIMESTAMP
        )""")
    done = {r[0] for r in con.execute(
        "SELECT person_id FROM person_coords").fetchall()}
    todo = [h for h in hits if h["person_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"\n待写 {len(todo):,} 条（已跳过 {len(done):,}，幂等）")
    if not todo:
        con.close()
        return 0

    wdb = kuzu.Database(KUZU_DB)
    wcon = kuzu.Connection(wdb)
    n = 0
    try:
        for h in todo:
            wcon.execute(
                "MATCH (n:Person) WHERE n.id = $id SET n.lon = $lon, n.lat = $lat",
                {"id": h["person_id"], "lon": h["lon"], "lat": h["lat"]})
            con.execute(
                "INSERT INTO person_coords VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [h["person_id"], h["person_name"], h["dynasty_id"], h["years"],
                 h["old_lon"], h["old_lat"], h["lon"], h["lat"],
                 h["cbdb_personid"], h["cbdb_addr_id"], h["how"], h["src"],
                 h["n_cands"], batch, datetime.now()])
            n += 1
            if n % 2000 == 0:
                wcon = None
                wcon = kuzu.Connection(wdb)
                log(f"  已写 {n:,} 条")
    except Exception as e:
        log(f"✗ 中断于第 {n} 条：{type(e).__name__}: {e}")
        log("  已写部分有证据记录，重跑会自动跳过")
        con.close()
        return 1
    left = wcon.execute(
        "MATCH (n:Person) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
        "RETURN count(*)").get_next()[0]
    log(f"✓ 写入 {n:,} 条；图库内有坐标的人物 {left:,} 名（batch={batch}）")
    con.close()
    wcon = None
    wdb = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
