# -*- coding: utf-8 -*-
"""批量向量化切块（本地 ONNX 模型 → DuckDB）

背景：当前 LLM 服务不提供 embedding 模型，RAG 只能走 BM25 关键词检索。
本脚本用本地模型把切块转成向量写回数仓，使检索升级为语义检索。

模型：BAAI/bge-small-zh-v1.5（512 维，90MB），存放于项目根 models/。
首次下载若直连 HuggingFace 超时，可设置 HF_ENDPOINT=https://hf-mirror.com。

用法（DuckDB/Kuzu 均为单写者，**必须先停后端服务**）：
    python scripts/embed_chunks.py                      # 全量
    python scripts/embed_chunks.py --books shiji,zztj   # 只处理指定书
    python scripts/embed_chunks.py --books shiji --limit 200   # 抽样试跑

实测速度约 48 条/秒（CPU），36848 条全量约 13 分钟。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import config                      # noqa: E402
if config.HF_ENDPOINT:
    os.environ.setdefault("HF_ENDPOINT", config.HF_ENDPOINT)

from app.llm.local_embed import local_embedder    # noqa: E402
from app.warehouse import warehouse               # noqa: E402

# 向量保留 4 位小数：余弦检索精度不受影响，体积约降 40%
_PRECISION = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模型批量向量化切块")
    ap.add_argument("--books", default="",
                    help="只处理指定书（逗号分隔 book_id），默认全部")
    ap.add_argument("--limit", type=int, default=0,
                    help="每本最多处理多少条（0=不限），用于抽样试跑")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--skip-done", action="store_true",
                    help="跳过已有向量的切块")
    args = ap.parse_args()

    if not local_embedder.available():
        print("× 本地模型不可用。请确认 models/ 下已有 .onnx 文件，"
              "或运行一次下载（HF_ENDPOINT=https://hf-mirror.com）。")
        return 1

    books = [b.strip() for b in args.books.split(",") if b.strip()]
    all_books = sorted({b for b, _ in warehouse.all_chunks()})
    if not books:
        books = all_books
    else:
        unknown = [b for b in books if b not in all_books]
        if unknown:
            print("× 未知 book_id:", unknown)
            print("  可选:", ", ".join(all_books))
            return 1

    print(f"模型 {local_embedder.model_name}（{local_embedder.dim} 维）"
          f" | 待处理 {len(books)} 本")
    t_all = time.time()
    total_vec = 0

    for bid in books:
        # 注意：--limit 只限制「本次处理多少条」，写回时必须是**全量**切块，
        # 否则 replace_chunks（先删后插）会把未处理的切块整本丢掉。
        all_chunks = warehouse.chunks_of(bid)
        targets = ([c for c in all_chunks if not c.get("embedding")]
                   if args.skip_done else all_chunks)
        if args.limit:
            targets = targets[:args.limit]
        if not targets:
            print(f"· {bid}: 跳过（无待处理切块）")
            continue

        t0 = time.time()
        vecs = local_embedder.embed_texts([c["text"] for c in targets],
                                          batch_size=args.batch_size)
        for c, v in zip(targets, vecs):
            c["embedding"] = [round(float(x), _PRECISION) for x in v]
        warehouse.replace_chunks(bid, all_chunks)

        dt = time.time() - t0
        total_vec += len(targets)
        print(f"· {bid}: {len(targets)} 条  {dt:.1f}s "
              f"({len(targets) / max(dt, 0.01):.0f} 条/秒)")

    print(f"\n✓ 共向量化 {total_vec} 条，用时 {time.time() - t_all:.1f}s")
    print("  重启后端即可生效（检索将走 vector 模式，失败自动回退 BM25）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
