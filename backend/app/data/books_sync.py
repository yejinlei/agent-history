# -*- coding: utf-8 -*-
"""内置古籍目录同步器（**增量**：一次只处理显式指定的书）。

books_manifest.json 里的书是「待构建队列」，**不会**在启动时被整体切块并挂载。
逐本叠加的两个入口：
- 离线：python scripts/ingest_one.py <book_id> --limit 100（需先停服）
- 在线：POST /api/ingest/{book_id}（后台线程），挂载仍由人工确认

抽取与切块统一走 app.ingestion.bookrun，与离线 CLI、在线 API 同一套实现。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

# 允许直接作为脚本运行，无需外部 PYTHONPATH
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.ingestion.bookrun import RunOptions, run_book
from app.store import Store

_DATA_DIR = Path(__file__).resolve().parent
_BOOKS_DIR = _DATA_DIR / "books"
_MANIFEST_PATH = _DATA_DIR / "books_manifest.json"


def _load_manifest() -> dict[str, dict]:
    if not _MANIFEST_PATH.exists():
        return {}
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def sync_builtin_books(
    store: Store,
    bids: Iterable[str] | None = None,
    verbose: bool = True,
    *,
    auto_mount: bool = False,
    limit: int | None = None,
    use_llm: bool = False,
) -> dict:
    """按 bids 逐本构建（幂等：已在库中的书跳过）。

    - 默认只构建并生成提案，**不挂载**（auto_mount=False），挂载由人确认；
    - bids 为 None 时**不**自动展开为全部书目 —— 逐本叠加要求显式指定，
      全量构建会同时污染图谱与进度，出问题无法定位到某一本。
    """
    manifest = _load_manifest()
    existing_ids = {b.id for b in store.books}
    synced: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []

    target_bids = list(bids) if bids is not None else []
    if not target_bids:
        if verbose:
            print("[books_sync] 未指定书目，跳过（逐本叠加：请显式给出 book_id）")
        return {"synced": [], "skipped": list(manifest),
                "missing_files": []}

    for bid in target_bids:
        meta = manifest.get(bid)
        if meta is None:
            skipped.append(bid)
            if verbose:
                print(f"[books_sync] {bid} not in manifest, skip")
            continue
        if bid in existing_ids:
            skipped.append(bid)
            if verbose:
                print(f"[books_sync] {bid} already in store, skip")
            continue

        file_name = meta.get("file", f"{bid}.txt")
        if not (_BOOKS_DIR / file_name).exists():
            missing.append(bid)
            if verbose:
                print(f"[books_sync] {bid} file missing: {file_name}")
            continue

        if verbose:
            print(f"[books_sync] building {bid} ...")
        # 逐本走统一编排器：切块 → 逐块抽取（可续跑）→ 提案 → Book.extracts
        res = run_book(bid, store=store,
                       opts=RunOptions(limit=limit, use_llm=use_llm,
                                       align_mode="link"))
        if verbose:
            print(f"[books_sync] {bid} chunks={res.chunks} "
                  f"pieces={res.done}/{res.pieces} "
                  f"proposals={len(res.proposals)} mode={res.mode}")

        book = next(b for b in store.books if b.id == bid)
        if auto_mount:
            store.commit_book(book, [True] * len(book.extracts),
                              persist=False)
            if verbose:
                print(f"[books_sync] {bid} auto-mounted")
        synced.append(bid)

    if synced and auto_mount:
        store.save()
        if verbose:
            print(f"[books_sync] saved {len(synced)} book(s) to Kuzu")
    return {"synced": synced, "skipped": skipped, "missing_files": missing}


if __name__ == "__main__":
    from app.store import store

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = args[0].split(",") if args else None
    auto = "--mount" in sys.argv
    lim = next((int(a.split("=")[1]) for a in sys.argv
                if a.startswith("--limit=")), None)
    stats = sync_builtin_books(store, selected, auto_mount=auto, limit=lim)
    print(stats)
