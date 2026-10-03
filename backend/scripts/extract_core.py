# -*- coding: utf-8 -*-
"""从原始主干数据派生精简表（normalized/），供导入与对齐使用。

原始数据体量很大（CBDB 53 万人物 / CHGIS 数十万地名），
直接全量灌进图库会让每次 save()（全量重写 Kuzu）变得很慢，
所以这里先「瘦身」成统一字段的精简表，再由 import_core.py 按需导入。

产物（体积可控、可 diff、入库）：
    normalized/cbdb_persons.jsonl      每个人的 id / 中文名 / 生卒 / 朝代 / external_ids
    normalized/wikidata_anchors.jsonl  Wikidata QID ↔ CBDB ID 交叉锚点
    normalized/chgis_places.jsonl      历史地名（名称 / 坐标 / 起止年）

用法：
    python scripts/extract_core.py                 # 全量派生
    python scripts/extract_core.py --only cbdb      # 只派生指定源
    python scripts/extract_core.py --limit 5000     # 每个源最多 N 条（试跑）
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_CORE = _BACKEND / "app" / "data" / "core"
_RAW = _CORE / "raw"
_OUT = _CORE / "normalized"


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def write_jsonl(name: str, rows: list[dict], shard: int = 0) -> list[Path]:
    """写 JSONL；shard>0 时按行数分片，避免单文件过大（便于入库/diff）。"""
    _OUT.mkdir(parents=True, exist_ok=True)
    if not shard or len(rows) <= shard:
        parts = [(name, rows)]
    else:
        parts = [(f"{name}_{i // shard + 1:03d}", rows[i:i + shard])
                 for i in range(0, len(rows), shard)]
    out = []
    for nm, chunk in parts:
        p = _OUT / f"{nm}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for r in chunk:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"{nm}: {len(chunk)} 行 → {p.name}（{p.stat().st_size/1e6:.2f} MB）")
        out.append(p)
    return out


# ---------------------------------------------------------------- CBDB
def extract_cbdb(limit: int) -> None:
    db = _RAW / "cbdb" / "CBDB_20240208_sqlite.db"
    if not db.exists():
        log("cbdb: 原始库不存在，先跑 fetch_core.py")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = {c[1] for c in con.execute("PRAGMA table_info(BIOG_MAIN)")}
    has = lambda *cs: [c for c in cs if c in cols]

    # 朝代区间（用于按指数年推导所属朝代）
    dyns = []
    try:
        dyns = [(r[0], r[1], r[2], r[3]) for r in con.execute(
            "SELECT c_dynasty_chn, c_start, c_end, c_dy FROM DYNASTIES")]
    except Exception as e:
        log(f"cbdb: 朝代表不可用 {e.__class__.__name__}")

    def dynasty_of(year: int | None) -> str:
        if not year:
            return ""
        for name, s, e, _ in dyns:
            try:
                if s and e and int(s) <= year <= int(e):
                    return name or ""
            except Exception:
                continue
        return ""

    sel = has("c_personid", "c_name_chn", "c_name", "c_birthyear",
              "c_deathyear", "c_index_year", "c_female")
    q = f"SELECT {', '.join(sel)} FROM BIOG_MAIN"
    if limit:
        q += f" LIMIT {limit}"
    out = []
    for row in con.execute(q):
        d = dict(zip(sel, row))
        pid = d.get("c_personid")
        name = (d.get("c_name_chn") or d.get("c_name") or "").strip()
        if not name or name in ("未詳", "未详"):
            continue
        birth = d.get("c_birthyear")
        death = d.get("c_deathyear")
        idx = d.get("c_index_year")
        out.append({
            "id": f"cbdb:{pid}",
            "name": name,
            "year_birth": int(birth) if isinstance(birth, int) and birth else None,
            "year_death": int(death) if isinstance(death, int) and death else None,
            "index_year": int(idx) if isinstance(idx, int) and idx else None,
            "dynasty": dynasty_of(idx if isinstance(idx, int) else None),
            "female": bool(d.get("c_female")) if d.get("c_female") is not None else None,
            "external_ids": {"cbdb": str(pid)},
        })
    write_jsonl("cbdb_persons", out, shard=150_000)


# ---------------------------------------------------------------- Wikidata
def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


def _year(v: str | None) -> int | None:
    if not v:
        return None
    m = re.match(r"(-?\d{1,4})", v)
    return int(m.group(1)) if m else None


def extract_wikidata(limit: int) -> None:
    rows: list[dict] = []
    for fn in ("persons_anchor.json", "persons_cn.json"):
        p = _RAW / "wikidata" / fn
        if not p.exists():
            continue
        for b in json.loads(p.read_text(encoding="utf-8")):
            qid = _qid((b.get("p") or {}).get("value", ""))
            name = (b.get("pLabel") or {}).get("value", "")
            cbdb = (b.get("cbdb") or {}).get("value")
            if not qid or not name:
                continue
            ext = {"wikidata": qid}
            if cbdb:
                ext["cbdb"] = cbdb
            rows.append({
                "id": f"wd:{qid}", "name": name,
                "year_birth": _year((b.get("birth") or {}).get("value")),
                "year_death": _year((b.get("death") or {}).get("value")),
                "external_ids": ext,
                "source": "anchor" if cbdb else "cn",
            })
            if limit and len(rows) >= limit:
                break
    write_jsonl("wikidata_anchors", rows)


# ---------------------------------------------------------------- CHGIS
def _mysql_cols(create_sql: str) -> list[str]:
    """从 CREATE TABLE 语句里按行取列名（跳过 PRIMARY KEY 之类）。"""
    cols = []
    for line in create_sql.split("\n"):
        line = line.strip()
        if not line or line.upper().startswith(
                ("PRIMARY", "KEY", "UNIQUE", "INDEX", "CONSTRAINT")):
            continue
        m = re.match(r"`?(\w+)`?\s+\w+", line)
        if m:
            cols.append(m.group(1))
    return cols


def _split_tuples(s: str) -> list[str]:
    """按顶层括号切分 INSERT 的多组 VALUES（引号内的括号不算）。"""
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
    """解析一组 VALUES 内容为字符串列表（处理引号内逗号与转义）。"""
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


def extract_chgis(limit: int) -> None:
    z = _RAW / "chgis" / "v3_mysql_distrib.zip"
    if not z.exists():
        log("chgis: 原始 zip 不存在")
        return
    with zipfile.ZipFile(z) as zf:
        target = next((n for n in zf.namelist()
                       if n.endswith("main3_dump.sql")), None)
        if not target:
            log(f"chgis: zip 内无 main3_dump.sql：{zf.namelist()}")
            return
        head = zf.open(target).read(200_000).decode("utf-8", "replace")
        m = re.search(r"CREATE TABLE \w+ \((.*?)\n\)", head, re.S)
        if not m:
            log("chgis: 未找到 CREATE TABLE，待人工核对")
            return
        cols = _mysql_cols(m.group(1))
        log(f"chgis: main3 列 {cols}")
        idx = {c: i for i, c in enumerate(cols)}
        pick = ("name_utf", "name_py", "pres_loc", "beg_yr", "end_yr",
                "type_utf", "data_src")
        have = [c for c in pick if c in idx]
        out, seen = [], set()
        with zf.open(target) as f:
            for raw in f:
                line = raw.decode("utf-8", "replace")
                if not line.startswith("INSERT INTO"):
                    continue
                body = line[line.find("VALUES") + 6:]
                for tup in _split_tuples(body):
                    vals = _mysql_values(tup)
                    if len(vals) < len(cols):
                        continue
                    nm = vals[idx["name_utf"]] or vals[idx.get("name_py", 0)]
                    if not nm:
                        continue
                    key = (nm, vals[idx.get("pres_loc", 0)])
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {c: vals[idx[c]] for c in have}
                    beg = vals[idx.get("beg_yr", 0)] if "beg_yr" in idx else ""
                    end = vals[idx.get("end_yr", 0)] if "end_yr" in idx else ""
                    out.append({
                        "id": f"chgis:{len(out)}",
                        "name": nm,
                        "modern": row.get("pres_loc") or "",
                        "pinyin": row.get("name_py") or "",
                        "kind": row.get("type_utf") or "",
                        "year_beg": int(beg) if beg.lstrip("-").isdigit() else None,
                        "year_end": int(end) if end.lstrip("-").isdigit() else None,
                        "external_ids": {"chgis": nm},
                    })
                    if limit and len(out) >= limit:
                        break
                if limit and len(out) >= limit:
                    break
    write_jsonl("chgis_places", out, shard=150_000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*",
                    choices=["cbdb", "wikidata", "chgis"])
    ap.add_argument("--limit", type=int, default=0, help="每源最多 N 条（0=不限）")
    a = ap.parse_args()
    want = set(a.only or ["cbdb", "wikidata", "chgis"])
    if "cbdb" in want:
        extract_cbdb(a.limit)
    if "wikidata" in want:
        extract_wikidata(a.limit)
    if "chgis" in want:
        extract_chgis(a.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
