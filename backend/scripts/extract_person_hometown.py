# -*- coding: utf-8 -*-
"""从 26 史原文里抽人物籍贯，再交给已验证的地名词典解坐标。

为什么走这条路
--------------
外部权威源给不出可靠籍贯：CBDB 的「基本地址」把曹操标在洛阳（实为亳州，差 324 km）、
诸葛亮标在成都执政地（实为琅琊阳都，差 1439 km），而且没有任何一类地址是籍贯
（诸葛亮的 type=3 是沂水，苏轼的 type=3 却是一串任职地）。见 §10.15。
而提案产物里 person.place **一条都没填**（61,627 条提案里 0 条）。

剩下的唯一可靠源就是原文本身——史书最有规律的一句就是「XX，YY人也」。做法：
1. 扫一遍数仓 chunks（36,848 条原文切片），用句式正则抓 (人名, 籍贯短语) 候选
2. 人名必须能在图里 61,355 个 Person 中对上（防止抓到随便一个词）
3. 籍贯短语交给 CBDB 地名词典解坐标（与事件坐标同一套判定，含后缀变体）
4. 同一个人出现多次：坐标一致的留下，不一致的算歧义丢弃
5. 用黄金集（14 位籍贯确凿的名人）打分，达标才写库

用法：
    python scripts/extract_person_hometown.py                 # 干跑 + 黄金集评分
    python scripts/extract_person_hometown.py --show 30       # 看候选样例
    python scripts/extract_person_hometown.py --apply         # 真写 Person.home/lon/lat
"""
from __future__ import annotations

import argparse
import json
import math
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config                                    # noqa: E402
from app.geo_rules import (addr_is_bad, suffix_variants,  # noqa: E402
                           prov_level, SUFFIX_BLOCK_ADMIN)
from zhconv import convert as zh_convert                  # noqa: E402

import duckdb                                             # noqa: E402
import kuzu                                               # noqa: E402
from enrich_event_coords import build_index               # noqa: E402

