# -*- coding: utf-8 -*-
"""给 CHGIS 地名帐补经纬度（派生新文件，不改现有 normalized/）。

背景
----
现有 core_places（12.2 万，已在 DuckDB 里）**没有经纬度**：当年 extract_core.py
派生时只挑了 name_py/name_utf/pres_loc/beg_yr/end_yr/type_utf/data_src，把坐标
连同可回连的主键一起丢了，id 又是自增序号 chgis:N，眼看无法反查。

坐标其实一直在原始包里：gis_info_dump.sql 的 x_coord / y_coord，主键 sys_id；
main3 的 pt_id / pgn_id / line_id 指向它。探针（scripts/_chgis_probe.py）实测：
**pt_id → sys_id 命中 122,072 / 122,075（100.0%）**。

为什么能对准现有表
------------------
这里逐字复刻 extract_core.extract_chgis 的行序与去重键 (name_utf, pres_loc)，
因此第 N 行必然对应 core_places 的 chgis:N。**写盘前逐行比对 name**：只要有一行
对不上就整体放弃并退出——错位会让 12 万条坐标全部张冠李戴，比没有坐标更糟。

产物
----
normalized/chgis_coords.jsonl  每行 {id, name, lon, lat, xy_type, obj_type,
                                     beg_yr, end_yr, how}
幂等：可反复重跑；已存在同名文件会被覆盖（校验不通过则**不会**覆盖）。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config

ZIP = Path(config.DATA_DIR) / "core" / "raw" / "chgis" / "v3_mysql_distrib.zip"
OUT_DIR = Path(config.DATA_DIR) / "core" / "normalized"
DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


# ---- MySQL dump 解析器（与 extract_core.py 同一套，保证行序一致）----
def _split_tuples(s: str) -> list[str]:
    out, depth, buf, i, in_q = [], 0, [], 0, False
    while i < len(s):
        c = s[i]
        if in_q:
            buf.append(c)
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i + 1])
                i += 2
                continue
            if c == "'":
                in_q = False
        elif c == "'":
            in_q = True
            buf.append(c)
        elif c == "(":
            depth += 1
            if depth == 1:
                buf = []
            else:
                buf.append(c)
        elif c == ")":
            depth -= 1
            if depth == 0:
                out.append("".join(buf))
                buf = []
            else:
                buf.append(c)
        elif depth >= 1:
            buf.append(c)
        i += 1
    return out


def _mysql_values(s: str) -> list[str]:
    out, buf, i, in_q = [], [], 0, False
    while i < len(s):
        c = s[i]
        if in_q:
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i + 1])
                i += 2
                continue
            if c == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_q = False
                i += 1
                continue
            buf.append(c)
        elif c == "'":
            in_q = True
        elif c == ",":
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf).strip())
    return [("" if v.upper() == "NULL" else v) for v in out]


def cols_of(txt: str) -> list[str]:
    m = re.search(r"CREATE TABLE\s+`?(\w+)`?\s*\((.*?)\n\)", txt, re.S)
    if m is None:
        return []
    out = []
    for p in [x.replace("\r", "").strip() for x in m.group(2).split(",")]:
        if p.upper().startswith(("PRIMARY", "KEY", "UNIQUE", "INDEX",
                                "CONSTRAINT", "FULLTEXT")):
            continue
        mm = re.match(r"`?(\w+)`?\s+\w+", p)
        if mm:
            out.append(mm.group(1))
    return out


def iter_rows(zf, member: str, cols: list[str]):
    idx = {c: i for i, c in enumerate(cols)}
    with zf.open(member) as f:
        for raw in f:
            line = raw.decode("utf-8", "replace")
            if not line.startswith("INSERT INTO"):
                continue
            body = line[line.find("VALUES") + 6:]
            for tup in _split_tuples(body):
                vals = _mysql_values(tup)
                if len(vals) < len(cols):
                    continue
                yield {c: vals[idx[c]] for c in cols}


def _in_china(x: str, y: str) -> bool:
    """CHGIS 覆盖中国及周边；越界的坐标宁可不用（错标比缺标更难发现）。"""
    try:
        return 73.0 <= float(x) <= 136.0 and 3.0 <= float(y) <= 54.0
    except Exception:
        return False


def main() -> int:
    if not ZIP.exists():
        log(f"原始 ZIP 不存在：{ZIP}")
        return 2
    z = zipfile.ZipFile(ZIP)
    g_cols = cols_of(z.open("mysql_distrib/gis_info_dump.sql")
                     .read(400_000).decode("utf-8", "replace"))
    m_cols = cols_of(z.open("mysql_distrib/main3_dump.sql")
                     .read(400_000).decode("utf-8", "replace"))
    if not g_cols or not m_cols:
        log("列名解析失败，中止")
        return 2

    # ---- 1. gis_info → sys_id 索引（保留首个；越界坐标丢弃）----
    by_sys: dict[str, dict] = {}
    by_name: dict[tuple, list[dict]] = {}
    dropped = 0
    for r in iter_rows(z, "mysql_distrib/gis_info_dump.sql", g_cols):
        x, y = r.get("x_coord", ""), r.get("y_coord", "")
        if not x or not y:
            continue
        if not _in_china(x, y):
            dropped += 1
            continue
        rec = {"lon": round(float(x), 5), "lat": round(float(y), 5),
               "xy_type": r.get("xy_type", ""), "obj_type": r.get("obj_type", ""),
               "beg_yr": r.get("beg_yr", ""), "end_yr": r.get("end_yr", "")}
        sid = (r.get("sys_id") or "").strip()
        if sid and sid not in by_sys:
            by_sys[sid] = rec
        key = ((r.get("name_utf") or "").strip(), (r.get("pres_loc") or "").strip())
        if key[0]:
            by_name.setdefault(key, []).append(rec)
    log(f"gis_info：sys_id 索引 {len(by_sys):,} 条，(名,今地) 索引 {len(by_name):,} 条"
        f"（越界坐标丢弃 {dropped} 条）")

    # ---- 2. main3 同序派生（复刻 extract_core 的行序与去重键）----
    rows: list[dict] = []
    seen: set = set()
    how_stat: dict[str, int] = {}
    no_geo = 0
    for r in iter_rows(z, "mysql_distrib/main3_dump.sql", m_cols):
        nm = (r.get("name_utf") or r.get("name_py") or "").strip()
        if not nm:
            continue
        key = (nm, r.get("pres_loc") or "")
        if key in seen:
            continue
        seen.add(key)
        rec = None
        how = ""
        for fk in ("pt_id", "pgn_id", "line_id"):     # 与探针同一优先级
            v = (r.get(fk) or "").strip()
            if v and not rec and v in by_sys:
                rec, how = by_sys[v], fk
        if rec is None:
            # 退回同名 + 同今地；多条时取年代最贴近的
            cand = by_name.get(((r.get("name_utf") or "").strip(),
                                (r.get("pres_loc") or "").strip()))
            if cand:
                try:
                    byr = int(r.get("beg_yr") or 0)
                except Exception:
                    byr = 0
                rec = min(cand, key=lambda c: abs(
                    (int(c["beg_yr"]) if str(c["beg_yr"]).lstrip("-").isdigit() else 0) - byr))
                how = "name"
        if rec is None:
            # 关键：没有坐标也必须**占位**，不能 continue。
            # 一旦跳过，后面所有行的 chgis:N 就会与 core_places 错位，12 万条
            # 坐标挂到别人名下——这种错误极难发现，只能靠行数/逐行校验兜住。
            no_geo += 1
            rows.append({
                "id": f"chgis:{len(rows)}", "name": nm,
                "modern": r.get("pres_loc") or "",
                "lon": None, "lat": None,
                "xy_type": "", "obj_type": "", "beg_yr": "", "end_yr": "",
                "how": "none",
            })
            continue
        how_stat[how] = how_stat.get(how, 0) + 1
        rows.append({
            "id": f"chgis:{len(rows)}",
            "name": nm,
            "modern": r.get("pres_loc") or "",
            "lon": rec["lon"], "lat": rec["lat"],
            "xy_type": rec["xy_type"], "obj_type": rec["obj_type"],
            "beg_yr": rec["beg_yr"], "end_yr": rec["end_yr"],
            "how": how,
        })
    log(f"main3 派生 {len(rows):,} 条有坐标（无坐标 {no_geo} 条），来源 {how_stat}")

    # ---- 3. 与现有 core_places 逐行校验：只要错位就整体放弃 ----
    import duckdb
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    # 注意：必须按 id 里的**数字**排序。字符串序会把 chgis:1, chgis:10, chgis:100
    # 排在 chgis:2 前面，导致整表比对全错——校验自己的顺序别写错。
    ref = con.execute(
        "SELECT id, name, modern FROM core_places "
        "ORDER BY CAST(regexp_extract(id, '[0-9]+$') AS BIGINT)").fetchall()
    log(f"core_places 现有 {len(ref):,} 行，开始逐行比对…")
    if len(ref) != len(rows):
        log(f"✗ 行数不一致：core_places {len(ref):,} vs 派生 {len(rows):,} —— 放弃写盘")
        return 3
    bad_n = bad_m = 0
    for (rid, rname, rmod), new in zip(ref, rows):
        if rid != new["id"]:
            bad_n += 1
        if rname != new["name"]:
            bad_n += 1
        if (rmod or "") != new["modern"]:
            bad_m += 1
    if bad_n:
        log(f"✗ 有 {bad_n} 行 name/id 对不上 —— 行序已错位，放弃写盘")
        return 3
    log(f"✓ 12 万行 name/id 全部对齐（modern 字段差异 {bad_m} 行，不影响坐标）")

    # ---- 4. 写盘（先写临时文件，成功后再替换，避免半截文件）----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final = OUT_DIR / "chgis_coords.jsonl"
    tmp = OUT_DIR / "chgis_coords.jsonl.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if final.exists():
        bak = OUT_DIR / "chgis_coords.jsonl.bak"
        final.replace(bak)
    tmp.replace(final)
    size = final.stat().st_size
    log(f"✓ 写出 {final.name}：{len(rows):,} 行 / {size / 1e6:.2f} MB"
        f"（旧版本已留 .bak）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
