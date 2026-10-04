# -*- coding: utf-8 -*-
"""单本书的增量编排器（离线 CLI 与在线 API 共用同一份实现）

设计目标：**一本一本地叠加**，而不是一次性全量构建。

    data/books/<id>.txt
        → 切块（DuckDB chunks，已有则跳过）
        → 逐抽取块抽取（chunk 级进度写 DuckDB，中断后重跑只补未完成的块）
        → 汇总提案（对齐主干）→ 产物 JSON + Book.extracts
        → 由人在界面/API 确认后挂载（写入 Kuzu）

幂等与续跑：
- 切块以 book_id 为单位整本替换，重复导入不产生双份；
- 抽取进度按 (book_id, piece_idx, model) 记录，换模型自动重跑；
- 已挂载的书不允许直接重跑（先卸载），避免提案与已写入子图不一致。
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.ingestion.extract import extract
from app.ingestion.pipeline import (
    BuildOptions, BuildResult, accumulate, finalize, known_from_store,
    new_accumulator)
from app.ingestion.rag import chunk_text
from app.schema.ontology import Book
from app.warehouse import warehouse

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_BOOKS_DIR = _DATA_DIR / "books"
_MANIFEST = _DATA_DIR / "books_manifest.json"
_ARTIFACT_DIR = _DATA_DIR / "ingest"


# ---------------------------------------------------------------- 书源
def load_manifest() -> dict[str, dict]:
    if not _MANIFEST.exists():
        return {}
    import json
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def source_path(book_id: str) -> Path | None:
    """内置书的 txt 路径（manifest 优先，其次 books/<id>.txt）。"""
    meta = load_manifest().get(book_id) or {}
    name = meta.get("file") or f"{book_id}.txt"
    p = _BOOKS_DIR / name
    return p if p.exists() else None


# ---------------------------------------------------------------- 选项 / 产物
@dataclass
class RunOptions:
    """单次构建参数（离线与在线共用）。"""
    # 本次最多处理的抽取块数；None = 全书。先小样本试跑，满意再放开
    limit: int | None = None
    use_llm: bool = False
    # link = 已知人物转成"对齐到主干"的 link 提案（叠加多本书时避免造重复节点）
    # skip = 已知人物不生成提案；new = 一律新建
    align_mode: str = "link"
    force: bool = False            # True：忽略已有切块与进度，整本重跑
    embed: bool = False            # 切块时算向量（本地模型不可用时自动跳过）
    dry_run: bool = False          # 只算不写（用于预估成本 / 验收抽取质量）
    # LLM 抽取并发数（网络 IO 密集，实测单次 ~20s，6 并发可把 100 块压到 ~5 分钟）
    concurrency: int = 6
    # 每次调用送入的抽取块数（>1 时把多块拼成一次请求，减少调用次数）
    pieces_per_call: int = 1
    # 只用已落盘的原始抽取结果重算提案（不调 LLM，改 TopN/阈值零成本）
    finalize_only: bool = False
    save_raw: bool = True          # 落盘每块原始抽取结果（建议保持 True）
    chunk_size: int = 800
    chunk_overlap: int = 100
    extract_size: int = 2400
    extract_overlap: int = 240
    # TopN：全书跑时太小会把候选实体截掉（实测 200/300 只留下 500 条）；
    # 1w 上限基本不会截断（改这个值不用重跑，用 --finalize-only 重算即可）
    max_persons: int = 10000
    max_events: int = 10000
    min_confidence: float = 0.0
    out: str | None = None         # 产物 JSON；默认 data/ingest/<book_id>.json

    # ---- 可靠性 / 完整性（补齐机制自身的保障）----
    # 单块失败重试次数（异常 / 网关空响应各算一次尝试），与并发无关
    retries: int = 2
    # 补齐时哪些块算「缺口」：
    #   retry_empty    = 抽到空结果的块（多为限流导致的空响应）
    #   retry_fallback = 期望大模型、却静默回退启发式的块（最隐蔽的质量缺口）
    retry_empty: bool = True
    retry_fallback: bool = True
    # repair：一轮跑完自动算缺口再补，直到无缺口或达 max_rounds
    repair: bool = False
    max_rounds: int = 3
    # strict：覆盖率不足（还有没跑/报错的块）时直接抛错，便于脚本判失败
    strict: bool = False
    # keep_best：新提案数少于旧产物时不覆盖旧产物（防止补跑反而把成果改小）
    keep_best: bool = True


@dataclass
class RunResult:
    book_id: str = ""
    title: str = ""
    chunks: int = 0
    pieces: int = 0
    processed: int = 0      # 本次实际抽取的块数
    done: int = 0           # 累计已完成块数
    proposals: list = field(default_factory=list)
    mode: str = "heuristic"
    model: str = "heuristic"
    stats: dict = field(default_factory=dict)
    artifact: str | None = None
    integrity: dict = field(default_factory=dict)   # 完整性自检报告

    @property
    def by_kind(self) -> dict:
        return self.stats.get("by_kind", {})


def _signature(text: str, path: Path, o: RunOptions) -> str:
    """构建参数 + 书源内容的指纹。

    切块大小、抽取窗口或原文一变，块边界就变了，旧进度与旧 raw 全部失效；
    没法靠「块数相同」判断（可能刚好一样），所以直接对内容取指纹。
    """
    h = hashlib.md5()
    h.update(text.encode("utf-8"))
    src = [o.chunk_size, o.chunk_overlap, o.extract_size, o.extract_overlap,
           str(path), len(text), h.hexdigest()]
    return hashlib.md5(
        json.dumps(src, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _artifact_proposal_count(p: Path) -> int | None:
    """旧产物里的提案条数（用于「不让补跑把产物改小」的保护）。"""
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    for key in ("proposals", "extracts"):
        v = data.get(key)
        if isinstance(v, list):
            return len(v)
    return None


def _model_key(llm) -> str:
    if llm is None or getattr(llm, "name", "stub") == "stub":
        return "heuristic"
    return f"llm:{getattr(llm, 'model', None) or getattr(llm, 'name', 'llm')}"


def _build_opts(o: RunOptions, bid: str = "") -> BuildOptions:
    # 事件 id 必须有书级前缀：否则每本书都从 e0 开始，第二本书挂载时
    # 会和上一本占掉的 id 撞主键（Kuzu 报 duplicated primary key）。
    # 人物沿用 normalize（名字本身即跨书自然对齐的键）。
    return BuildOptions(
        chunk_size=o.chunk_size, chunk_overlap=o.chunk_overlap,
        extract_size=o.extract_size, extract_overlap=o.extract_overlap,
        max_persons=o.max_persons, max_events=o.max_events,
        min_confidence=o.min_confidence, align_mode=o.align_mode,
        person_id_mode="normalize", id_prefix=bid,
    )


def _embedder():
    try:
        from app.llm.local_embed import get_embedder
        return get_embedder()
    except Exception:
        return None


# ---------------------------------------------------------------- 主流程
def run_book(book_id: str, *, store=None, opts: RunOptions | None = None,
             llm=None, resolver=None,
             on_progress: Callable[[int, int], None] | None = None
             ) -> RunResult:
    """构建一本书（增量）：切块 → 逐块抽取 → 提案 → 产物 JSON。

    store 为 None 时只写 DuckDB + 产物文件（不碰 Kuzu）；
    传入 store 时额外把提案挂到对应 Book.extracts（**不自动挂载**）。
    """
    o = opts or RunOptions()
    res = RunResult(book_id=book_id)

    meta = load_manifest().get(book_id) or {}
    res.title = meta.get("title", book_id)
    path = source_path(book_id)
    if path is None:
        raise FileNotFoundError(
            f"找不到书源：{book_id}（manifest 未登记或 data/books/ 下无对应 txt）")

    if o.use_llm and llm is None:
        # 抽取走专用网关（EXTRACT_*），与对话/综述（CHAT_*）解耦
        from app.llm import get_extract_llm
        llm = get_extract_llm()
    model = _model_key(llm)
    if o.finalize_only and model == "heuristic":
        # 重算时若没指定 --llm，自动选用已落盘的模型，避免读到空结果
        models = warehouse.extract_models(book_id)
        if models:
            model = models[0]
            res.model = model
            print(f"  [finalize-only] 自动选用已落盘模型：{model}")
    res.model = model

    # 已挂载的书先卸载才能重跑：否则新提案与已写入的子图会对不上
    if store is not None and not o.dry_run:
        b0 = next((b for b in store.books if b.id == book_id), None)
        if b0 is not None and b0.mounted:
            raise ValueError(
                f"《{b0.title}》已挂载，请先卸载再重跑抽取"
                "（否则提案与已写入的子图会不一致）")

    text = path.read_text(encoding="utf-8")

    # ---- 1) 切块（幂等：已有切块且非 force 则直接复用）----
    if o.force or warehouse.chunk_count(book_id) == 0:
        pieces_txt = chunk_text(text, size=o.chunk_size, overlap=o.chunk_overlap)
        chunks = [{"idx": i, "text": t, "embedding": None}
                  for i, t in enumerate(pieces_txt)]
        if o.embed:
            fn = _embedder()
            if fn is not None:
                try:
                    for i, v in enumerate(fn([c["text"] for c in chunks])):
                        if i < len(chunks):
                            chunks[i]["embedding"] = v
                except Exception:
                    pass  # 向量失败不影响入库，RAG 自动回退 BM25
        res.chunks = len(chunks)
        if not o.dry_run:
            if store is not None:
                store.set_chunks(book_id, chunks)
            else:
                warehouse.replace_chunks(book_id, chunks)
    else:
        res.chunks = warehouse.chunk_count(book_id)
        if not o.dry_run:
            warehouse.set_book_run(book_id, chunks=res.chunks)

    # ---- 2) 参数指纹：切块/抽取参数或书源一变，块边界就变，旧进度全部失效 ----
    signature = _signature(text, path, o)
    want_llm = model != "heuristic"
    if not o.dry_run:
        prev = warehouse.get_run_params(book_id)
        if prev and prev.get("signature") != signature:
            print(f"  [参数漂移] 构建参数/书源已变（{prev.get('signature')} "
                  f"→ {signature}）：旧进度与旧结果作废，自动重跑")
            warehouse.reset_progress(book_id)
            warehouse.reset_raw(book_id)
            warehouse.reset_failures(book_id)
        warehouse.set_run_params(book_id, signature, {
            "chunk": [o.chunk_size, o.chunk_overlap],
            "extract": [o.extract_size, o.extract_overlap],
            "src": str(path), "chars": len(text)})

    # ---- 3) 逐块抽取：只跑「缺口块」（断点续跑 + 多轮精准补跑）----
    big = chunk_text(text, size=o.extract_size, overlap=o.extract_overlap)
    total = len(big)
    res.pieces = total
    limit = min(o.limit, total) if o.limit else total
    if o.force and not o.dry_run:
        warehouse.reset_progress(book_id, model)
        warehouse.reset_raw(book_id, model)
        warehouse.reset_failures(book_id, model)

    known = known_from_store(store) if store is not None else {}
    acc = new_accumulator()
    cnt = {"done": 0, "llm": 0, "fallback": 0, "error": 0, "empty": 0}
    cur_total = 0
    pass_done = 0
    retries = max(1, o.retries)

    def _attempt(g: list[int]) -> tuple[dict | None, str]:
        """跑一组块（带重试）：返回 (结果, 错误码)；错误码 '' = 正常。

        单块异常绝不能拖垮整批——以前一块抛异常，后面所有组的结果全丢。
        """
        txt = "\n".join(big[k] for k in g)
        last = "unknown"
        for att in range(1, retries + 1):
            try:
                out = extract(txt, llm=llm, known=known)
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(min(6.0, 1.5 * att))
                continue
            if not isinstance(out, dict):
                last = f"返回类型异常 {type(out).__name__}"
                time.sleep(min(6.0, 1.5 * att))
                continue
            if want_llm:
                # 大模型静默回退启发式：算失败并重试，仍回退则记 fallback 待补
                if out.get("mode") != "llm":
                    if att < retries:
                        last = "fallback"
                        time.sleep(min(6.0, 1.5 * att))
                        continue
                    return out, "fallback"
                # 网关空响应：内容全空，重试
                if not (out.get("persons") or out.get("events")
                        or out.get("links")):
                    if att < retries:
                        last = "empty"
                        time.sleep(min(6.0, 1.5 * att))
                        continue
                    return out, "empty"
            return out, ""
        return None, last

    def _handle(g: list[int], out: dict | None, err: str) -> None:
        nonlocal pass_done
        # 累积与落盘都在主线程，无需加锁
        if out is None:
            cnt["error"] += len(g)
            if not o.dry_run:
                for k in g:
                    warehouse.mark_piece_failed(book_id, k, model, "error",
                                                err, retries)
            return
        accumulate(acc, out, known=known)
        if want_llm and out.get("mode") == "llm":
            cnt["llm"] += len(g)
        elif want_llm:
            cnt["fallback"] += len(g)
        if not o.dry_run:
            # 顺序要紧：先落原始结果，再记进度。反过来会出现
            #「进度说跑过、结果没落盘」的静默缺口，收口时永远补不回来。
            if o.save_raw:
                for k in g:
                    warehouse.save_extract_raw(book_id, k, model, out)
            for k in g:
                warehouse.mark_piece_done(book_id, k, model, out)
            if err:      # empty / fallback：结果照收，但记台账供下一轮补跑
                for k in g:
                    warehouse.mark_piece_failed(book_id, k, model, err, err, 0)
                cnt[err] = cnt.get(err, 0) + len(g)
        cnt["done"] += len(g)
        pass_done += len(g)
        if on_progress:
            on_progress(pass_done, cur_total)

    def _plan() -> list[int]:
        """本次要跑的缺口块：没跑过 / 报错 / 缺原始结果 / 可补的空块与回退块。"""
        rep = warehouse.piece_report(book_id, model, limit)
        gap = set(rep["missing"]) | set(rep["error"])
        if o.save_raw:
            gap |= set(rep["no_raw"])
        if o.retry_empty:
            # 空块最多补 2 次：补过还是空，基本就是这段原文本来没内容，
            # 别每轮都重烧一遍 token
            fc = warehouse.failure_counts(book_id, model)
            gap |= {i for i in rep["empty"] if fc.get((i, "empty"), 0) < 2}
        if o.retry_fallback and want_llm:
            gap |= set(rep["fallback"])
        return [i for i in sorted(gap) if 0 <= i < limit]

    def _work(g: list[int]) -> tuple[list[int], dict | None, str]:
        out, err = _attempt(g)
        return g, out, err

    def _run_pass(todo: list[int]) -> None:
        nonlocal cur_total, pass_done
        cur_total = len(todo)
        pass_done = 0          # 进度按「本轮」算，否则第二轮会显示 11/6
        step = max(1, o.pieces_per_call)
        groups = [todo[i:i + step] for i in range(0, len(todo), step)]
        if o.concurrency > 1 and len(groups) > 1:
            with ThreadPoolExecutor(max_workers=o.concurrency) as ex:
                for g, out, err in ex.map(_work, groups):   # map 保序，结果可复现
                    _handle(g, out, err)
        else:
            for g in groups:
                _handle(*_work(g))

    # 补齐循环：每轮只跑缺口块，成功块不重跑（省钱省时间）
    rounds = max(1, o.max_rounds) if o.repair else 1
    aborted: BaseException | None = None
    try:
        if o.finalize_only:
            print("  [finalize-only] 不调 LLM，直接用已落盘结果重算提案")
        else:
            for rd in range(rounds):
                todo = _plan()
                if not todo:
                    print("  无缺口块：全部已完成" if rd == 0
                          else f"— 补齐第 {rd + 1} 轮：已无缺口，结束")
                    break
                if rd > 0 or o.repair:
                    print(f"— 补齐第 {rd + 1} 轮：{len(todo)} 块"
                          f"（只补缺口，成功块不重跑）")
                _run_pass(todo)
    except BaseException as e:      # 含 Ctrl+C：先收口落盘，再把异常抛回去
        aborted = e

    # ---- 4) 收口：合并「全部已落盘结果」→ 提案 ----
    # 增量补跑时 acc 只有本次跑的块，直接用会让产物反而变小；
    # 所以只要落盘了原始结果，就一律合并全部历史块重算（不调 LLM，成本为零）。
    # 注意：这里**不按 limit 裁剪**——limit 只限制本次跑多少块，
    # 提案要基于全部已落盘结果，否则小样本续跑会把产物越跑越小。
    raw_n = 0
    if o.save_raw and not o.dry_run:
        rows = warehouse.load_extract_raw(book_id, model)
        if rows:
            acc = new_accumulator()
            for _idx, out in rows:
                accumulate(acc, out, known=known)
            raw_n = len(rows)
            print(f"  [收口] 合并已落盘的 {raw_n} 块抽取结果重算提案")
    proposals, info = finalize(acc, known=known, resolver=resolver,
                               opts=_build_opts(o, book_id))
    rep = ({} if o.dry_run else warehouse.piece_report(book_id, model, limit))
    res.proposals = proposals
    res.processed = cnt["done"]
    res.mode = info.get("mode", "heuristic")
    res.stats = {**info, "pieces": total, "limit": limit,
                 "processed": cnt["done"], "chunks": res.chunks,
                 "title": res.title,
                 "llm_pieces": cnt["llm"], "fallback_pieces": cnt["fallback"],
                 "error_pieces": cnt["error"], "empty_pieces": cnt["empty"],
                 "raw_pieces": raw_n, "coverage": rep.get("coverage", 0.0),
                 "missing_pieces": rep.get("missing_n", 0),
                 "no_raw_pieces": rep.get("no_raw_n", 0)}
    res.done = rep.get("done", cnt["done"])
    res.integrity = rep

    if o.dry_run:
        return res

    # ---- 5) 状态 + 产物 ----
    complete = bool(rep.get("complete"))
    stage = "prepared" if (complete and limit >= total) else "partial"
    # 状态不能被「小样本试跑 / 换模型试跑」冲掉：
    # 例如全书已用 llm 跑完 296 块，再拿 heuristic 跑 5 块看质量，
    # 不能把状态改写成「5/296、0 提案」——那会让界面以为这本书没跑完。
    prev_run = warehouse.book_run(book_id) or {}
    keep_old = False
    if prev_run and not o.force:
        if (prev_run.get("model") or model) == model:
            keep_old = (res.done < int(prev_run.get("done") or 0)
                        or len(proposals) < int(prev_run.get("proposals") or 0))
        else:
            keep_old = limit < total      # 换模型：只有全书跑才改状态
    if keep_old:
        print(f"  [保护] 保留已有构建状态（{prev_run.get('stage')} "
              f"{prev_run.get('done')}/{prev_run.get('pieces')} 块、"
              f"{prev_run.get('proposals')} 提案）：本次只是样本/换模型试跑")
        res.stats["kept_old_run"] = True
    else:
        warehouse.set_book_run(book_id, stage=stage, chunks=res.chunks,
                               pieces=total, done=res.done, model=model,
                               mode=res.mode, proposals=len(proposals))

    out_path = Path(o.out) if o.out else _ARTIFACT_DIR / f"{book_id}.json"
    old_n = _artifact_proposal_count(out_path)
    if (o.keep_best and not o.force and old_n is not None
            and len(proposals) < old_n):
        # 补跑只会让结果变多；变少说明这次跑得不完整，别把好产物冲掉
        print(f"  [保护] 本次提案 {len(proposals)} < 旧产物 {old_n}："
              f"保留旧产物不覆盖（确认要覆盖就加 --force）")
        res.stats["kept_old_artifact"] = True
    else:
        BuildResult(text="", chunks=[], proposals=proposals, mode=res.mode,
                    stats=res.stats).write_json(out_path, book_meta={
                        "id": book_id, "title": res.title,
                        "author": meta.get("author", ""),
                        "dynasty_id": meta.get("dynasty_id"),
                        "span": meta.get("span"), "level": meta.get("level", 1),
                        "color": meta.get("color", "#b91c1c"),
                        "tags": meta.get("tags", []),
                        "description": meta.get("description", ""),
                        "year": meta.get("year", 0)})
    res.artifact = str(out_path)

    if aborted is not None:
        # 已跑到的都收口了，异常照抛，调用方（CLI/API）据此判失败
        raise aborted
    if o.strict and not complete:
        raise RuntimeError(
            f"覆盖不完整：{rep.get('done')}/{rep.get('total')} 块"
            f"（缺 {rep.get('missing_n')} · 原始结果缺 {rep.get('no_raw_n')}"
            f" · 报错 {rep.get('error_n')}）。补齐：重跑同一命令，或加 --repair")

    # ---- 6) 挂到 Book.extracts（不挂载；挂载必须由人确认）----
    if store is not None:
        book = next((b for b in store.books if b.id == book_id), None)
        if book is None:
            # 首次建书：即使只跑了样本也要能看见，先挂上
            book = book_from_manifest(book_id, meta, proposals)
            store.books.append(book)
            store.save()
        else:
            if book.mounted:
                raise ValueError(
                    f"《{book.title}》已挂载，请先卸载再重跑抽取"
                    "（否则提案与已写入的子图会不一致）")
            # 已有提案：只跑了一部分（或中断过）时不要顶掉完整的那一份
            full = limit >= total and (complete or o.force)
            if full or len(proposals) >= len(book.extracts or []):
                book.extracts = proposals
                store.save()
            else:
                print(f"  [保护] 本次只跑了 {limit}/{total} 块且不完整："
                      f"保留 Book.extracts 里已有的 {len(book.extracts or [])} 条提案"
                      f"（跑全书或 --finalize-only 后再写入）")
                res.stats["kept_old_extracts"] = True
    return res


def book_from_manifest(book_id: str, meta: dict, proposals: list) -> Book:
    """按 manifest 元数据建一本「未挂载」的书（内置书首次进入队列时用）。"""
    span = tuple(int(x) for x in (meta.get("span") or [0, 0]))
    return Book(
        id=book_id,
        title=meta.get("title", book_id),
        author=meta.get("author", "佚名"),
        year=int(meta.get("year", 0)),
        level=int(meta.get("level", 1)),
        color=meta.get("color", "#b91c1c"),
        dynasty_id=meta.get("dynasty_id", "xihan"),
        span=span,
        tags=list(meta.get("tags", [])),
        description=meta.get("description", ""),
        extracts=list(proposals),
        mounted=False,
        origin="builtin",
        file=meta.get("file") or f"{book_id}.txt",
    )


# ---------------------------------------------------------------- 进度视图
def book_status(book_id: str, *, store=None) -> dict:
    """一本书的构建进度（CLI / API 共用）。"""
    meta = load_manifest().get(book_id) or {}
    run = warehouse.book_run(book_id) or {}
    has_src = source_path(book_id) is not None
    book = None
    if store is not None:
        book = next((b for b in store.books if b.id == book_id), None)
    pieces = int(run.get("pieces") or 0)
    done = int(run.get("done") or 0)
    integrity = None
    if pieces and run.get("model"):
        try:
            integrity = warehouse.piece_report(book_id, run["model"], pieces)
        except Exception:
            integrity = None
    return {
        "integrity": integrity,
        "book_id": book_id,
        "title": meta.get("title", book_id),
        "author": meta.get("author", ""),
        "has_source": has_src,
        "stage": run.get("stage") or ("pending" if has_src else "no_source"),
        "chunks": int(run.get("chunks") or 0),
        "pieces": pieces,
        "done": done,
        "percent": round(done * 100 / pieces, 1) if pieces else 0.0,
        "model": run.get("model") or "",
        "mode": run.get("mode") or "",
        "proposals": int(run.get("proposals") or 0),
        "in_store": book is not None,
        "mounted": bool(book.mounted) if book else False,
        "updated": run.get("updated"),
    }


def queue_status(*, store=None) -> list[dict]:
    """全部内置书的构建队列：先看 pending，再看已 prepared 未挂载。"""
    return [book_status(bid, store=store) for bid in load_manifest()]