CBDB = str(config.DATA_DIR / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db")
DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
KUZU = str(config.DATA_DIR / "kg" / "kuzu_db")
OUT = (Path(config.DATA_DIR) / "core" / "normalized"
       / "person_hometown_candidates.jsonl")

# 籍贯句式：史书写人物开场白几乎都是这个模子
PATTERNS = [
    re.compile(r"([\u4e00-\u9fff]{2,4})[，,]\s*([\u4e00-\u9fff]{2,8}?)人也"),
    re.compile(r"([\u4e00-\u9fff]{2,4})(?:者)?[，,]\s*([\u4e00-\u9fff]{2,8}?)人[。．]"),
    re.compile(r"([\u4e00-\u9fff]{2,4})[，,][^。；]{0,10}?([\u4e00-\u9fff]{2,8}?)人也"),
    re.compile(r"([\u4e00-\u9fff]{2,4})[，,](?:其先|本贯|世居|望出|贯)([\u4e00-\u9fff]{2,8})"),
]

# 黄金集：籍贯在史学界没有争议的人物，用来给整套流程打分
GOLDEN = [
    ("曹操", 115.78, 33.87), ("诸葛亮", 118.40, 35.55), ("岳飞", 114.35, 35.93),
    ("王安石", 116.30, 27.95), ("司马迁", 110.45, 35.48), ("杜甫", 112.98, 34.75),
    ("苏轼", 103.83, 30.05), ("曾国藩", 112.20, 27.48), ("张居正", 112.24, 30.35),
    ("朱元璋", 117.56, 32.87), ("李白", 104.75, 31.78), ("欧阳修", 114.99, 27.12),
    ("司马光", 111.22, 35.14), ("范仲淹", 120.62, 31.31),
]
TOL_KM = 120.0
# 就近关联时，人名必须落在籍贯短语前面这么多字以内（再远就不是同一句了）
PROXIMITY = 50


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def hav(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def resolve_place(idx: dict[str, list[tuple]], raw: str):
    """籍贯短语 → (lon, lat, addr_id, 词条名, 档位)。只认强证据。"""
    s = (raw or "").strip()
    s = re.sub(r"[（(].*?[)）]", "", s)          # 去掉「（今河南上蔡）」这类注
    s = s.strip()
    if not s or len(s) < 2 or prov_level(s):
        return None
    cands: list[str] = []
    # 1) 整串
    cands.append(s)
    # 2) 剥掉行政前缀：沛国谯 → 谯；庐江舒人 → 舒
    for cut in range(1, min(4, len(s))):
        cands.append(s[cut:])
    # 3) 补后缀
    cands += suffix_variants(s)
    for tail in cands[:]:
        cands += suffix_variants(tail)
    seen = set()
    for name in cands:
        key = zh_convert(name, "zh-cn")
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        # 卫所条目（admin=Wei）是明清军事建制，与史书里的籍贯不是一回事：
        # 实测「颍川」被解到《潁川衛》(安徽阜阳 115.81,32.89)，而汉代颍川郡在禹州
        # (113.8,34.1)。人物籍贯一律不采用卫所条目。
        recs = [r for r in idx.get(key, [])
                if not addr_is_bad(r[5]) and (r[4] or "") not in SUFFIX_BLOCK_ADMIN]
        if not recs:
            continue
        coords = {(round(r[2], 3), round(r[3], 3)) for r in recs}
        if len(coords) == 1:
            r = recs[0]
            return r[2], r[3], r[5], r[6], ("exact" if name == s else "variant")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--tiers", default="pattern", choices=("pattern", "both"),
                    help="pattern=只认「XX，YY人也」句式（默认，精度高）；"
                         "both=再加就近关联（召回高一倍，但实测掺入约两成假阳性）")
    ap.add_argument("--rollback", action="store_true",
                    help="按证据表 person_hometown 把这次写的坐标全部退回 NULL")
    args = ap.parse_args()

    if args.rollback:
        duck = duckdb.connect(DUCK)
        n = duck.execute("SELECT count(DISTINCT person_id) "
                         "FROM person_hometown").fetchone()[0]
        log(f"回滚：待清 {n:,} 个 Person 坐标")
        wdb = kuzu.Database(KUZU)
        wc = kuzu.Connection(wdb)
        ids = [r[0] for r in duck.execute(
            "SELECT DISTINCT person_id FROM person_hometown").fetchall()]
        done = 0
        for pid in ids:
            wc.execute("MATCH (n:Person) WHERE n.id = $id "
                       "SET n.lon = NULL, n.lat = NULL", {"id": pid})
            done += 1
        duck.execute("DELETE FROM person_hometown")
        duck.close()
        ck = kuzu.Connection(wdb)
        left = ck.execute("MATCH (n:Person) WHERE n.lon IS NOT NULL "
                          "AND n.lon <> 0 RETURN count(*)").get_next()[0]
        log(f"✓ 已清 {done:,} 个；图内剩余有坐标 Person {left:,} 个"
            f"（应回到 13 个手写种子）")
        return 0

    log("构建 CBDB 地名词典…")
    idx = build_index()
    log(f"词典就绪：{len(idx):,} 个名字")

    log("读图内人物…")
    # 读写共用同一个 Database：Kuzu 是单写者（文件锁），先开只读再开写库会报
    # Could not set lock（踩过一次）。服务已停，这里直接独占。
    db = kuzu.Database(KUZU)
    c = kuzu.Connection(db)
    persons: dict[str, str] = {}                 # 名字 → 其中一个 id
    name_to_pids: dict[str, set[str]] = {}       # 名字 → 全部同名 id
    pid_to_names: dict[str, set[str]] = {}
    q = c.execute("MATCH (n:Person) RETURN n.id, n.name")
    while q.has_next():
        pid, nm = q.get_next()
        nm = (nm or "").strip()
        if not nm:
            continue
        forms = {nm}
        # 「秦始皇 嬴政」这类带空格/庙号的拆开也能对上
        forms |= {p for p in nm.replace("　", " ").split() if len(p) >= 2}
        for f in forms:
            persons.setdefault(f, pid)
            name_to_pids.setdefault(f, set()).add(pid)
            pid_to_names.setdefault(pid, set()).add(f)
    log(f"人物名字索引 {len(persons):,} 个；Person 节点 "
        f"{len(pid_to_names):,} 个（同名重复实体会被一起赋值）")

    log("扫原文切片…")
    con = duckdb.connect(DUCK, read_only=True)
    rows = con.execute("SELECT book_id, idx, text FROM chunks").fetchall()
    con.close()
    log(f"切片 {len(rows):,} 条")

    hits: dict[str, Counter] = defaultdict(Counter)      # pid → (坐标,词条) 计数
    phrase_of: dict[tuple, str] = {}
    n_pair = 0

    def record(pid: str, phrase: str, tag: str) -> None:
        nonlocal n_pair
        res = resolve_place(idx, phrase)
        if not res:
            return
        lon, lat, aid, aname, how = res
        key = (round(lon, 3), round(lat, 3))
        hits[pid][(key, aid, aname, f"{how}/{tag}")] += 1
        phrase_of[(pid, key)] = phrase
        n_pair += 1

    # 第二遍：史书的开场白常常是「太祖武皇帝，沛国谯人也」——名字与籍贯不挨着
    # （曹操二字根本没出现），句式正则抓不到。于是再扫一遍：段落里只有一个
    # 「YY人也/人。」短语、且人名出现在它前面 50 字内时，就近关联一次。
    tail_pat = re.compile(r"([\u4e00-\u9fff]{2,8}?)人(?:也|。|．)")
    name_lens = sorted({len(k) for k in persons if len(k) >= 2}, reverse=True)
    n_ctx = 0
    for book_id, cidx, text in rows:
        if not text:
            continue
        # 第一遍：句式
        for pat in PATTERNS:
            for m in pat.finditer(text):
                pid = persons.get(m.group(1))
                if pid:
                    record(pid, m.group(2), "pattern")
        if args.tiers != "both":
            continue
        # 第二遍：就近关联（默认关闭——抽样显示它把「自言泗州」「所以怀远」
        # 「后为太原」这类非籍贯叙述也抓了进来，误判约两成）
        ms = list(tail_pat.finditer(text))
        if len(ms) != 1:
            continue
        m = ms[0]
        start, end = m.start(), m.end()
        window = text[max(0, start - PROXIMITY):start]
        best = None
        for i in range(len(window) - 1):
            for L in name_lens:
                if i + L > len(window):
                    continue
                pid = persons.get(window[i:i + L])
                if pid:
                    # 越靠近短语越可信
                    if best is None or i > best[0]:
                        best = (i, pid, window[i:i + L])
        if best:
            record(best[1], m.group(1), "context")
            n_ctx += 1
    log(f"抓到 (人物, 籍贯坐标) 对 {n_pair:,} 条（其中就近关联 {n_ctx:,}），"
        f"涉及人物 {len(hits):,} 人")

    # 每个人的最终判定：坐标一致的留下，冲突的丢弃
    decided: dict[str, tuple] = {}
    ambig = 0
    for pid, counter in hits.items():
        coords = {(k[0]) for k in counter}
        if len(coords) > 1:
            # 取票数最高的，若次高接近就判歧义
            top = counter.most_common()
            if len(top) > 1 and top[1][1] >= top[0][1] * 0.6:
                ambig += 1
                continue
        (key, aid, aname, how), n = counter.most_common(1)[0]
        decided[pid] = (key[0], key[1], aid, aname, how, n,
                        phrase_of.get((pid, key), ""))
    log(f"可判定 {len(decided):,} 人（歧义丢弃 {ambig:,} 人）")

    # 黄金集评分
    ok = miss = wrong = 0
    print("\n=== 黄金集评分（14 位籍贯确凿的名人）===")
    for name, wlon, wlat in GOLDEN:
        pid = persons.get(name)
        if pid and pid in decided:
            lon, lat, aid, aname, how, n, ph = decided[pid]
            d = hav(lon, lat, wlon, wlat)
            mark = "✓" if d <= TOL_KM else "✗"
            if d <= TOL_KM:
                ok += 1
            else:
                wrong += 1
            print(f"  {name:<6} 抽到《{ph}》→ 《{aname}》{lon:.2f},{lat:.2f} "
                  f"{mark}{d:>7.0f}km  （{n} 次）")
        else:
            miss += 1
            print(f"  {name:<6} — 未抽到")
    total = len(GOLDEN)
    print(f"\n  命中 {ok + wrong}/{total}，其中正确 {ok}，错误 {wrong}，未抽到 {miss}")
    print(f"  精度 {ok / max(ok + wrong, 1) * 100:.0f}%   召回 {ok / total * 100:.0f}%")

    if args.show:
        print(f"\n=== 样例 {args.show} 条 ===")
        for i, (pid, v) in enumerate(list(decided.items())[:args.show]):
            nm = next((k for k, p in persons.items() if p == pid), pid)
            print(f"  {nm:<10} 籍贯《{v[6]}》→ 《{v[3]}》{v[0]:.2f},{v[1]:.2f} "
                  f"（{v[5]} 次, {v[4]}）")

    # 候选清单无论如何都落盘（派生产物，不碰图库），方便干跑阶段就抽查
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for pid, (lon, lat, aid, aname, how, n, ph) in decided.items():
            f.write(json.dumps({"person_id": pid, "place_phrase": ph,
                                "addr_id": aid, "addr_name": aname,
                                "lon": lon, "lat": lat, "how": how,
                                "votes": n}, ensure_ascii=False) + "\n")
    log(f"候选清单：{OUT}（{len(decided):,} 条）")

    if not args.apply:
        log("干跑结束（加 --apply 才写库）")
        return 0

    # 写库：只填原本没有坐标的人，绝不覆盖；同名重复实体一起赋上
    written = 0
    wdb = db
    wc = kuzu.Connection(wdb)
    duck = duckdb.connect(DUCK)
    duck.execute("""
        CREATE TABLE IF NOT EXISTS person_hometown (
            person_id VARCHAR, name VARCHAR, place_phrase VARCHAR,
            addr_id VARCHAR, addr_name VARCHAR, lon DOUBLE, lat DOUBLE,
            how VARCHAR, votes INTEGER, batch VARCHAR, ts TIMESTAMP)
    """)
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    for pid, (lon, lat, aid, aname, how, n, ph) in decided.items():
        # 同名的人一并赋值：图里同一个人常有多条实体（手工种子 + 各书挂载的副本）
        targets: set[str] = set()
        for nm in pid_to_names.get(pid, ()):
            targets |= name_to_pids.get(nm, set())
        for t in targets:
            try:
                wc.execute(
                    "MATCH (n:Person) WHERE n.id = $id AND "
                    "(n.lon IS NULL OR n.lon = 0) "
                    "SET n.lon = $lon, n.lat = $lat",
                    {"id": t, "lon": lon, "lat": lat})
                written += 1
            except Exception as e:
                log(f"  ✗ {t}: {type(e).__name__}: {e}")
            duck.execute(
                "INSERT INTO person_hometown VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [t, next(iter(pid_to_names.get(t, {""}))), ph, str(aid), aname,
                 lon, lat, how, n, batch, datetime.now()])
    duck.execute("CHECKPOINT")
    duck.close()
    log(f"✓ 写入 {written:,} 个 Person 节点坐标（batch={batch}）")

    # 自检
    ck = kuzu.Connection(wdb)
    total = ck.execute("MATCH (n:Person) WHERE n.lon IS NOT NULL AND n.lon <> 0 "
                       "RETURN count(*)").get_next()[0]
    log(f"自检：图内有坐标的 Person {total:,} 个（写前 13 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
