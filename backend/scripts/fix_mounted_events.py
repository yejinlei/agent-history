# -*- coding: utf-8 -*-
"""一次性数据修正：挂载事件的坐标占位与朝代归属（docs/PLAN.md Phase 2）

实测暴露的两个问题：
1) `commit_book` 曾把缺失坐标填为 0.0 → 所有无坐标事件堆在 (0,0)（几内亚湾）。
   现改为 None，地图层跳过渲染。
2) 挂载事件统一使用 `book.dynasty_id` → 《资治通鉴》这类通史跨十余朝，
   整本书标一个朝代会让纪年归属全错。现按事件年份推导所属朝代。

用法（Kuzu 单写者，必须先停后端服务）：
    python scripts/fix_mounted_events.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.store import store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="修正挂载事件的坐标与朝代归属")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落库")
    args = ap.parse_args()

    n_coord = 0
    n_dyn = 0
    dyn_samples: list[tuple[str, str, str]] = []

    for e in store.events:
        # 1) 坐标占位 0.0 → None（真正的 (0,0) 地点在史料场景中不存在）
        if e.lon == 0.0 and e.lat == 0.0:
            e.lon = None
            e.lat = None
            n_coord += 1

        # 2) 挂载事件：按年份重推朝代（仅主时间线 lane=0 参与定位）
        if getattr(e, "from_book", None) and e.year:
            try:
                did = store.dynasty_at(int(e.year)).id
            except Exception:
                continue
            if did != e.dynasty_id:
                dyn_samples.append((e.name[:16], e.dynasty_id, did))
                e.dynasty_id = did
                n_dyn += 1

    total = len(store.events)
    located = sum(1 for e in store.events if e.lon is not None)
    print(f"事件总数 {total}")
    print(f"· 坐标 0.0 → None：{n_coord} 条（修正后仍有坐标 {located} 条，"
          f"无坐标 {total - located} 条将在地图上跳过）")
    print(f"· 朝代按年份重推：{n_dyn} 条")
    for name, old, new in dyn_samples[:8]:
        print(f"    {name}：{old} → {new}")
    if len(dyn_samples) > 8:
        print(f"    … 另 {len(dyn_samples) - 8} 条")

    if args.dry_run:
        print("\n（--dry-run：未落库）")
        return 0

    store.save()
    print("\n✓ 已落库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
