# -*- coding: utf-8 -*-
"""离线本体构建 CLI（与在线共用同一条管线）

    parse → chunk → extract → normalize → align → score → proposals

两种用法：
1) 干跑（默认）：只解析/抽取，**不打开 Kuzu**，因此后端服务可以继续运行。
   python scripts/build_ontology.py --file 史记.txt --out build/shiji.json
   python scripts/build_ontology.py --dir app/data/books --out build/ --limit 3

2) 落库（--apply）：写入 Kuzu。Kuzu 是单写者库，**必须先停后端服务**，
   否则脚本会因拿不到锁而明确报错退出（不会破坏数据）。
   python scripts/build_ontology.py --file x.pdf --apply --mount

在线复用：
- 干跑产物 JSON 可直接 POST /api/ontology/import 导入并挂载；
- 在线 POST /api/ontology/build 走的是同一个 build_from_text，
  返回的 JSON 结构与本脚本 --out 完全一致。

可选：
- --llm：配置 OPENAI_API_KEY 时启用真实 LLM 抽取（否则一律启发式）；
- --known-json：载入主干实体快照做离线对齐（可用 --dump-known 导出）；
- --embed：LLM 模式下同时计算切块向量。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文/符号输出会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
_BACKEND_DIR = _HERE.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.ingestion.pipeline import (  # noqa: E402
    BuildOptions, build_from_path, build_from_text, known_from_store)
from app.ingestion.parse import SUPPORTED  # noqa: E402
from app.schema.ontology import Book  # noqa: E402


def _slug(title: str) -> str:
    return "of_" + hashlib.md5(title.encode("utf-8")).hexdigest()[:10]


def _collect(args) -> list[tuple[str, Path | None, str]]:
    """返回 [(title, path_or_None, text)]。"""
    out: list[tuple[str, Path | None, str]] = []
    if args.text:
        out.append((args.title or "命令行文本", None, args.text))
    for f in args.file or []:
        p = Path(f).expanduser().resolve()
        if not p.is_file():
            sys.exit(f"文件不存在：{p}")
        if p.suffix.lower() not in SUPPORTED:
            sys.exit(f"不支持的类型：{p.suffix}（支持 {sorted(SUPPORTED)}）")
        out.append((args.title or p.stem, p, ""))
    if args.dir:
        root = Path(args.dir).expanduser().resolve()
        if not root.is_dir():
            sys.exit(f"目录不存在：{root}")
        files = sorted(q for q in root.rglob("*")
                       if q.is_file() and q.suffix.lower() in SUPPORTED)
        if args.limit:
            files = files[:args.limit]
        if not files:
            sys.exit(f"目录内无可用文件：{root}")
        for p in files:
            out.append((p.stem, p, ""))
    if not out:
        out.append(("标准输入", None, sys.stdin.read()))
    return out


def _make_opts(args, embed_fn) -> BuildOptions:
    return BuildOptions(
        align_mode=args.align,
        min_confidence=args.min_confidence,
        max_persons=args.max_persons,
        max_events=args.max_events,
        max_extract_pieces=args.max_pieces or None,
        id_prefix=args.book_id or _slug(args.title or "offline"),
        person_id_mode="prefix" if args.align == "link" else "normalize",
        embed_fn=embed_fn)


def _run_build(sources, args, llm, known) -> list[tuple[str, object]]:
    """对每份输入跑一次管线，返回 [(title, BuildResult)]。"""
    embed_fn = None
    if args.embed and llm is not None and getattr(llm, "name", "stub") != "stub":
        embed_fn = llm.embed_texts
    opts = _make_opts(args, embed_fn)
    results = []
    for title, path, text in sources:
        if path is not None:
            res = build_from_path(path, known=known, llm=llm, opts=opts)
        else:
            res = build_from_text(text, known=known, llm=llm, opts=opts)
        results.append((title, res))
        print(f"· {title}: 切块 {len(res.chunks)} · 提案 {len(res.proposals)} "
              f"({res.stats.get('by_kind')}) · 模式 {res.mode}")
    return results


def _apply(results, args) -> None:
    from app.store import store  # 延迟导入：干跑时完全不触碰 Kuzu

    for title, res in results:
        bid = args.book_id or _slug(title)
        if any(b.id == bid for b in store.books) and not args.force:
            print(f"- 已存在，跳过：{bid}（--force 覆盖）")
            continue
        if any(b.id == bid for b in store.books):
            old = next(b for b in store.books if b.id == bid)
            store.uncommit_book(old)
            store.books = [b for b in store.books if b.id != bid]
            store.drop_chunks(bid)

        valid_dyns = {d.id for d in store.dynasties}
        did = args.dynasty_id or res.stats.get("dynasty_hint") or "qing"
        if did not in valid_dyns:
            did = "qing"
        ev_years = [p.year for p in res.proposals
                    if p.kind == "event" and p.year is not None]
        book = Book(
            id=bid, title=title[:40], author="（离线构建）",
            year=ev_years[0] if ev_years else 0,
            level=int(args.level), color="#0f766e", dynasty_id=did,
            span=(min(ev_years), max(ev_years)) if ev_years else (0, 0),
            tags=["离线构建", res.mode],
            description=f"离线管线构建：{len(res.chunks)} 切块 / "
                        f"{len(res.proposals)} 提案",
            extracts=res.proposals, mounted=False,
            origin="upload", file=None)
        with store.transaction():
            store.set_chunks(bid, res.chunks)
            store.books.append(book)
            if args.mount and res.proposals:
                store.commit_book(book, [True] * len(res.proposals),
                                  persist=False)
            store.save()
        print(f"✓ 已落库：《{title}》 id={bid} "
              f"mounted={book.mounted}（提案 {len(res.proposals)}）")


def _dump_known(path: str) -> None:
    from app.store import store  # 需要读主干，服务须停止

    snap = known_from_store(store)
    # 集合不可 JSON 序列化，统一转为列表（载入后用 in 判断，语义不变）
    snap = {k: (sorted(v) if isinstance(v, set) else v) for k, v in snap.items()}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"✓ 主干快照已导出：{p}"
          f"（人物 {len(snap['people'])} · 地点 {len(snap['places'])}）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="离线本体构建（parse→chunk→extract→align→proposals）")
    src = ap.add_argument_group("输入（四选一，可组合）")
    src.add_argument("--file", action="append", help="文件（txt/md/pdf/epub），可重复")
    src.add_argument("--dir", help="目录（递归扫描支持的类型）")
    src.add_argument("--limit", type=int, help="--dir 最多处理前 N 个文件")
    src.add_argument("--text", help="直接给一段文本")
    src.add_argument("--title", help="书名（默认取文件名）")

    out = ap.add_argument_group("输出")
    out.add_argument("--out", help="产物 JSON 路径（多输入时视为目录）")
    out.add_argument("--apply", action="store_true",
                     help="写入 Kuzu（必须先停后端服务）")
    out.add_argument("--mount", action="store_true", help="--apply 时直接全量挂载")
    out.add_argument("--force", action="store_true", help="--apply 时覆盖同名书")

    opt = ap.add_argument_group("管线参数")
    opt.add_argument("--align", choices=["link", "skip", "new"], default="new",
                     help="已知人物策略：link=转对齐提案 / skip=跳过 / new=新建（默认 new，离线无主干快照时等价于全新建）")
    opt.add_argument("--llm", action="store_true",
                     help="启用真实 LLM 抽取（需 OPENAI_API_KEY）")
    opt.add_argument("--embed", action="store_true", help="LLM 模式下计算切块向量")
    opt.add_argument("--known-json", help="主干实体快照（--dump-known 导出）")
    opt.add_argument("--dump-known", help="导出主干实体快照到该路径后退出")
    opt.add_argument("--min-confidence", type=float, default=0.0)
    opt.add_argument("--max-persons", type=int, default=200)
    opt.add_argument("--max-events", type=int, default=300)
    opt.add_argument("--max-pieces", type=int,
                     help="最多处理的抽取块数（LLM 控成本）")
    opt.add_argument("--book-id", help="指定书籍 id（默认按标题生成）")
    opt.add_argument("--level", type=int, default=2, help="史料层级 1/2/3")
    opt.add_argument("--dynasty-id", help="强制指定朝代 id")
    args = ap.parse_args()

    if args.dump_known:
        _dump_known(args.dump_known)
        return 0

    sources = _collect(args)

    known: dict = {}
    if args.known_json:
        known = json.loads(Path(args.known_json).read_text(encoding="utf-8"))

    llm = None
    if args.llm:
        from app.llm.base import get_llm
        llm = get_llm()

    results = _run_build(sources, args, llm, known)

    # ---- 产物落盘 ----
    if args.out:
        dest = Path(args.out)
        if len(results) == 1:
            title, res = results[0]
            p = res.write_json(dest, {"title": title})
            print(f"✓ 产物已写入：{p}")
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for title, res in results:
                p = res.write_json(dest / f"{_slug(title)}.json",
                                   {"title": title})
                print(f"✓ 产物已写入：{p}")

    # ---- 落库 ----
    if args.apply:
        try:
            _apply(results, args)
        except RuntimeError as e:
            if "lock" in str(e).lower() or "Could not set lock" in str(e):
                sys.exit("无法打开 Kuzu：后端服务仍在运行（单写者锁）。"
                         "请先停服务再 --apply；或去掉 --apply 只做干跑。")
            raise
    elif not args.out:
        print("\n（干跑：未写库、未落盘。加 --out 导出 JSON 或 --apply 落库）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
