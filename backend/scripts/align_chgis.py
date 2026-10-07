"""把图里 Place 锚定到 CHGIS（external_ids.chgis）。

CHGIS v3 的 main3 表给的是「地名 + 今地 + 起止年」，与图中的 Place（古名 /
今名并存）按名匹配：
  1. 先用 Place.name（古名）精确匹配；
  2. 再用 Place.modern / Place.ancient 里的今地名匹配；
  3. 多个候选取唯一，仍多义则记为 ambiguous，不锚定。

与 align_cbdb.py 一样：先干跑报统计，--apply 才写库，写入前导出撤销文件。

用法：
  python scripts/align_chgis.py              # 干跑
  python scripts/align_chgis.py --apply      # 写库
  python scripts/align_chgis.py --undo FILE  # 回滚
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
CHGIS = os.path.join(DATA_DIR, "core", "normalized", "chgis_places.jsonl")
OUT_DIR = os.path.join(DATA_DIR, "kg")

# 「今河北xx」→「河北xx」，用于从 Place.modern 里取出可匹配的地名
_PREFIX_RE = re.compile(r"^今?\s*")


def _key(s: str) -> str:
    s = (s or "").strip()
    s = _PREFIX_RE.sub("", s)
    # 「陕西西安（古称X）」只取括号前部分
    for sep in ("（", "(", "/", "，"):
        if sep in s:
            s = s.split(sep)[0].strip()
    return s


def load_chgis() -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    n = 0
    with open(CHGIS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            nm = (d.get("name") or "").strip()
            if not nm:
                continue
            rec = {"chgis": str(d.get("external_ids", {}).get("chgis") or nm),
                   "kind": d.get("kind") or "",
                   "yb": d.get("year_beg"), "yd": d.get("year_end")}
            for k in {nm, _key(d.get("modern") or "")}:
                if k:
                    idx[k].append(rec)
    return idx, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="FILE")
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    idx, n = load_chgis()
    print(f"CHGIS 载入 {n:,} 条 / {len(idx):,} 个地名键")

    conn = kuzu.Connection(
        kuzu.Database(KG, read_only=not args.apply and not args.undo))

    if args.undo:
        rows = [json.loads(l) for l in open(args.undo, encoding="utf-8") if l.strip()]
        for i, r in enumerate(rows, 1):
            conn.execute("MATCH (p:Place {id:$id}) SET p.external_ids=$v",
                         {"id": r["place_id"], "v": r["old"]})
            if i % 100 == 0:
                print(f"  回滚 {i}/{len(rows)}", end="\r")
        print(f"\n回滚完成 {len(rows)} 条")
        return 0

    res = conn.execute(
        "MATCH (p:Place) RETURN p.id, p.name, p.ancient, p.modern, p.external_ids")
    places = []
    while res.has_next():
        places.append(res.get_next())
    print(f"图中 Place {len(places):,}")

    plan, stat = [], defaultdict(int)
    for pid, name, ancient, modern, ext in places:
        try:
            cur = json.loads(ext) if isinstance(ext, str) and ext.strip() else (ext or {})
        except json.JSONDecodeError:
            cur = {}
        if isinstance(cur, dict) and cur.get("chgis"):
            stat["already"] += 1
            continue
        trials = []
        for raw in (name, modern, ancient):
            for k in (raw, _key(raw or "")):
                if k and k not in trials:
                    trials.append(k)
        cands = None
        for t in trials:
            cands = idx.get(t)
            if cands:
                break
        if not cands:
            stat["no_match"] += 1
            continue
        if len(cands) > 1:
            stat["ambiguous"] += 1
            continue
        new = dict(cur) if isinstance(cur, dict) else {}
        new["chgis"] = cands[0]["chgis"]
        plan.append({"place_id": pid, "name": name,
                     "old": ext if isinstance(ext, str) else json.dumps(cur, ensure_ascii=False),
                     "new": json.dumps(new, ensure_ascii=False)})
        stat["unique"] += 1

    total = len(places)
    print("\n== 统计 ==")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:10s} {v:7,}")
    cov = len(plan) + stat["already"]
    print(f"  待写入   {len(plan):7,}   锚定后覆盖率 {cov/max(1,total)*100:.1f}%")
    for p in plan[:12]:
        print("  ", p["name"], "->", p["new"])

    if not args.apply:
        print("\n[干跑] 未写库。")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    undo_fp = os.path.join(OUT_DIR, f"align_chgis_undo_{ts}.jsonl")
    with open(undo_fp, "w", encoding="utf-8") as fh:
        for p in plan:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\n撤销文件: {undo_fp}")

    done = 0
    for i in range(0, len(plan), args.batch):
        chunk = plan[i:i + args.batch]
        try:
            for p in chunk:
                conn.execute("MATCH (p:Place {id:$id}) SET p.external_ids=$v",
                             {"id": p["place_id"], "v": p["new"]})
        except Exception as e:  # noqa: BLE001
            print(f"\n批次 {i} 失败（已写 {done}）: {e}\n--undo {undo_fp}")
            return 1
        done += len(chunk)
        print(f"  写入 {done}/{len(plan)}", end="\r")
    print(f"\n写入完成 {done} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
