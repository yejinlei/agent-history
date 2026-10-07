"""把图里 Person 锚定到 CBDB（external_ids.cbdb）。

匹配策略（保守、可判定）：
  1. 先在 CBDB 全量人物上建 name -> [候选] 索引；
  2. 图里每个 Person 按姓名取候选：
     - 候选唯一 → 直接锚定；
     - 候选多个 → 用「朝代 + 生卒/指数年」降歧义；仍多义则记为 ambiguous，不锚定；
  3. external_ids 以 JSON 字符串写回（与库中现有格式一致：'{"cbdb": "31725"}'）。

回滚：每次 --apply 前把「本次将写入的 (person_id, 旧值, 新值)」导出到
      data/align_cbdb_undo_<ts>.jsonl，可用 --undo <file> 还原。

用法：
  python scripts/align_cbdb.py                # 干跑，只报统计
  python scripts/align_cbdb.py --apply        # 写库
  python scripts/align_cbdb.py --undo <file>  # 回滚
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.config import DATA_DIR  # noqa: E402

import kuzu  # noqa: E402

KG = os.path.join(DATA_DIR, "kg", "kuzu_db")
CBDB_GLOB = os.path.join(DATA_DIR, "core", "normalized", "cbdb_persons_*.jsonl")
OUT_DIR = os.path.join(DATA_DIR, "kg")

# 图里 dynasty_id -> CBDB dynasty 名的常见写法；用于多候选降歧义
_DYN_ALIAS = {
    "qin": "秦", "xihan": "汉", "han": "汉", "xin": "新", "donghan": "汉",
    "sanguo": "三国", "wei": "魏", "shu": "蜀", "wu": "吴",
    "xijin": "晋", "jin": "晋", "dongjin": "晋",
    "song": "宋", "qi": "齐", "liang": "梁", "chen": "陈",
    "beiwei": "北魏", "dongwei": "东魏", "xiwei": "西魏",
    "beijing": "北齐", "beizhou": "北周", "nanbei": "南北朝",
    "sui": "隋", "tang": "唐", "wuwei": "武周",
    "liao": "辽", "jin_d": "金", "song_n": "宋", "xiang": "宋",
    "yuan": "元", "ming": "明", "qing": "清",
}


try:
    from zhconv import convert as _zhconv
except ImportError:  # 没有 zhconv 时退化为原样匹配
    def _zhconv(s, _t="zh-cn"):
        return s


def _norm(s: str) -> str:
    return (s or "").strip()


def _simp(s: str) -> str:
    """繁→简归一化，用于跨字形匹配（CBDB 与书中人名繁简混用）。"""
    return _zhconv(_norm(s), "zh-cn")


def load_cbdb() -> tuple[dict[str, list[dict]], int]:
    idx: dict[str, list[dict]] = defaultdict(list)
    n = 0
    for fp in sorted(glob.glob(CBDB_GLOB)):
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n += 1
                nm = _norm(d.get("name"))
                if not nm:
                    continue
                rec = {
                    "cbdb": str(d.get("external_ids", {}).get("cbdb")
                                or str(d.get("id", "")).replace("cbdb:", "")),
                    "dyn": _norm(d.get("dynasty")),
                    "y": _int(d.get("index_year") or d.get("year_birth")),
                    "yb": _int(d.get("year_birth")),
                    "yd": _int(d.get("year_death")),
                }
                idx[nm].append(rec)
                s = _simp(nm)
                if s != nm:
                    idx[s].append(rec)
    return idx, n


def _int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError, AttributeError):
        return None


_YEAR_RE = __import__("re").compile(r"-?\d{1,4}")


def _years_span(years: str | None) -> tuple[int | None, int | None]:
    """从 '1514—1587' / '前145-前87' 之类里取出生卒年。"""
    if not years:
        return None, None
    nums = [int(x) for x in _YEAR_RE.findall(str(years))]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], None
    return nums[0], nums[1]


def _near(a: int | None, b: int | None, tol: int = 40) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def pick(cands: list[dict], dynasty_id: str | None,
         years: str | None) -> tuple[str | None, str]:
    """返回 (cbdb_id, 判定)：unique / by_dynasty / by_year / ambiguous。"""
    if len(cands) == 1:
        return cands[0]["cbdb"], "unique"

    # 1) 朝代降歧义
    dyn = _DYN_ALIAS.get((dynasty_id or "").lower())
    if dyn:
        sub = [c for c in cands if dyn in (c["dyn"] or "")]
        if len(sub) == 1:
            return sub[0]["cbdb"], "by_dynasty"
        if len(sub) > 1:
            cands = sub

    # 2) 生卒年降歧义：图里人物的生年需与 CBDB 指数年/生年接近
    y0, y1 = _years_span(years)
    if y0 is not None:
        for key in ("y", "yb", "yd"):
            sub = [c for c in cands if _near(y0, c.get(key))]
            if len(sub) == 1:
                return sub[0]["cbdb"], "by_year"
            if len(sub) > 1:
                cands = sub
                break
    return None, "ambiguous"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认干跑）")
    ap.add_argument("--undo", metavar="FILE", help="按导出的撤销文件回滚")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试）")
    ap.add_argument("--batch", type=int, default=100, help="写库批大小")
    args = ap.parse_args()

    print(f"数据根: {DATA_DIR}")
    t0 = time.time()
    idx, n_cbdb = load_cbdb()
    print(f"CBDB 载入 {n_cbdb:,} 人 / {len(idx):,} 个名字，用时 {time.time()-t0:.1f}s")

    conn = kuzu.Connection(kuzu.Database(KG, read_only=not args.apply and not args.undo))

    if args.undo:
        rows = [json.loads(l) for l in open(args.undo, encoding="utf-8") if l.strip()]
        done = 0
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            for r in chunk:
                conn.execute(
                    "MATCH (p:Person {id:$id}) SET p.external_ids=$v",
                    {"id": r["person_id"], "v": r["old"]},
                )
            done += len(chunk)
            print(f"  回滚 {done}/{len(rows)}", end="\r")
        print(f"\n回滚完成 {done} 条")
        return 0

    print("扫描图中 Person ...")
    res = conn.execute(
        "MATCH (p:Person) RETURN p.id, p.name, p.dynasty_id, p.external_ids, p.years")
    people = []
    while res.has_next():
        people.append(res.get_next())
    print(f"图中 Person {len(people):,}")

    plan = []
    stat = defaultdict(int)
    for pid, name, dyn, ext, years in people:
        if args.limit and len(plan) >= args.limit:
            break
        nm = _norm(name)
        if not nm:
            stat["no_name"] += 1
            continue
        try:
            cur = json.loads(ext) if isinstance(ext, str) and ext.strip() else (ext or {})
        except json.JSONDecodeError:
            cur = {}
        if isinstance(cur, dict) and cur.get("cbdb"):
            stat["already"] += 1
            continue
        # 候选名：原名 / 简体名 / 空格或括号拆出的别名（如「秦始皇 嬴政」→「嬴政」）
        trials = []
        for cand in (nm, _simp(nm)):
            if cand and cand not in trials:
                trials.append(cand)
        if " " in nm:
            for part in nm.split():
                for cand in (part, _simp(part)):
                    if cand and cand not in trials:
                        trials.append(cand)
        cands, hit = None, None
        for t in trials:
            cands = idx.get(t)
            if cands:
                hit = t
                break
        if not cands:
            stat["no_match"] += 1
            continue
        cbdb, verdict = pick(cands, dyn, years)
        if not cbdb:
            stat["ambiguous"] += 1
            continue
        stat[verdict] += 1
        new = dict(cur) if isinstance(cur, dict) else {}
        new["cbdb"] = cbdb
        plan.append({"person_id": pid, "name": nm, "old": ext if isinstance(ext, str) else json.dumps(cur, ensure_ascii=False),
                     "new": json.dumps(new, ensure_ascii=False), "verdict": verdict})

    total = len(people)
    print("\n== 统计 ==")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:12s} {v:8,}")
    print(f"  待写入     {len(plan):8,}")
    print(f"  覆盖率     {len(plan)/max(1,total)*100:.1f}% (不含已锚定的重复计算)")
    covered = len(plan) + stat["already"]
    print(f"  锚定后总覆盖率 {covered/max(1,total)*100:.1f}%")
    print("\n样例:")
    for p in plan[:10]:
        print("  ", p["name"], "->", p["new"], f"({p['verdict']})")

    if not args.apply:
        print("\n[干跑] 未写库。加 --apply 生效。")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    undo_fp = os.path.join(OUT_DIR, f"align_cbdb_undo_{ts}.jsonl")
    with open(undo_fp, "w", encoding="utf-8") as fh:
        for p in plan:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\n撤销文件: {undo_fp}")

    done = 0
    for i in range(0, len(plan), args.batch):
        chunk = plan[i:i + args.batch]
        try:
            for p in chunk:
                conn.execute(
                    "MATCH (p:Person {id:$id}) SET p.external_ids=$v",
                    {"id": p["person_id"], "v": p["new"]},
                )
        except Exception as e:  # noqa: BLE001
            print(f"\n批次 {i} 失败（已写 {done} 条）: {e}")
            print(f"可用 --undo {undo_fp} 回滚")
            return 1
        done += len(chunk)
        print(f"  写入 {done}/{len(plan)}", end="\r")
    print(f"\n写入完成 {done} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
