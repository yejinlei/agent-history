# -*- coding: utf-8 -*-
"""给图上人物挂 CBDB 锚点，并带回籍贯坐标。

为什么这么做
------------
图上的 61,355 个人物几乎全是从书里抽出来的（`home` 是占位符「（挂载书源实体）」），
`external_ids` 是空的 —— 也就是说，这些「张三」「李四」到底是谁，没有任何权威档案
可以佐证。而 CBDB 有 535,180 个人物、27 万条带坐标的籍贯记录，我们本地就有。

对齐口径（保守：宁缺毋滥）
--------------------------
按姓名匹配（两边统一转简体后比较），**只有同名唯一才落锚点**：
- 同名唯一 → 直接采信，写 external_ids {"cbdb": ...}，坐标也一并带回
- 同名多候选（如「王佐」CBDB 有 76 个）→ 一律标待定，**不猜**
理由：7 成图书人物是常见的两字人名，靠姓名 + 生卒/朝代去猜，错一次就是把一个人的
生平安到另一个人头上，比缺锚点更难发现。

为什么不走 store.embed/commit
-----------------------------
Store 会把全部实体常驻内存再差分落盘，本机可用提交内存只剩几 GB，Kuzu 在
VirtualAlloc 阶段就会失败（挂载大书时踩到过 ERROR 1455）。这里沿用
enrich_event_coords 的路子：直连 Kuzu 批量 SET，改动很小且可回滚。

用法
----
    python scripts/enrich_person_anchors.py                  # 干跑，先看样例
    python scripts/enrich_person_anchors.py --apply --limit 200
    python scripts/enrich_person_anchors.py --apply          # 全量（幂等）
"""
from __future__ import annotations

import argparse
import json
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
from app import config                                       # noqa: E402

from zhconv import convert as zh_convert                     # noqa: E402

import duckdb                                                # noqa: E402
import kuzu                                                  # noqa: E402

CBDB_DB = Path(config.DATA_DIR) / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db"
KUZU_DB = Path(config.DATA_DIR) / "kg" / "kuzu_db"
DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"
# 只用「籍貫(基本地址)」：CBDB 每人对一个索引地，type=1 共 276,740 行，98.4% 带坐标
ADDR_TYPE_BASIC = 1
# 年代容差：图上人物的 years 是生卒或活动期，CBDB 用的是 index_year（索引年，
# 多半是登科/出仕之年），给 30 年的余量仍足以把「汉人对上明人」这类错配挡掉
TOL = 30

