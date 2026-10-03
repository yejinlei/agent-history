# -*- coding: utf-8 -*-
"""逐本增量构建 CLI —— 一次只处理一本书（离线，需先停服）。

    python scripts/ingest_one.py --list
    python scripts/ingest_one.py shiji --limit 100          # 先跑 100 块样本
    python scripts/ingest_one.py shiji --limit 100 --llm    # 用 .env 里的大模型抽取
    python scripts/ingest_one.py shiji                      # 放开全书（续跑，只补未完成的块）
    python scripts/ingest_one.py shiji --status             # 只看进度与缺口，不跑
    python scripts/ingest_one.py shiji --reset              # 清空进度后重跑
    python scripts/ingest_one.py shiji --llm --repair       # 自动多轮补齐（只补缺口块）
    python scripts/ingest_one.py shiji --llm --repair --strict  # 跑不完整就报错（退出码 2）

为什么要停服：DuckDB（切块/进度）与 Kuzu（图谱）都是**单进程写**，
服务运行时本脚本无法打开同一个库文件。

流程（与在线完全同构，产物可互相复用）：
    切块 → 逐块抽取（进度落 DuckDB）→ 提案 → data/ingest/<book_id>.json
    →（有 --store 时）写入 Book.extracts，**不自动挂载**，等界面人工确认。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

API_BASE = "http://127.0.0.1:8000"


def _server_alive() -> bool:
    from urllib import request as urlreq
    try:
        with urlreq.urlopen(f"{API_BASE}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _print_status(bid: str | None, store) -> None:
    from app.ingestion.bookrun import book_status, queue_status
    from app.warehouse import warehouse
    rows = ([book_status(bid, store=store)] if bid
            else queue_status(store=store))
    print(f"{'书ID':<14}{'状态':<10}{'块(完成/总)':<14}{'进度':<8}"
          f"{'提案':<8}{'模型':<30}{'挂载':<6}缺口(缺/空/退/错)")
    for r in rows:
        gap = "-"
        if r["model"] and r["pieces"]:
            rep = warehouse.piece_report(r["book_id"], r["model"], r["pieces"])
            gap = (f"{rep['missing_n']}/{rep['empty_n']}/"
                   f"{rep['fallback_n']}/{rep['error_n']}")
        print(f"{r['book_id']:<14}{r['stage']:<10}"
              f"{str(r['done']) + '/' + str(r['pieces']):<14}"
              f"{str(r['percent']) + '%':<8}{r['proposals']:<8}"
              f"{r['model'] or '-':<30}{'是' if r['mounted'] else '否':<6}{gap}")


def main() -> None:
    ap = argparse.ArgumentParser(description="逐本增量构建（离线）")
    ap.add_argument("book_id", nargs="?", help="books_manifest.json 中的书 ID")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多处理的抽取块数（先小样本）")
    ap.add_argument("--llm", action="store_true",
                    help="用 .env 配置的大模型抽取（默认启发式，离线可用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有切块与进度，整本重跑")
    ap.add_argument("--reset", action="store_true", help="先清空该书的抽取进度")
    ap.add_argument("--reset-failed", action="store_true",
                    help="只清空「抽取为空」的块（补跑用，成功的块不重跑）")
    ap.add_argument("--embed", action="store_true", help="切块时算向量（可选）")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="LLM 抽取并发数（默认 6；1=串行）")
    ap.add_argument("--batch", type=int, default=1,
                    help="每次 LLM 调用送入的抽取块数（默认 1）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--no-store", action="store_true",
                    help="不打开 Kuzu：只写 DuckDB 与产物 JSON")
    ap.add_argument("--list", action="store_true", help="列出全部内置书与进度")
    ap.add_argument("--status", action="store_true", help="只看进度不跑")
    ap.add_argument("--out", help="产物 JSON 路径（默认 data/ingest/<book_id>.json）")
    ap.add_argument("--finalize-only", action="store_true",
                    help="只重算提案：复用已落盘的抽取结果，不调 LLM（改 TopN/阈值用）")
    ap.add_argument("--max-persons", type=int, default=10000)
    ap.add_argument("--max-events", type=int, default=10000)
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="提案置信度下限（配合 --finalize-only 可零成本调阈值）")
    ap.add_argument("--retry-rounds", type=int, default=1,
                    help="补齐轮数（>1 等价于 --repair，每轮只跑缺口块）")
    ap.add_argument("--repair", action="store_true",
                    help="自动补齐：跑完一轮就重算缺口再补，直到无缺口或达 --max-rounds")
    ap.add_argument("--max-rounds", type=int, default=3,
                    help="--repair 最多补几轮（默认 3）")
    ap.add_argument("--retries", type=int, default=2,
                    help="单块失败重试次数（异常 / 空响应各算一次，默认 2）")
    ap.add_argument("--strict", action="store_true",
                    help="覆盖不完整（还有没跑/报错的块）就以退出码 2 结束")
    ap.add_argument("--no-retry-empty", action="store_true",
                    help="补跑时跳过「抽到空」的块（默认会补）")
    ap.add_argument("--no-retry-fallback", action="store_true",
                    help="补跑时跳过「大模型回退启发式」的块（默认会补）")
    ap.add_argument("--no-keep-best", action="store_true",
                    help="关掉产物保护：提案变少也覆盖旧产物")
    args = ap.parse_args()

    if not args.dry_run and not args.list and _server_alive():
        sys.exit("检测到服务正在运行（单写者锁）。请先停服再跑离线构建，"
                 "或改用在线入口：POST /api/ingest/{book_id}")

    from app.ingestion.bookrun import RunOptions, run_book
    from app.warehouse import warehouse

    store = None
    if not args.no_store:
        from app.store import store as _store
        store = _store

    if args.list or args.status or not args.book_id:
        if not args.book_id and not args.list:
            ap.error("请给出 book_id，或用 --list 查看队列")
        _print_status(args.book_id if args.status else None, store)
        return

    bid = args.book_id
    if args.reset:
        n = warehouse.reset_progress(bid)
        print(f"已清空《{bid}》的抽取进度：{n} 条")
    if args.reset_failed:
        # 精确清掉台账里记过的失败块，成功块保留（一般不必手动做：
        # 正常跑一轮就会自动识别并补跑这些块）
        n = 0
        for m in (warehouse.extract_models(bid) or ["heuristic"]):
            fails = warehouse.failed_pieces(bid, m)
            n += warehouse.reset_pieces(bid, m, list(fails.keys()))
        print(f"已清空《{bid}》失败块的进度：{n} 条（成功块保留，不重跑）")

    opts = RunOptions(limit=args.limit, use_llm=args.llm, force=args.force,
                      embed=args.embed, dry_run=args.dry_run, out=args.out,
                      concurrency=max(1, args.concurrency),
                      pieces_per_call=max(1, args.batch),
                      finalize_only=args.finalize_only,
                      max_persons=args.max_persons,
                      max_events=args.max_events,
                      min_confidence=args.min_confidence,
                      retries=max(1, args.retries),
                      retry_empty=not args.no_retry_empty,
                      retry_fallback=not args.no_retry_fallback,
                      keep_best=not args.no_keep_best,
                      strict=args.strict)
    rounds = max(1, args.retry_rounds) if not args.finalize_only else 1
    if rounds > 1 or args.repair:
        opts.repair = True
        opts.max_rounds = max(1, args.max_rounds if args.repair else rounds)

    shown = 0

    def _prog(n: int, total: int) -> None:
        nonlocal shown
        if total and (n - shown >= 5 or n == total):
            shown = n
            print(f"  抽取中 {n}/{total}", flush=True)

    try:
        res = run_book(bid, store=store, opts=opts, on_progress=_prog)
    except RuntimeError as e:          # --strict 且覆盖不完整
        print(f"[不完整] {e}")
        sys.exit(2)
    print(f"[ok] 《{res.title}》({res.book_id})  mode={res.mode} model={res.model}")
    print(f"  切块 {res.chunks} · 抽取块 {res.done}/{res.pieces}"
          f"（本次处理 {res.processed}）")
    print(f"  提案 {len(res.proposals)} {res.by_kind}")
    if res.model != "heuristic":
        print(f"  大模型块 {res.stats.get('llm_pieces')} · 回退启发式块 "
              f"{res.stats.get('fallback_pieces')} · 空块 "
              f"{res.stats.get('empty_pieces')} · 报错块 "
              f"{res.stats.get('error_pieces')}")
    rep = res.integrity or {}
    if rep:
        print(f"  完整性 覆盖 {rep.get('done')}/{rep.get('total')}"
              f"（{rep.get('coverage')}%，本次范围；全书 {res.pieces} 块）"
              f" · 原始结果 {rep.get('raw')}"
              f" · 缺块 {rep.get('missing_n')} · 缺结果 {rep.get('no_raw_n')}"
              f" · 空 {rep.get('empty_n')} · 回退 {rep.get('fallback_n')}"
              f" · 报错 {rep.get('error_n')}")
        if not rep.get("complete"):
            miss = rep.get("missing") or []
            print("  [不完整] 还有块没跑成功。重跑同一条命令即可续跑；"
                  "或加 --repair 自动补齐多轮"
                  + (f"（缺口前 20 块：{miss[:20]}）" if miss else ""))
    if res.stats.get("kept_old_artifact"):
        print("  [提示] 保留了更大的旧产物；确认要覆盖请加 --force")
    if res.artifact:
        print(f"  产物 {res.artifact}")
    if store is not None and not args.dry_run:
        print("  已写入 Book.extracts；挂载请在界面「挂载书库」确认，"
              "或用 API /api/mounts/start → /api/mounts/commit")


if __name__ == "__main__":
    main()
