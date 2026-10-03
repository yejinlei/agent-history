# -*- coding: utf-8 -*-
"""统一本体构建管线（离线 / 在线共用同一份实现）

    parse  →  chunk  →  extract  →  normalize  →  align  →  score  →  proposals

设计要点：
- 与"触发方式"完全解耦：离线 CLI、启动同步、HTTP 上传三条入口都调用本模块，
  因此离线跑出的结果与在线跑出的结果结构一致、可互相复用。
- 与"存储"解耦：本模块不 import Store。已知实体通过 known 字典传入，
  离线场景可以只传空字典（不做实体对齐），也可以从 JSON 载入主干快照。
- 与"模型"解耦：llm=None 或 StubLLM 时走启发式；传入真实 LLM 走 JSON schema
  抽取，失败自动回退。embed_fn 缺省时切块 embedding 为 None（RAG 自动 BM25）。

产物 BuildResult 可整体导出为 JSON（--out），再由在线 /api/ontology/import 导入，
实现"离线构建 → 在线挂载"的闭环。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.ingestion.extract import extract
from app.ingestion.parse import parse_document
from app.ingestion.rag import chunk_text
from app.ingestion.resolve import normalize_name
from app.schema.ontology import Extract

# 常见误抽：含中文/阿拉伯数字的通常不是人名（如「兵四十万」「三年春」）
_NUM_CHARS = set("0123456789一二两三四五六七八九十百千万零○元")
_FORMAT_VERSION = 1


# ---------------------------------------------------------------- 选项 / 产物
@dataclass
class BuildOptions:
    """构建参数（离线与在线共用，默认值与内置书同步保持一致）。"""
    # 切块
    chunk_size: int = 800
    chunk_overlap: int = 100
    # 抽取用大块（降低调用次数同时保留上下文）
    extract_size: int = 2400
    extract_overlap: int = 240
    # chunked = 按大块逐块抽取后聚合（离线/内置书：覆盖全文）
    # full    = 整篇一次送入抽取器（在线上传：LLM 模式只发一次请求，
    #           由抽取器内部截断；大书不会退化成上百次调用）
    extract_mode: str = "chunked"
    # 单次构建最多处理的抽取块数（None=不限；LLM 模式建议设上限控成本）
    max_extract_pieces: int | None = None
    # 单本书提案上限（按 confidence 排序取 Top，避免图谱无限膨胀）
    max_persons: int = 200
    max_events: int = 300
    # 低于该置信度的候选不生成提案
    min_confidence: float = 0.0
    # 已知人物的处理策略：
    #   skip = 不生成提案（内置古籍：避免跨书重复造节点）
    #   link = 生成 link 提案对齐到主干（上传书：不造重复，只挂观点）
    #   new  = 一律按新实体生成提案
    align_mode: str = "skip"
    # 新实体 id 生成方式：normalize = 用归一化名（内置书）；prefix = {prefix}_p{i}
    person_id_mode: str = "normalize"
    id_prefix: str = ""
    # 人名合法性过滤（长度 2–6、不含数字）；关闭则完全信任抽取器
    filter_person_names: bool = True
    # 可选向量化函数：embed_fn(list[str]) -> list[list[float]]，失败由调用方兜底
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None


@dataclass
class BuildResult:
    text: str = ""
    chunks: list[dict] = field(default_factory=list)
    proposals: list[Extract] = field(default_factory=list)
    mode: str = "heuristic"
    stats: dict = field(default_factory=dict)

    # ---------------- 序列化（离线产物 <-> 在线导入）----------------
    def to_json(self, book_meta: dict | None = None) -> dict:
        return {
            "version": _FORMAT_VERSION,
            "book": book_meta or {},
            "mode": self.mode,
            "stats": self.stats,
            "chunks": [{"idx": c["idx"], "text": c["text"]}
                       for c in self.chunks],
            "proposals": [p.model_dump() for p in self.proposals],
        }

    def write_json(self, path: str | Path,
                   book_meta: dict | None = None) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_json(book_meta), ensure_ascii=False,
                                indent=1), encoding="utf-8")
        return p


def proposals_from_json(data: dict) -> tuple[list[Extract], list[dict], dict]:
    """从离线产物恢复（提案 / 切块 / 元信息）。"""
    if int(data.get("version", 0)) != _FORMAT_VERSION:
        raise ValueError(f"产物版本不兼容：{data.get('version')}（期望 {_FORMAT_VERSION}）")
    proposals = [Extract.model_validate(x) for x in data.get("proposals", [])]
    chunks = [{"idx": int(c["idx"]), "text": c["text"], "embedding": None}
              for c in data.get("chunks", [])]
    meta = {"mode": data.get("mode", "heuristic"),
            "stats": data.get("stats", {}),
            "book": data.get("book", {})}
    return proposals, chunks, meta


# ---------------------------------------------------------------- known 快照
def known_from_store(store) -> dict:
    """从 Store 抽取"已知实体"快照（离线无 store 时可传空字典）。"""
    return {
        "people": [{"name": p.name, "aliases": list(p.aliases)}
                   for p in store.people],
        "places": [{"name": pl.name, "ancient": pl.ancient, "id": pl.id,
                    "lon": pl.lon, "lat": pl.lat} for pl in store.places],
        "person_ids": {p.id for p in store.people},
        "person_names": {normalize_name(p.name) for p in store.people}
                        | {normalize_name(a) for p in store.people
                           for a in (p.aliases or [])},
        "event_ids": {e.id for e in store.events},
        "event_names": {normalize_name(e.name) for e in store.events},
        "dynasty_ids": {d.id for d in store.dynasties},
    }


# ---------------------------------------------------------------- 构建入口
def build_from_text(text: str, *, known: dict | None = None, llm=None,
                    resolver=None, opts: BuildOptions | None = None
                    ) -> BuildResult:
    """文本 → 切块 + 本体提案（纯函数，不触碰任何存储）。"""
    opts = opts or BuildOptions()
    known = known or {}
    res = BuildResult(text=text)

    # ---- 1) RAG 切块（全量）----
    pieces = chunk_text(text, size=opts.chunk_size, overlap=opts.chunk_overlap)
    res.chunks = [{"idx": i, "text": p, "embedding": None}
                  for i, p in enumerate(pieces)]
    if opts.embed_fn is not None and pieces:
        try:
            vecs = opts.embed_fn(pieces)
            for i, v in enumerate(vecs):
                if i < len(res.chunks):
                    res.chunks[i]["embedding"] = v
        except Exception:
            pass  # 向量失败不影响本体构建；RAG 自动回退 BM25

    # ---- 2) 抽取（大块循环 + 聚合）----
    if opts.extract_mode == "full":
        big = [text]
    else:
        big = chunk_text(text, size=opts.extract_size,
                         overlap=opts.extract_overlap)
        if opts.max_extract_pieces:
            big = big[:opts.max_extract_pieces]

    acc = new_accumulator()
    for piece in big:
        # 过短的尾块跳过；但若全文本就只有一块（短文本输入），仍须处理
        if len(piece) < 40 and len(big) > 1:
            continue
        accumulate(acc, extract(piece, llm=llm, known=known), known=known)

    res.mode = "llm" if "llm" in acc["modes"] else "heuristic"

    # ---- 3) 归一化 / 排序 / TopN + 4) 对齐 + 5) 置信度 → 提案 ----
    res.proposals, info = finalize(acc, known=known, resolver=resolver,
                                   opts=opts)
    res.stats = {"chars": len(text), "chunks": len(res.chunks),
                 "pieces": len(big), **info}
    return res


# ---------------------------------------------------------------- 可续跑接口
def new_accumulator() -> dict:
    """跨块累积容器（逐本增量跑时由调用方持有，可随时落盘/恢复）。"""
    return {"persons": {}, "events": {}, "links": [], "modes": set()}


def accumulate(acc: dict, out: dict, *, known: dict | None = None) -> None:
    """把一次单块抽取结果并入累积容器。"""
    known = known or {}
    acc["modes"].add(out.get("mode", "heuristic"))
    for p in out.get("persons", []):
        _merge_person(acc["persons"], p, known)
    for e in out.get("events", []):
        _merge_event(acc["events"], e, known)
    acc["links"].extend(out.get("links", []))


def finalize(acc: dict, *, known: dict | None = None, resolver=None,
             opts: BuildOptions | None = None) -> tuple[list[Extract], dict]:
    """收口：TopN → 对齐 → 提案，并给出统计信息。"""
    opts = opts or BuildOptions()
    known = known or {}
    persons = sorted(acc["persons"].values(),
                     key=lambda x: x["confidence"], reverse=True)
    events = sorted(acc["events"].values(),
                    key=lambda x: x["confidence"], reverse=True)
    persons = persons[:opts.max_persons]
    events = events[:opts.max_events]
    proposals = _to_proposals(persons, events, acc["links"],
                              known=known, resolver=resolver, opts=opts)

    # 主导朝代：候选事件里出现最多的 dynasty_id（供落库时给 Book 定朝代）
    dyn_counts: dict[str, int] = {}
    for e in acc["events"].values():
        if e.get("dynasty_id"):
            dyn_counts[e["dynasty_id"]] = dyn_counts.get(e["dynasty_id"], 0) + 1
    dynasty_hint = max(dyn_counts, key=dyn_counts.get) if dyn_counts else None

    mode = "llm" if "llm" in acc["modes"] else "heuristic"
    info = {
        "dynasty_hint": dynasty_hint,
        "candidate_persons": len(acc["persons"]),
        "candidate_events": len(acc["events"]),
        "candidate_links": len(acc["links"]),
        "proposals": len(proposals),
        "by_kind": {
            "person": sum(1 for x in proposals if x.kind == "person"),
            "event": sum(1 for x in proposals if x.kind == "event"),
            "link": sum(1 for x in proposals if x.kind == "link"),
        },
        "mode": mode,
    }
    return proposals, info


def build_from_path(path: str | Path, **kw) -> BuildResult:
    """文件（txt/md/pdf/epub）→ 解析后进入同一管线。"""
    parsed = parse_document(path)
    return build_from_text(parsed["text"], **kw)


# ---------------------------------------------------------------- 内部步骤
# G0 名称闸门：人名里出现这些字，基本可断为句读碎片而非人名
# （"傅险姓之""世贬帝号""续嬴氏祀"这类误抽已实测污染过图谱）
_NAME_STOP_ANY = set("於以为封授续贬号位世氏姓曰政侯邑地国年也者")
_NAME_STOP_TAIL = set("之其乃遂使而将於也")


def _ok_person_name(name: str, filter_on: bool) -> bool:
    if not name:
        return False
    key = normalize_name(name)
    if not key or len(key) < 2 or len(key) > 6:
        return False
    if filter_on and any(ch in name for ch in _NUM_CHARS):
        return False
    if filter_on and any(ch in key for ch in _NAME_STOP_ANY):
        return False
    if filter_on and key[-1] in _NAME_STOP_TAIL:
        return False
    return True


def _merge_person(person_map: dict, p: dict, known: dict) -> None:
    name = (p.get("name") or "").strip()
    if not _ok_person_name(name, True):
        return
    key = normalize_name(name)
    old = person_map.get(key)
    conf = float(p.get("confidence") or 0.5)
    if old is None:
        person_map[key] = {
            "name": name,
            "aliases": list(p.get("aliases") or []),
            "summary": p.get("summary", ""),
            "confidence": conf,
            "dynasty_id": p.get("dynasty_id"),
        }
        return
    old["confidence"] = max(old["confidence"], conf)
    for a in p.get("aliases") or []:
        if a not in old["aliases"]:
            old["aliases"].append(a)
    if len(p.get("summary", "")) > len(old["summary"]):
        old["summary"] = p["summary"]
    if not old["dynasty_id"] and p.get("dynasty_id"):
        old["dynasty_id"] = p["dynasty_id"]


def _merge_event(event_map: dict, e: dict, known: dict) -> None:
    name = (e.get("name") or "").strip()
    if not name or len(name) < 2:
        return
    did = e.get("dynasty_id")
    key = (did, e.get("year"), normalize_name(name))
    conf = float(e.get("confidence") or 0.45)
    old = event_map.get(key)
    if old is None:
        event_map[key] = {
            "name": name, "year": e.get("year"),
            "place": e.get("place"),
            "lon": e.get("lon"), "lat": e.get("lat"),
            "summary": e.get("summary", ""),
            "confidence": conf, "dynasty_id": did,
        }
        return
    old["confidence"] = max(old["confidence"], conf)
    if len(e.get("summary", "")) > len(old["summary"]):
        old["summary"] = e["summary"]
    if old["place"] is None and e.get("place"):
        old["place"] = e["place"]
        old["lon"], old["lat"] = e.get("lon"), e.get("lat")


def _resolve_target(resolver, known: dict, name: str, kind: str) -> str | None:
    """优先用 Resolver（别名/归一化），否则退回 known 快照比对。"""
    if resolver is not None:
        try:
            hit = resolver.resolve(name, kind)
            if hit:
                return hit["id"]
        except Exception:
            pass
    key = normalize_name(name)
    if kind == "person":
        if key in known.get("person_ids", set()):
            return key
        for p in known.get("people", []):
            if normalize_name(p["name"]) == key:
                return p.get("id") or key
        return None
    if kind == "event":
        if key in known.get("event_ids", set()):
            return key
        return None
    return None


def _to_proposals(persons: Iterable[dict], events: Iterable[dict],
                  links: Iterable[dict], *, known: dict, resolver,
                  opts: BuildOptions) -> list[Extract]:
    out: list[Extract] = []
    prefix = opts.id_prefix

    for i, p in enumerate(persons):
        conf = float(p["confidence"])
        if conf < opts.min_confidence:
            continue
        key = normalize_name(p["name"])
        # 已知人物：按 align_mode 决定跳过 / 转 link / 仍新建
        is_known = key in known.get("person_names", set()) or (
            key in known.get("person_ids", set()))
        if is_known and opts.align_mode == "skip":
            continue
        if is_known and opts.align_mode == "link":
            tid = _resolve_target(resolver, known, p["name"], "person")
            if tid:
                out.append(Extract(
                    kind="link", name=p["name"],
                    summary=f"书内「{p['name']}」对齐到主干人物",
                    confidence=max(conf, 0.6), target_id=tid))
            continue
        new_id = key if opts.person_id_mode == "normalize" else f"{prefix}_p{i}"
        out.append(Extract(
            kind="person", name=p["name"], summary=p["summary"][:220],
            confidence=conf, new_id=new_id))

    for i, e in enumerate(events):
        conf = float(e["confidence"])
        if conf < opts.min_confidence:
            continue
        out.append(Extract(
            kind="event", name=e["name"], summary=e["summary"][:240],
            confidence=conf, new_id=f"{prefix}_e{i}" if prefix else f"e{i}",
            year=e.get("year"), lon=e.get("lon"), lat=e.get("lat"),
            place=e.get("place")))

    # 抽取器给出的关系：端点能落到主干才生成 link 提案（不造悬空边）
    for lk in links:
        dst = lk.get("dst") or lk.get("target_id")
        if not dst:
            continue
        tid = _resolve_target(resolver, known, dst, "event") \
            or _resolve_target(resolver, known, dst, "person")
        if not tid:
            continue
        out.append(Extract(
            kind="link", name=lk.get("src") or dst,
            summary=lk.get("rel") or "书内观点挂载到主干实体",
            confidence=float(lk.get("confidence") or 0.5), target_id=tid))
    return out