# 图上 dynasty_id → CBDB dynasty 可接受的取值。CBDB 这个字段只有 17 个取值、
# 且缺宋/金/元/遼，26 万人干脆为空，所以只能兜底、不能当主证据；没有对应关系的
# 朝代（beiSong/liao/yuan 等）一律不猜。
DYNASTY_MAP = {
    "tang": {"唐"},
    "ming": {"明"},
    "sui": {"隋"},
    "nanbei": {"南北朝"},
    "wudai": {"五代", "南唐", "後蜀", "吳越"},
    "qin": {"秦漢", "漢前"},
    "xihan": {"秦漢", "漢前"},
    "donghan": {"秦漢", "漢前"},
    "jin": {"東晉", "西晉"},
    "shu": {"三國"},
    "xing": {"清"},
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _key(name: str) -> str:
    """统一到简体并去掉空白，作为跨库姓名比较的键。"""
    return zh_convert((name or "").strip(), "zh-cn")


def load_graph_persons(conn: kuzu.Connection) -> list[dict]:
    q = conn.execute(
        "MATCH (n:Person) RETURN n.id, n.name, n.years, n.dynasty_id, n.lon, "
        "n.external_ids")
    rows = []
    while q.has_next():
        pid, name, years, dy, lon, ext = q.get_next()
        rows.append({"id": pid, "name": name or "", "lon": lon,
                     "years": _years(years), "dynasty_id": dy or "",
                     "external_ids": ext or ""})
    return rows


def _years(raw: str) -> list[int]:
    """把 Person.years（JSON 字符串，可能含 "?"）里的年份取出来。"""
    try:
        vals = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except Exception:
        return []
    out = []
    for v in vals:
        s = str(v).strip()
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def load_cbdb_index() -> dict[str, list[dict]]:
    """CBDB 全量人物：姓名 → 候选列表。"""
    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    idx: dict[str, list[dict]] = defaultdict(list)
    for cid, name, iy, dy, yb, yd in duck.execute(
            "SELECT id, name, index_year, dynasty, year_birth, year_death "
            "FROM core_persons").fetchall():
        k = _key(name)
        if not k:
            continue
        idx[k].append({"id": cid, "name": name, "index_year": iy,
                       "dynasty": dy or "", "yb": yb, "yd": yd})
    duck.close()
    return idx


def pick_hometown(sql: sqlite3.Connection, cbdb_id: str,
                  year: int | None) -> dict | None:
    """取该人的籍贯坐标；多条时优先取年代覆盖该人活动期的那条。"""
    rows = sql.execute(
        "SELECT a.c_name_chn, a.c_admin_type, a.x_coord, a.y_coord, "
        "       b.c_firstyear, b.c_lastyear "
        "FROM BIOG_ADDR_DATA b JOIN ADDR_CODES a ON a.c_addr_id = b.c_addr_id "
        "WHERE b.c_personid = ? AND b.c_addr_type = ? "
        "ORDER BY b.c_sequence",
        (cbdb_id, ADDR_TYPE_BASIC)).fetchall()
    got = []
    for nm, admin, x, y, fy, ly in rows:
        try:
            lon, lat = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        got.append({"addr_name": nm or "", "admin": admin or "",
                    "lon": lon, "lat": lat,
                    "fy": int(fy) if str(fy).lstrip("-").isdigit() else None,
                    "ly": int(ly) if str(ly).lstrip("-").isdigit() else None})
    if not got:
        return None
    if year is not None:
        for g in got:
            if ((g["fy"] is None or g["fy"] <= year)
                    and (g["ly"] is None or g["ly"] >= year)):
                return g
    return got[0]


def ensure_evidence_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS person_anchors (
            person_id      VARCHAR,
            name           VARCHAR,
            cbdb_id        VARCHAR,
            cbdb_name      VARCHAR,
            cbdb_year      INTEGER,
            cbdb_dynasty   VARCHAR,
            old_lon        DOUBLE,
            old_lat        DOUBLE,
            lon            DOUBLE,
            lat            DOUBLE,
            addr_name      VARCHAR,
            addr_admin     VARCHAR,
            how            VARCHAR,
            batch          VARCHAR,
            ts             TIMESTAMP
        )""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多写多少条")
    a = ap.parse_args()

    db = kuzu.Database(str(KUZU_DB),
                       buffer_pool_size=int(2 * 1024 ** 3))
    con = kuzu.Connection(db)
    persons = load_graph_persons(con)
    log(f"图上人物 {len(persons):,}")

    idx = load_cbdb_index()
    log(f"CBDB 人物 {sum(len(v) for v in idx.values()):,}，"
        f"不同姓名（简体）{len(idx):,}")

    # ---- 分：同名唯一 / 同名多候选 ----
    uniq: list[tuple[dict, dict]] = []
    multi = Counter()
    miss = 0
    for p in persons:
        if p["external_ids"] not in ("", "{}"):
            continue                      # 已有锚点的不动
        cands = idx.get(_key(p["name"]))
        if not cands:
            miss += 1
            continue
        if len(cands) > 1:
            multi[p["name"]] = len(cands)
            continue
        uniq.append((p, cands[0]))
    log(f"\n同名唯一（姓名层唯一）: {len(uniq):,}")
    log(f"同名多候选（标待定不猜）: {sum(multi.values()):,} 条 / "
        f"{len(multi):,} 个姓名")
    log(f"CBDB 查无此人（多半是 CBDB 未收的先秦人物/书中别名）: {miss:,}")

    # ---- 年代闸门：同名还得同代 ----
    # 只靠姓名会把「岳飞」对上 CBDB 里一个高麗人、「唐蒙」对上隋人。CBDB 的
    # dynasty 字段又粗又不全（17 个取值里没有宋/金/元/遼，26 万人干脆为空），
    # 靠不住；能够落到实处的一致性只有年代。
    confirmed: list[tuple[dict, dict, str]] = []
    era_clash = dyn_ok = dyn_clash = dyn_unknown = 0
    for p, c in uniq:
        ys = p["years"]
        iy = c["index_year"]
        if ys and iy is not None:
            lo, hi = min(ys), max(ys)
            if lo - TOL <= iy <= hi + TOL:
                confirmed.append((p, c, f"year±{TOL}"))
            else:
                era_clash += 1
            continue
        # 没有年代可用时才退回朝代。图上只有 13/53151 人有生卒年，所以这条路
        # 才是主力；但它只能覆盖两边朝代都非空、且口径可比对的那一小部分。
        want = DYNASTY_MAP.get(p.get("dynasty_id", ""))
        got = c["dynasty"]
        if not (want and got):
            dyn_unknown += 1
        elif got in want:
            confirmed.append((p, c, "dynasty"))
            dyn_ok += 1
        else:
            dyn_clash += 1
    log(f"\n-- 一致性闸门 --")
    log(f"   年代吻合（±{TOL} 年）      : "
        f"{sum(1 for x in confirmed if x[2].startswith('year')):,}"
        f"　年代冲突: {era_clash:,}")
    log(f"   朝代吻合                : {dyn_ok:,}　"
        f"朝代冲突: {dyn_clash:,}　任一侧朝代为空: {dyn_unknown:,}")
    log(f"   → 合计可采信 {len(confirmed):,}")
    # ---- 补籍贯坐标（有就带回来，没有也照样落锚点）----
    sql = sqlite3.connect(str(CBDB_DB))
    hits, noref = [], 0
    for p, c, how in confirmed:
        h = pick_hometown(sql, c["id"].split(":")[-1], c["index_year"])
        if not h:
            noref += 1
            h = {"addr_name": "", "admin": "", "lon": None, "lat": None}
        hits.append({"person_id": p["id"], "name": p["name"],
                     "cbdb_id": c["id"], "cbdb_name": c["name"],
                     "cbdb_year": c["index_year"],
                     "cbdb_dynasty": c["dynasty"],
                     "old_lon": p["lon"], "old_lat": None,
                     "lon": h["lon"], "lat": h["lat"],
                     "addr_name": h["addr_name"], "addr_admin": h["admin"],
                     "how": how})
    sql.close()
    log(f"\n可落锚点 {len(hits):,} 人；其中带籍贯坐标 {len(hits) - noref:,}"
        f"（其余 {noref:,} 只有 ID 锚点）")

    log("\n-- 拟写入样例（前 15）--")
    for h in hits[:15]:
        xy = "(%.2f, %.2f)" % (h["lon"], h["lat"]) if h["lon"] is not None else "无坐标"
        log("   %-10s %-6s CBDB %-12s %-11s %-8s %s  [%s]" % (
            h["name"], h["cbdb_dynasty"] or "-", h["cbdb_id"],
            h["cbdb_year"] if h["cbdb_year"] is not None else "?",
            h["addr_name"] or "-", xy, h["how"]))
    import random
    log("\n-- 随机抽查 20 条（人工核对方才 --apply）--")
    for h in random.sample(hits, min(20, len(hits))):
        xy = "(%.2f, %.2f)" % (h["lon"], h["lat"]) if h["lon"] is not None else "无坐标"
        log("   %-10s %-6s CBDB %-12s %-11s %-8s %s  [%s]" % (
            h["name"], h["cbdb_dynasty"] or "-", h["cbdb_id"],
            h["cbdb_year"] if h["cbdb_year"] is not None else "?",
            h["addr_name"] or "-", xy, h["how"]))

    log("\n-- 同名待定样例（Top 8）--")
    for nm, n in multi.most_common(8):
        log("   %-8s CBDB 有 %3d 人 → 不猜" % (nm, n))

    if not a.apply:
        log("\n干跑结束，未写入。确认后加 --apply。")
        return 0

    # ---- 写入 ----
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    duck_con = duckdb.connect(str(DUCKDB_PATH))
    ensure_evidence_table(duck_con)
    done = {r[0] for r in duck_con.execute(
        "SELECT person_id FROM person_anchors").fetchall()}
    todo = [h for h in hits if h["person_id"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    log(f"\n开始写入 {len(todo):,} 条（已有 {len(done):,} 条跳过，幂等）")
    if not todo:
        duck_con.close()
        return 0

    wcon = kuzu.Connection(db)
    n = 0
    try:
        for h in todo:
            ext = json.dumps({"cbdb": h["cbdb_id"].split(":")[-1]},
                             ensure_ascii=False)
            if h["lon"] is None or h["old_lon"] is not None:
                # 没有坐标，或图上已有坐标（先前补过的不覆盖）：只挂锚点
                wcon.execute(
                    "MATCH (n:Person) WHERE n.id = $id "
                    "SET n.external_ids = $ext",
                    {"id": h["person_id"], "ext": ext})
            else:
                wcon.execute(
                    "MATCH (n:Person) WHERE n.id = $id "
                    "SET n.external_ids = $ext, n.lon = $lon, n.lat = $lat",
                    {"id": h["person_id"], "ext": ext,
                     "lon": h["lon"], "lat": h["lat"]})
            duck_con.execute(
                "INSERT INTO person_anchors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [h["person_id"], h["name"], h["cbdb_id"], h["cbdb_name"],
                 h["cbdb_year"], h["cbdb_dynasty"], h["old_lon"], h["old_lat"],
                 h["lon"], h["lat"], h["addr_name"], h["addr_admin"],
                 h["how"], batch, datetime.now()])
            n += 1
            if n % 2000 == 0:
                wcon = None
                wcon = kuzu.Connection(db)   # 定期重开，避免连接脏页堆积
                log(f"  已写 {n:,} 条")
    except Exception as e:
        log(f"✗ 写入中断于第 {n} 条：{type(e).__name__}: {e}")
        log("  已写入部分有证据记录，重跑自动跳过")
        duck_con.close()
        return 1
    chk = wcon.execute(
        "MATCH (n:Person) WHERE n.external_ids <> '' AND n.external_ids <> '{}' "
        "RETURN count(*)").get_next()[0]
    log(f"✓ 写入完成：人 {n:,} 条 + 证据 {n:,} 行（batch={batch}）")
    log(f"自检：图库内有外部锚点的人物 {chk:,}")
    duck_con.close()
    wcon = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
