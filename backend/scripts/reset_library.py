# -*- coding: utf-8 -*-
"""书库基线整理：把「启动即自动挂载」的书批量卸载，回到干净基线。

背景：旧版本启动时会把 books/ 下 27 本书一次性切块并自动挂载。
逐本叠加要求从干净基线开始 —— 一本跑通、人工确认、再下一本。

    python scripts/reset_library.py --list                    # 看哪些书已挂载
    python scripts/reset_library.py --unmount-all --dry-run   # 只看计划不动手
    python scripts/reset_library.py --unmount-all --keep shiji,资治通鉴,读通鉴论

走 HTTP API（不直接开 Kuzu），因此**服务需要处于运行状态**。
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib import request as urlreq

BASE = "http://127.0.0.1:8000"


def _get(path: str) -> dict:
    with urlreq.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    req = urlreq.Request(f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
                         headers={"Content-Type": "application/json"},
                         method="POST")
    with urlreq.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description="整理书库基线（批量卸载）")
    ap.add_argument("--list", action="store_true", help="列出已挂载的书")
    ap.add_argument("--unmount-all", action="store_true",
                    help="卸载全部已挂载的书（可用 --keep 保留）")
    ap.add_argument("--keep", default="", help="保留不卸载的书，逗号分隔 book_id")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    ap.add_argument("--yes", action="store_true",
                    help="跳过交互确认（非交互环境/脚本调用用）")
    args = ap.parse_args()

    try:
        books = _get("/api/books")["books"]
    except Exception as e:
        sys.exit(f"无法连接后端（{BASE}）：{e}\n请先启动服务："
                 "python -m uvicorn app.main:app")

    mounted = [b for b in books if b["mounted"]]
    if args.list or not args.unmount_all:
        print(f"共 {len(books)} 本书，其中已挂载 {len(mounted)}：")
        for b in mounted:
            print(f"  {b['id']:<16}{b['title']}")
        if not args.unmount_all:
            print("\n加 --unmount-all [--keep id1,id2] [--dry-run] 执行卸载")
        return

    keep = {x.strip() for x in args.keep.split(",") if x.strip()}
    targets = [b for b in mounted if b["id"] not in keep]
    print(f"计划卸载 {len(targets)} 本，保留 {len(mounted) - len(targets)} 本")
    for b in targets:
        print(f"  - {b['id']}《{b['title']}》")
    if args.dry_run:
        print("（--dry-run：未执行）")
        return
    if not args.yes:
        ans = input("确认执行？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消")
            return
    ok, fail = 0, 0
    for b in targets:
        try:
            _post("/api/mounts/unmount", {"book_id": b["id"]})
            ok += 1
            print(f"  已卸载 {b['id']}")
        except Exception as e:
            fail += 1
            print(f"  卸载失败 {b['id']}：{e}")
    print(f"完成：成功 {ok}，失败 {fail}")
    print("下一步：停服跑 scripts/ingest_one.py <book_id> --limit 100，"
          "或在线 POST /api/ingest/{book_id}，跑通一本再下一本。")


if __name__ == "__main__":
    main()
