# -*- coding: utf-8 -*-
"""校验 emperors.json：与 seed.py 的 DYNASTIES span 对照，检查排序、越界、断档、重叠。

用法: python backend/scripts/validate_emperors.py          # 校验全部政权
      python backend/scripts/validate_emperors.py xia shang # 只校验指定政权
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.data import seed  # noqa: E402

TOL = 5       # span 容差（年）
GAP_WARN = 40  # 相邻断档告警阈值（年）
OVERLAP_WARN = 2  # 重叠告警阈值（年）


def validate(dyn_id: str, name: str, span: tuple[int, int], emps: list[dict]) -> list[str]:
    warns: list[str] = []
    if not emps:
        return warns
    lo, hi = span
    for i, e in enumerate(emps):
        tag = f"{dyn_id}[{i}] {e.get('n', '?')}"
        if not isinstance(e.get("s"), int) or not isinstance(e.get("e"), int):
            warns.append(f"{tag}: s/e 非整数 {e}")
            continue
        if e["s"] > e["e"]:
            warns.append(f"{tag}: 即位年 {e['s']} > 退位年 {e['e']}")
        if e["s"] < lo - TOL or e["s"] > hi + TOL:
            warns.append(f"{tag}: 即位年 {e['s']} 超出 span ({lo}~{hi})±{TOL}")
        if e["e"] < lo - TOL or e["e"] > hi + TOL:
            warns.append(f"{tag}: 退位年 {e['e']} 超出 span ({lo}~{hi})±{TOL}")
    starts = [e["s"] for e in emps if isinstance(e.get("s"), int)]
    if starts != sorted(starts):
        warns.append(f"{dyn_id}: 未按即位年升序排列")
    for i in range(len(emps) - 1):
        a, b = emps[i], emps[i + 1]
        if not all(isinstance(x.get(k), int) for x in (a, b) for k in ("s", "e")):
            continue
        gap = b["s"] - a["e"]
        if gap > GAP_WARN:
            warns.append(f"{dyn_id}: {a['n']}.e={a['e']} 与 {b['n']}.s={b['s']} 断档 {gap} 年（可能缺失）")
        overlap = a["e"] - b["s"]
        if overlap > OVERLAP_WARN:
            warns.append(f"{dyn_id}: {a['n']}.e={a['e']} 与 {b['n']}.s={b['s']} 重叠 {overlap} 年")
    return warns


def main() -> None:
    emp: dict[str, list[dict]] = json.loads(
        (BACKEND / "app" / "data" / "emperors.json").read_text(encoding="utf-8")
    )
    only = set(sys.argv[1:])
    total_warn = 0
    for d in seed.DYNASTIES:
        if only and d.id not in only:
            continue
        emps = emp.get(d.id, [])
        warns = validate(d.id, d.name, d.span, emps)
        total_warn += len(warns)
        status = "OK " if not warns else "WARN"
        print(f"[{status}] {d.id:<12} {d.name:<10} span={d.span[0]:>7}~{d.span[1]:<5} 帝王数={len(emps):>2}")
        for w in warns:
            print(f"       - {w}")
    print(f"\n共 {total_warn} 条 warning")


if __name__ == "__main__":
    sys.exit(main())
