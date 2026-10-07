# -*- coding: utf-8 -*-
"""放开 TopN 上限：用已落盘的抽取结果重算提案并重挂某本书。

背景：`bookrun.RunOptions.max_events / max_persons` 默认各 10000。大部头
（宋史 34004 条候选事件、明史 27537、旧唐书 18082）被这条上限截掉大半，
实际只挂了 10000 条。上限不进 `_signature`（指纹只认切块窗口与原文），
所以改它不需要重跑 LLM —— `--finalize-only` 重算即可。

用法（服务须停止：独占 Kuzu 写锁）：
    python scripts/remount_events.py songshi --dry-run
    python scripts/remount_events.py songshi --max-events 60000 --max-persons 60000
    python scripts/remount_events.py 宋史列表 --plan default   # 批量按表跑

流程：卸载旧挂载 → 重算提案（不调 LLM）→ 覆盖 Book.extracts → 重新提交挂载。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion.bookrun import RunOptions, run_book  # noqa: E402

# 实测候选量（extract_raw 里的候选条目数），上限取到候选量之上即可全量保留
KNOWN_CAP = {
    "songshi": 34_004, "mingshi": 27_537, "jiutangshu": 18_082,
    "资治通鉴": 28_727, "yuanshi": 9_198, "jinshi": 8_999,
    "jinshu": 7_684, "weishu": 7_077, "hanshu": 6_464,
    "jiusidai": 6_052, "houhanshu": 5_816, "xintangshu": 4_129,
}


def _plan_books(name: str) -> list[str]:
    """预设批次：只重算被截断的书（候选量明显多于已挂载数）。"""
    if name == "truncated":
        return ["songshi", "mingshi", "jiutangshu"]
    if name == "all":
        return sorted(KNOWN_CAP, key=lambda b: -KNOWN_CAP[b])
    if name == "default":
        return ["songshi", "mingshi", "jiutangshu", "资治通鉴"]
    sys.exit(f"未知批次：{name}（可选 truncated / all / default）")


def main() -> None:
    ap = argparse.ArgumentParser(description="放开上限重算并重挂")
    ap.add_argument("book_id", nargs="?", help="书 ID；省略时配合 --plan 批量")
    ap.add_argument("--plan", default="", help="预设批次：truncated / all / default")
    ap.add_argument("--max-events", type=int, default=60_000)
    ap.add_argument("--max-persons", type=int, default=60_000)
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="提案置信度下限（默认不改，保持与已有挂载一致）")
    ap.add_argument("--dry-run", action="store_true", help="只算提案不落库")
    ap.add_argument("--auto-cap", action="store_true",
                    help="按实测候选量自动抬高上限（默认严格用 --max-* 的值）")
    args = ap.parse_args()

    books = _plan_books(args.plan) if args.plan else [args.book_id]
    if not args.plan and not args.book_id:
        sys.exit("需要指定 book_id 或 --plan")

    for bid in books:
        cap = KNOWN_CAP.get(bid)
        # 默认严格服从手传值：机器可用提交内存只有几 GB 时，提案越多越容易
        # 在 DuckDB 批量落盘时 OOM（那条异常被 catch 成 warn，表现为「挂载了
        # 却没写进图库」），所以宁可分小批，也不要一次梭哈。
        mx = max(args.max_events,
                 int((cap or 0) * 1.2)) if args.auto_cap else args.max_events
        mp = max(args.max_persons, mx) if args.auto_cap else args.max_persons
        print(f"\n=== 《{bid}》 候选约 {cap or '未知'} "
              f"→ 上限 events={mx} persons={mp} ===")
        # 注意：save_raw / dry_run 任意一个为假，收口就会跳过「合并历史落盘
        # 结果」，提案直接变成 0 条。所以这里两者都必须保持默认的写模式，
        # 本脚本的 --dry-run 只控制「是否动 Kuzu」。
        opts = RunOptions(
            finalize_only=True,   # 不调 LLM，只用已落盘的 raw 重算
            save_raw=True,        # 必须 True：否则不合并已有 raw → 提案清零
            max_events=mx, max_persons=mp,
            min_confidence=args.min_confidence,
            dry_run=False, out=None)
        res = run_book(bid, opts=opts)
        print(f"  提案 {len(res.proposals)}  {res.by_kind}  "
              f"模式 {res.mode} / 模型 {res.model}")
        integ = res.integrity or {}
        if integ and not integ.get("complete"):
            print(f"  [警告] 抽取未跑全：{integ.get('done')}/{integ.get('total')}"
                  f" 块（{integ.get('coverage')}%）")
        if args.dry_run or not res.proposals:
            print("  --dry-run：不落库" if args.dry_run else "  无提案，跳过")
            continue

        # ---- 落库：先卸旧挂载，再按新提案重新提交 ----
        from app.store import store  # 延迟导入：干跑时不碰 Kuzu

        book = next((b for b in store.books if b.id == bid), None)
        if book is None:
            print(f"  [跳过] 图库里没有这本书（{bid}）：先跑常规构建流程")
            continue
        before = len(store.events)
        if book.mounted:
            store.uncommit_book(book)
            print(f"  已卸载旧挂载：事件 {before} → {len(store.events)}")
        book.extracts = res.proposals
        with store.transaction():
            added = store.commit_book(book, [True] * len(res.proposals),
                                      persist=False)
            store.save()
        after = len(store.events)
        print(f"  commit 明细: {added}")
        print(f"  ✓ 重挂完成：事件 {before} → {after}（+{after - before}），"
              f"人物 {len(store.people)}，mounted={book.mounted}")
        if after <= before and res.proposals:
            print("  [严重] 事件数没有增加：多半是落盘时 OOM 了"
                  "（DuckDB/Kuzu 的分配失败会被上层 catch 成告警，"
                  "内存 redo 的结果不会写进图库）。先停掉占内存的程序再重跑。")


if __name__ == "__main__":
    main()
