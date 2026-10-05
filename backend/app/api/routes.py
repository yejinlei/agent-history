# -*- coding: utf-8 -*-
"""HTTP API：前端只与此层交互；Agent 图与本体均在后端。"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from langgraph.types import Command

from app.store import store, _LIB_FILES
from app.warehouse import warehouse
from app import config
from app.schema.ontology import REL_TYPES, Book as BookModel
from app.graph.builder import build_chat_graph
from app.ingestion.mount import mount_graph, PIPELINE_STAGES
from app.ingestion.parse import parse_document, ParseError, SUPPORTED
from app.ingestion.pipeline import (
    BuildOptions, build_from_text, known_from_store, proposals_from_json)
from app.ingestion.bookrun import (
    RunOptions, book_status, queue_status, run_book)
from app.ingestion.resolve import Resolver
from app.ontology.actions import action_log
from app.ontology.verify import grounding_report
from app.agents.registry import AgentRegistry, TOOLS
from app.agents.runtime import (
    run_agent, pending_ops, confirm_operation, cancel_operation)
from app.llm.base import get_llm
from app.llm.local_embed import get_embedder

router = APIRouter(prefix="/api")
chat_graph = build_chat_graph(store)


# ---------------- 序列化 ----------------
def src_dict(s) -> dict:
    return {"level": s.level, "work": s.work, "text": s.text,
            "translation": s.translation, "confidence": s.confidence,
            "from_book": s.from_book}


def _person_name(pid: str) -> str:
    p = next((x for x in store.people if x.id == pid), None)
    return p.name if p else pid


def event_dict(e, with_sources: bool = False) -> dict:
    d = {
        "id": e.id, "name": e.name, "year": e.year,
        "month": e.month, "day": e.day,
        "date_confidence": getattr(e, "date_confidence", "exact"),
        "dynasty_id": e.dynasty_id, "lon": e.lon, "lat": e.lat,
        "place": e.place, "actors": e.actors,
        "actor_names": [_person_name(a) for a in e.actors],
        "significance": e.significance, "dispute": e.dispute,
        "route": getattr(e, "route", None),
        "route_label": getattr(e, "route_label", None),
        "from_book": e.from_book,
        "external_ids": getattr(e, "external_ids", {}) or {},
        "provenance": getattr(e, "provenance", "seed"),
    }
    if with_sources:
        d["sources"] = [src_dict(s) for s in store.sources_of("event", e)]
    return d


def person_dict(p, with_sources: bool = False) -> dict:
    d = {"id": p.id, "name": p.name, "years": list(p.years),
         "dynasty_id": p.dynasty_id, "home": p.home,
         "lon": p.lon, "lat": p.lat,
         "from_book": p.from_book,
         "external_ids": getattr(p, "external_ids", {}) or {},
         "provenance": getattr(p, "provenance", "seed")}
    if with_sources:
        d["sources"] = [src_dict(s) for s in store.sources_of("person", p)]
    return d


def dynasty_dict(d) -> dict:
    return {"id": d.id, "name": d.name, "span": list(d.span),
            "color": d.color,
            "capital": {"name": d.capital[0], "lon": d.capital[1], "lat": d.capital[2]},
            "map_tile": d.map_tile, "emperors": d.emperors,
            "group": getattr(d, "group", None), "lane": getattr(d, "lane", 0),
            "external_ids": getattr(d, "external_ids", {}) or {},
            "provenance": getattr(d, "provenance", "seed")}


def narrative_dict(obj) -> dict:
    return {"student": obj.narrative.student,
            "scholar": obj.narrative.scholar, "fan": obj.narrative.fan}


# ---------------- 基础接口 ----------------
@router.get("/health")
def health():
    return {"ok": True, "llm": config.LLM_PROVIDER}


@router.get("/dynasties")
def dynasties():
    return [dynasty_dict(d) for d in store.dynasties]


@router.get("/timeline")
def timeline(year: int, month: int | None = None,
             dynasty_id: str | None = None):
    # 默认按年份定位主线朝代；dynasty_id 可强制指定（含并立政权 lane>0），
    # 用于点击时间轴上某政权车道时联动展示该政权的事件与疆域图。
    if dynasty_id:
        d = next((x for x in store.dynasties if x.id == dynasty_id), None)
        if d is None:
            raise HTTPException(404, "dynasty not found")
    else:
        d = store.dynasty_at(year)
    # 同时段并立政权（lane>0）：剔除当前选中的那个，避免重复
    parallel = [x for x in store.dynasties
                if getattr(x, "lane", 0) > 0
                and x.id != d.id
                and x.span[0] <= year <= x.span[1]]
    evts = list(store.events_of(d.id))
    for x in parallel:
        evts.extend(store.events_of(x.id))
    if month is not None:
        # 月粒度切片：该年该月（含精确到月/日的事件）
        evts = [e for e in evts if e.year == year and e.month == month]
    evts.sort(key=lambda e: (e.year, e.month or 0, e.day or 0))
    return {"year": year, "month": month, "dynasty": dynasty_dict(d),
            "parallel": [dynasty_dict(x) for x in parallel],
            "events": [event_dict(e) for e in evts]}


@router.get("/events")
def all_events(frm: int = -8000, to: int = 1912):
    """全量事件轻量索引（供时间轴缩放落点）。"""
    out = [
        {"id": e.id, "name": e.name, "year": e.year, "month": e.month,
         "day": e.day, "dynasty_id": e.dynasty_id, "from_book": e.from_book}
        for e in store.events if frm <= e.year <= to
    ]
    out.sort(key=lambda x: (x["year"], x["month"] or 0, x["day"] or 0))
    return out


# ---------------- OSGeo 历史地图瓦片：本地缓存代理 ----------------
# 前端请求 /api/tiles/{tile}/{z}/{x}/{y}.png →
#   本地 backend/app/data/tiles/ 已有 → 直接返回；
#   否则从 tile.osgeo.cn 下载、落盘缓存后返回。
# 浏览过的瓦片自动积累在本地，可随 git 提交，离线/内网也能用。
_TILE_DIR = Path(config.DATA_DIR) / "tiles"
_OSGEO_WMTS = "https://tile.osgeo.cn/wmts"
_TILE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


@router.get("/tiles/{tile}/{z}/{x}/{y}.png")
def tile_proxy(tile: str, z: int, x: int, y: int):
    if not _TILE_ID_RE.fullmatch(tile):
        raise HTTPException(400, "非法瓦片 ID")
    if not (0 <= z <= 19):
        raise HTTPException(400, "非法 zoom")
    local = _TILE_DIR / tile / str(z) / str(x) / f"{y}.png"
    if local.exists():
        return FileResponse(local, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})
    url = f"{_OSGEO_WMTS}/{tile}/webmercator/{z}/{x}/{y}.png"
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        # 故障转移：上游不可达时返回 1x1 透明 PNG 占位，地图层不破裂
        return Response(content=_TRANSPARENT_PNG, media_type="image/png",
                        status_code=200,
                        headers={"X-Tile-Source": "fallback", "Cache-Control": "no-cache"})
    if r.status_code != 200 or not r.content:
        raise HTTPException(404, "瓦片不存在")
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(".png.tmp")
    tmp.write_bytes(r.content)
    tmp.replace(local)
    return Response(content=r.content, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# 1x1 透明 PNG（上游故障降级用，Leaflet 仍能正常渲染占位瓦片）
_TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63fcffff3f030005fe02fea72d8144"
    "0000000049454e44ae426082")


@router.get("/maps")
def maps():
    return {"catalog": store.maps_catalog,
            "tile_tpl": config.OSGEO_TILE_TPL, "attribution": config.OSGEO_ATTR}


# 朝代 → maps.json 图库分组（细分后的并立政权共享所属时期的图库）
_MAP_GROUPS = {
    "old_stone": ["pre_qin"], "yangshao": ["pre_qin"], "hemudu": ["pre_qin"],
    "hongshan": ["pre_qin"], "longshan": ["pre_qin"], "liangzhu": ["pre_qin"],
    "sanxingdui": ["pre_qin"],
    "xia": ["pre_qin"], "shang": ["pre_qin"], "xizhou": ["pre_qin"],
    "chunqiu": ["pre_qin"], "zhanguo": ["pre_qin"],
    "qin": ["qin_han"], "xihan": ["qin_han"], "donghan": ["qin_han"],
    "wei": ["nanbei"], "shu": ["nanbei"], "wu": ["nanbei"],
    "jin": ["nanbei"], "dongjin": ["nanbei"], "shiliuguo": ["nanbei"],
    "liusong": ["nanbei"], "nanqi": ["nanbei"], "liang": ["nanbei"], "chen": ["nanbei"],
    "beiwei": ["nanbei"], "dongwei": ["nanbei"], "beiqi": ["nanbei"],
    "xiwei": ["nanbei"], "beizhou": ["nanbei"],
    "sui": ["sui_tang"], "tang": ["sui_tang"],
    "houliang": ["sui_tang"], "houtang": ["sui_tang"], "houjin": ["sui_tang"],
    "houhan": ["sui_tang"], "houzhou": ["sui_tang"],
    "sg_nantang": ["sui_tang"], "sg_wuyue": ["sui_tang"], "sg_qianshu": ["sui_tang"],
    "sg_houshu": ["sui_tang"], "sg_nh": ["sui_tang"], "sg_beiha": ["sui_tang"],
    "sg_jingnan": ["sui_tang"],
    "cq_lu": ["pre_qin"], "cq_qi": ["pre_qin"], "cq_jin": ["pre_qin"],
    "cq_chu": ["pre_qin"], "cq_qin": ["pre_qin"], "cq_yan": ["pre_qin"],
    "cq_song": ["pre_qin"],
    "zg_han": ["pre_qin"], "zg_zhao": ["pre_qin"], "zg_wei": ["pre_qin"],
    "zg_tianqi": ["pre_qin"],
    "qianqin": ["nanbei"], "houyan": ["nanbei"],
    "xiongnu": ["qin_han"],
    "chenghan": ["nanbei"], "qianzhao": ["nanbei"], "houzhao": ["nanbei"],
    "qianliang": ["nanbei"], "qianyan": ["nanbei"], "houtqin": ["nanbei"],
    "huxia": ["nanbei"], "rouran": ["nanbei"],
    "tujue": ["sui_tang"], "tubo": ["sui_tang"], "huigu": ["sui_tang"],
    "nanzhao": ["sui_tang"], "bohai": ["sui_tang"],
    "sg_min": ["sui_tang"], "sg_chu": ["sui_tang"],
    "beisong": ["song"], "nansong": ["song"],
    "liao": ["song"], "xixia": ["song"], "jindyn": ["song"],
    "xiliao": ["song"], "dali": ["song"],
    "yuan": ["yuan"], "ming": ["ming"], "beiyuan": ["ming"], "qing": ["qing"],
}


@router.get("/maps/{dynasty_id}")
def dynasty_maps(dynasty_id: str):
    """该朝代的疆域地图图库：首选主图 + osgeo 图库中同时期的全部地图。"""
    d = next((x for x in store.dynasties if x.id == dynasty_id), None)
    if d is None:
        raise HTTPException(404, "dynasty not found")
    out, seen = [], set()
    if d.map_tile:
        out.append({"tile": d.map_tile, "title": f"{d.name}·疆域主图", "primary": True})
        seen.add(d.map_tile)
    for g in _MAP_GROUPS.get(dynasty_id, []):
        for m in store.maps_catalog.get(g, []):
            tile = m["tile"] if m["tile"].startswith("m") else "m" + m["tile"]
            if tile in seen:
                continue
            seen.add(tile)
            out.append({"tile": tile, "title": m["title"]})
    return out


@router.get("/search")
def search(q: str):
    out = []
    for kind, obj in store.search(q):
        if kind == "person":
            out.append({"kind": kind, **person_dict(obj)})
        elif kind == "event":
            out.append({"kind": kind, **event_dict(obj)})
        else:
            out.append({"kind": kind, "id": obj.id, "name": obj.name,
                        "ancient": obj.ancient, "modern": obj.modern,
                        "lon": obj.lon, "lat": obj.lat,
                        "external_ids": getattr(obj, "external_ids", {}) or {},
                        "provenance": getattr(obj, "provenance", "seed")})
    return out


@router.get("/entity/{kind}/{oid}")
def entity(kind: str, oid: str):
    if kind not in ("event", "person", "place"):
        raise HTTPException(400, "bad kind")
    obj = store.get(kind, oid)
    if obj is None:
        raise HTTPException(404, "not found")
    edges = _edges_payload(oid)
    if kind == "event":
        return {"kind": kind, **event_dict(obj, with_sources=True),
                "narrative": narrative_dict(obj), "edges": edges}
    if kind == "person":
        return {"kind": kind, **person_dict(obj, with_sources=True),
                "narrative": narrative_dict(obj), "edges": edges}
    return {"kind": kind, "id": obj.id, "name": obj.name, "ancient": obj.ancient,
            "modern": obj.modern, "lon": obj.lon, "lat": obj.lat,
            "external_ids": getattr(obj, "external_ids", {}) or {},
            "provenance": getattr(obj, "provenance", "seed"),
            "narrative": narrative_dict(obj), "edges": edges}


def _node_brief(node_id: str) -> dict:
    """把边对端节点 ID 解析成 {kind,id,name} 供前端跳转。"""
    for kind, table in (("event", store.events), ("person", store.people),
                        ("place", store.places)):
        hit = next((x for x in table if x.id == node_id), None)
        if hit is not None:
            return {"kind": kind, "id": hit.id, "name": hit.name}
    b = next((x for x in store.books if x.id == node_id), None)
    if b is not None:
        return {"kind": "book", "id": b.id, "name": f"《{b.title}》"}
    d = next((x for x in store.dynasties if x.id == node_id), None)
    if d is not None:
        return {"kind": "dynasty", "id": d.id, "name": d.name}
    return {"kind": "unknown", "id": node_id, "name": node_id}


def _edges_payload(node_id: str) -> list[dict]:
    out = []
    for e in store.edges_of(node_id):
        other = e["b"] if e["a"] == node_id else e["a"]
        out.append({"rel": e["rel"], "direction": "out" if e["a"] == node_id else "in",
                    "node": _node_brief(other), "from_book": e.get("from_book"),
                    "confidence": e.get("confidence", 0.8),
                    "src_ids": e.get("src_ids", [])})
    return out


# ---------------- 本体导航（左侧面板） ----------------
@router.get("/ontology")
def ontology():
    """分类实体清单 + 关系统计：驱动左侧本体导航栏。"""
    rels: dict[str, int] = {}
    for e in store.edges:
        rels[e["rel"]] = rels.get(e["rel"], 0) + 1
    return {
        "stats": {
            "dynasty": len(store.dynasties),
            "event": len(store.events),
            "person": len(store.people),
            "place": len(store.places),
            "book": len(store.books),
            "institution": len(store.institutions),
            "artifact": len(store.artifacts),
            "concept": len(store.concepts),
            "edge": len(store.edges),
        },
        "relations": [{"rel": k, "count": v} for k, v in
                      sorted(rels.items(), key=lambda x: -x[1])],
        "dynasties": [{"id": d.id, "name": d.name, "span": list(d.span),
                       "lane": getattr(d, "lane", 0)} for d in store.dynasties],
        "events": [{"id": e.id, "name": e.name, "year": e.year,
                    "dynasty_id": e.dynasty_id, "from_book": e.from_book}
                   for e in store.events],
        "people": [{"id": p.id, "name": p.name, "years": list(p.years),
                    "dynasty_id": p.dynasty_id, "from_book": p.from_book}
                   for p in store.people],
        "places": [{"id": p.id, "name": p.name, "ancient": p.ancient}
                   for p in store.places],
        "books": [{"id": b.id, "title": b.title, "mounted": b.mounted}
                  for b in store.books],
        # 本体深化新增三类：供左侧本体导航浏览（无实体卡，关系在图谱浏览查看）
        "institutions": [{"id": x.id, "name": x.name, "kind": x.kind,
                          "dynasty_id": x.dynasty_id} for x in store.institutions],
        "artifacts": [{"id": x.id, "name": x.name, "kind": x.kind,
                       "year": x.year, "museum": x.museum}
                      for x in store.artifacts],
        "concepts": [{"id": x.id, "name": x.name, "kind": x.kind,
                      "dynasty_id": x.dynasty_id} for x in store.concepts],
    }


@router.get("/ontology/relations")
def ontology_relations():
    """规范 Link Types 目录：边类型、端点类型、逆关系、传递性与说明。"""
    pairs = [{"src": s, "dst": d} for s, d in sorted({(t.src, t.dst) for t in REL_TYPES})]
    return {
        "rel_types": [t.model_dump() for t in REL_TYPES],
        "pairs": pairs,
    }


# ---------------- 本体构建（在线；与离线 CLI 共用同一管线） ----------------
class OntologyBuildIn(BaseModel):
    text: str
    title: str | None = None
    # link=已知人物转对齐提案 | skip=跳过 | new=一律新建（见 BuildOptions）
    align_mode: str = "link"
    use_llm: bool = True          # 无真实 LLM 时自动走启发式
    embed: bool = False           # 是否计算切块向量（需真实 LLM）
    min_confidence: float = 0.0
    max_persons: int = 200
    max_events: int = 300


@router.post("/ontology/build")
def ontology_build(body: OntologyBuildIn):
    """在线构建本体提案（不落库）。

    与离线 `scripts/build_ontology.py` 调用同一条管线，返回结构与离线
    `--out` 产物完全一致，可原样落盘后再由 /api/ontology/import 导入挂载。
    """
    text = (body.text or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "文本过短（至少 20 字）")
    if body.align_mode not in ("link", "skip", "new"):
        raise HTTPException(400, "align_mode 必须是 link / skip / new")

    llm = get_llm() if body.use_llm else None
    # 本地 ONNX 模型优先；不可用时 get_embedder 回退云端 LLM，再不行返回 None → BM25
    embed_fn = get_embedder() if body.embed else None
    opts = BuildOptions(align_mode=body.align_mode,
                        min_confidence=body.min_confidence,
                        max_persons=body.max_persons,
                        max_events=body.max_events,
                        id_prefix="up_" + uuid.uuid4().hex[:12],
                        embed_fn=embed_fn)
    result = build_from_text(text, known=known_from_store(store), llm=llm,
                             resolver=resolver, opts=opts)
    return result.to_json({"title": body.title or "（在线构建）"})


class OntologyImportIn(BaseModel):
    payload: dict                 # 离线产物 JSON（build_ontology.py --out）
    title: str | None = None
    level: int = 2                # 挂载后史料层级：1 原始 | 2 现代史学 | 3 大众
    dynasty_id: str | None = None
    mount: bool = False           # 是否立即全量挂载


@router.post("/ontology/import")
def ontology_import(body: OntologyImportIn):
    """导入离线构建产物：建书（默认未挂载）+ 切块，可选直接挂载。

    离线跑 `build_ontology.py --out x.json` → 在线 POST 该 JSON 即可，
    无需在服务器上重跑解析与抽取。
    """
    try:
        proposals, chunks, meta = proposals_from_json(body.payload)
    except Exception as e:
        raise HTTPException(400, f"产物解析失败：{e}")

    bid = "up_" + uuid.uuid4().hex[:12]
    book_meta = meta.get("book") or {}
    valid_dyns = {d.id for d in store.dynasties}
    did = (body.dynasty_id or book_meta.get("dynasty_id")
           or meta.get("stats", {}).get("dynasty_hint") or "qing")
    if did not in valid_dyns:
        did = "qing"
    ev_years = [p.year for p in proposals
                if p.kind == "event" and p.year is not None]
    span = (min(ev_years), max(ev_years)) if ev_years else (0, 0)

    book = BookModel(
        id=bid,
        title=(body.title or book_meta.get("title") or "离线构建书源").strip()[:40],
        author=book_meta.get("author", "（离线构建）"),
        year=int(book_meta.get("year", ev_years[0] if ev_years else 0)),
        level=int(body.level), color="#0f766e",
        dynasty_id=did, span=span,
        tags=["离线构建", meta.get("mode", "heuristic")],
        description=(book_meta.get("description")
                     or f"离线管线构建：{len(chunks)} 切块 / {len(proposals)} 提案"),
        extracts=proposals, mounted=False,
        origin="upload", file=None)

    with store.transaction():
        store.set_chunks(bid, chunks)
        store.books.append(book)
        if body.mount and proposals:
            store.commit_book(book, [True] * len(proposals), persist=False)
        store.save()

    action_log.log("book.upload", {"book_id": bid, "filename": "ontology-build",
                                   "size": meta.get("stats", {}).get("chars", 0)})
    return {"book": book_dict(book), "chunks": len(chunks),
            "proposals": len(proposals), "extraction_mode": meta.get("mode")}


# ---------------- 内嵌图库浏览（Kuzu Cypher） ----------------
@router.get("/kg/schema")
def kg_schema():
    """节点表/关系表概览：计数 + 关系类型分布 + 悬空边数。"""
    return store.kg_schema()


@router.get("/kg/nodes")
def kg_nodes(table: str, q: str = "", limit: int = 40):
    """按表列节点 / 名称模糊搜索（图搜索框用）。"""
    return store.kg_nodes(table, q, min(max(limit, 1), 100))


@router.get("/kg/subgraph/{node_id}")
def kg_subgraph(node_id: str):
    """以节点为中心的一度邻域子图（点击节点展开）。"""
    g = store.kg_subgraph(node_id)
    if g is None:
        raise HTTPException(404, "node not found")
    return g


@router.get("/graph/query")
def graph_query(year_from: int | None = None, year_to: int | None = None,
                bbox: str | None = None, kind: str | None = None,
                book_id: str | None = None, limit: int = 200):
    """统一查询入口：按时间窗 / 空间窗 / 类型 / 来源书过滤实体。

    时间轴、地图、图谱三个视图共用同一入口——联动由接口保证，
    而不是靠前端在各视图间手动同步状态（见 docs/PLAN.md §2）。

    bbox 格式：`west,south,east,north`
    """
    box: tuple[float, float, float, float] | None = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            box = (parts[0], parts[1], parts[2], parts[3])
        except Exception:
            raise HTTPException(400, "bbox 格式应为 west,south,east,north")
    return {"items": warehouse.query_entities(
        year_from=year_from, year_to=year_to, bbox=box,
        kind=kind, book_id=book_id, limit=limit)}


@router.get("/kg/whole")
def kg_whole():
    """全图：全部 KGREL 端点与边。"""
    return store.kg_whole()


# ---------------- 对话（Agent 图） ----------------
class ChatIn(BaseModel):
    question: str
    audience: str = "scholar"
    agent_id: str | None = None


agent_registry = AgentRegistry()


@router.post("/chat")
def chat(body: ChatIn):
    # 指定 Agent：走 Agent 运行时（按授权工具执行；写操作返回确认卡）
    if body.agent_id:
        agent = agent_registry.get(body.agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        result = run_agent(agent, body.question)
        return {"answer": result["answer"],
                "operations": result["operations"],
                "agent_context": result["context"],
                "entity": None, "evidence": [], "warnings": [], "actions": []}

    final = chat_graph.invoke({"question": body.question, "audience": body.audience})
    ent = final.get("entity")
    entity_ref = None
    if ent is not None:
        entity_ref = {"kind": final["entity_kind"], "id": ent.id, "name": ent.name}

    # G3 接地校验：回答里的年份/专名必须能在证据原文中找到，否则显式告警
    evidence = final.get("evidence", []) or []
    names = ([p.name for p in store.people] + [e.name for e in store.events]
             + [pl.name for pl in store.places])
    # 实体卡自带的年份来自本体而非模型生成，不算幻觉，作为已知年份放行
    years: list[int] = []
    if ent is not None:
        y = getattr(ent, "year", None)
        if isinstance(y, (int, float)):
            years.append(int(y))
        for v in (getattr(ent, "years", None) or []):
            s = str(v).lstrip("?").strip()
            if s.lstrip("-").isdigit():
                years.append(int(s))
    gr = grounding_report(final.get("answer", ""),
                          [s.text for s in evidence], names,
                          known_years=years)
    warnings = list(final.get("warnings", []))
    if gr["has_evidence"] and not gr["grounded"]:
        warnings.append(
            f"该回答有 {gr['checked']} 处年份/专名未在史料原文中出现"
            f"（接地率 {gr['score']}），请以引用原文为准")
    return {"answer": final["answer"], "entity": entity_ref,
            "evidence": [src_dict(s) for s in evidence],
            "warnings": warnings, "grounding": gr,
            "actions": _plan_actions_llm(body.question, final.get("entity_kind"), ent)}


_ACTION_SPEC = """\
可用 UI 动作（JSON 数组，按执行顺序给出）：
- {"type":"set_year","year":整数}  时间轴跳转到某年（公元前用负数）
- {"type":"select_entity","kind":"event|person|place","id":"实体id"}  打开实体详情卡
- {"type":"show_route","id":"事件id"}  在地图上绘制该事件的行军/行进路线（事件需自带 route）
- {"type":"fly_to","lon":经度,"lat":纬度,"zoom":3-10}  地图飞移到指定位置
- {"type":"play","from":起始年,"to":结束年}  时间轴自动播放
- {"type":"pulse","lon":经度,"lat":纬度,"label":"标签"}  在某地点显示波纹强调
- {"type":"highlight_events","ids":["事件id",...]}  高亮多个事件（最多12个）
- {"type":"region","lon":经度,"lat":纬度,"km":半径公里,"label":"标签"}  圈出影响范围
- {"type":"dim","on":true|false}  让其余事件变暗以聚焦
- {"type":"shake"}  地图震动强调（适用于地震、突袭、剧变等场景）
- {"type":"clear_effects"}  清除所有动态效果

规划原则：从用户意图出发选择最有助于理解的动作组合，动作要少而精（一般2-5个），
通常以 clear_effects 开头；涉及空间移动务必 fly_to 到相关位置；有 route 的事件才能 show_route。"""


def _plan_actions_llm(question: str, kind: str | None, ent) -> list[dict]:
    """LLM 动作规划：让模型根据问题意图选择效果组合；失败回退规则版。"""
    if kind is None or ent is None:
        return []
    brief = {
        "kind": kind,
        "id": ent.id,
        "name": ent.name,
        "year": getattr(ent, "year", None),
        "lon": getattr(ent, "lon", None),
        "lat": getattr(ent, "lat", None),
        "has_route": bool(getattr(ent, "route", None)),
        "birth": getattr(ent, "years", [None])[0] if kind == "person" else None,
    }
    user = f"用户问题：{question}\n相关实体：{json.dumps(brief, ensure_ascii=False)}\n请只输出动作 JSON 数组。"
    try:
        llm = get_llm()
        raw = llm.chat(system=_ACTION_SPEC, user=user)
        arr = _extract_actions_json(raw)
        if arr is not None:
            return arr[:6]
    except Exception:
        pass
    return _plan_actions(question, kind, ent)


def _extract_actions_json(raw: str) -> list[dict] | None:
    """从 LLM 输出中提取动作 JSON 数组，容忍 markdown 代码块包裹。"""
    m = re.search(r"\[[\s\S]*\]", raw or "")
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    valid_types = {"set_year", "select_entity", "show_route", "fly_to", "play",
                   "pulse", "highlight_events", "region", "dim", "shake", "clear_effects"}
    return [a for a in arr if isinstance(a, dict) and a.get("type") in valid_types]


def _plan_actions(question: str, kind: str | None, ent) -> list[dict]:
    """规则版 UI 动作规划：Agent 根据解析出的实体驱动界面操作。
    动作类型与前端 App 的 actions 执行器一一对应。"""
    if kind is None or ent is None:
        return []
    actions: list[dict] = []
    q = question
    # 过程/路线类提问：若事件带 route，则跳到起始年 + 画路线
    route = getattr(ent, "route", None)
    if kind == "event" and route and any(
            k in q for k in ("过程", "进程", "路线", "怎么打", "如何", "经过", "发展", "进军", "西行", "下西洋")):
        actions.append({"type": "set_year", "year": ent.year})
        actions.append({"type": "select_entity", "kind": "event", "id": ent.id})
        actions.append({"type": "show_route", "id": ent.id})
        return actions
    if kind == "event":
        actions.append({"type": "set_year", "year": ent.year})
        actions.append({"type": "select_entity", "kind": "event", "id": ent.id})
        actions.append({"type": "fly_to", "lon": ent.lon, "lat": ent.lat, "zoom": 6})
        # 影响/波及类提问：圈出大致影响范围，并脉冲强调事发地
        if any(k in q for k in ("影响", "波及", "范围", "震动", "震撼")):
            km = 900 if any(k in ent.name for k in ("乱", "之战", "起义")) else 500
            actions.append({"type": "region", "lon": ent.lon, "lat": ent.lat,
                            "km": km, "label": f"{ent.name}·影响范围示意"})
            actions.append({"type": "pulse", "lon": ent.lon, "lat": ent.lat, "label": ent.name})
        # 同年相关事件高亮（让"当时的局势"类提问有空间感）
        if any(k in q for k in ("局势", "同时", "哪些", "背景")):
            same = [e.id for e in store.events
                    if e.id != ent.id and abs(e.year - ent.year) <= 5]
            if same:
                actions.append({"type": "highlight_events", "ids": same[:12]})
    elif kind == "person":
        # 人物：跳到生年中点（找不到则不动），选中实体卡，飞到籍贯/活动地
        y0 = ent.years[0].lstrip("?")
        if y0.lstrip("-").isdigit():
            actions.append({"type": "set_year", "year": int(y0)})
        actions.append({"type": "select_entity", "kind": "person", "id": ent.id})
        if getattr(ent, "lon", None) is not None:
            actions.append({"type": "fly_to", "lon": ent.lon, "lat": ent.lat, "zoom": 6})
    elif kind == "place":
        actions.append({"type": "select_entity", "kind": "place", "id": ent.id})
        actions.append({"type": "fly_to", "lon": ent.lon, "lat": ent.lat, "zoom": 7})
    return actions


# ---------------- 书库与挂载 ----------------
def book_dict(b) -> dict:
    return {"id": b.id, "title": b.title, "author": b.author, "year": b.year,
            "level": b.level, "color": b.color, "dynasty_id": b.dynasty_id,
            "span": list(b.span), "tags": b.tags, "description": b.description,
            "mounted": b.mounted,
            "origin": getattr(b, "origin", "builtin"),
            "accepted": getattr(b, "accepted", None),
            "extracts": [x.model_dump() for x in b.extracts]}


@router.get("/books")
def books():
    ing = {r["book_id"]: r for r in queue_status(store=store)}
    blist = []
    for b in store.books:
        d = book_dict(b)
        d["ingest"] = ing.get(b.id) or book_status(b.id, store=store)
        blist.append(d)
    return {"pipeline_stages": [s[1] for s in PIPELINE_STAGES],
            "books": blist}


# ---- 增量构建队列：内置书一本一本地叠加入库 ----
_STAGE_ORDER = {"pending": 0, "partial": 1, "prepared": 2, "chunked": 3,
                "no_source": 9}


@router.get("/books/queue")
def books_queue():
    """内置书的构建队列（pending → partial → prepared → 已挂载）。"""
    rows = queue_status(store=store)
    rows.sort(key=lambda r: (4 if r["mounted"] else
                             _STAGE_ORDER.get(r["stage"], 5), r["title"]))
    return {"queue": rows}


_INGEST_JOBS: dict[str, dict] = {}
_INGEST_LOCK = threading.Lock()


def _ingest_worker(job_id: str, book_id: str, opts: RunOptions) -> None:
    job = _INGEST_JOBS[job_id]
    try:
        def _prog(n: int, total: int) -> None:
            job["processed"], job["todo"] = n, total

        with _INGEST_LOCK:  # 单写者：同一时刻只构建一本书
            res = run_book(book_id, store=store, opts=opts, resolver=resolver,
                           on_progress=_prog)
        job.update({"state": "done", "result": {
            "title": res.title, "chunks": res.chunks,
            "pieces": res.pieces, "done": res.done,
            "processed": res.processed, "proposals": len(res.proposals),
            "by_kind": res.by_kind, "mode": res.mode, "model": res.model,
            "llm_pieces": res.stats.get("llm_pieces"),
            "fallback_pieces": res.stats.get("fallback_pieces"),
            "empty_pieces": res.stats.get("empty_pieces"),
            "error_pieces": res.stats.get("error_pieces"),
            "integrity": res.integrity,
            "kept_old_artifact": bool(res.stats.get("kept_old_artifact")),
            "artifact": res.artifact}})
    except Exception as e:      # 失败要留在 job 里，前端据此提示
        job.update({"state": "error", "error": str(e)})


class IngestIn(BaseModel):
    limit: int | None = None
    use_llm: bool = False
    force: bool = False
    embed: bool = False
    dry_run: bool = False
    concurrency: int = 6          # LLM 抽取并发数
    pieces_per_call: int = 1      # 每次调用送入的抽取块数
    # ---- 可靠性 / 完整性 ----
    retries: int = 2              # 单块失败重试次数
    repair: bool = False          # 自动多轮补齐缺口
    max_rounds: int = 3
    retry_empty: bool = True      # 把「抽到空」的块算作缺口
    retry_fallback: bool = True   # 把「大模型回退启发式」的块算作缺口
    strict: bool = False          # 覆盖不完整就报错
    keep_best: bool = True        # 提案变少时不覆盖旧产物


@router.post("/ingest/{book_id}")
def ingest_start(book_id: str, body: IngestIn | None = None):
    """在线逐本构建（后台线程）：切块 + 逐块抽取 + 生成提案，**不自动挂载**。"""
    b = body or IngestIn()
    if any(j["state"] == "running" for j in _INGEST_JOBS.values()):
        raise HTTPException(409, "已有构建任务在跑（单写者锁）：一次只构建一本书")
    job_id = uuid.uuid4().hex[:8]
    job = {"job_id": job_id, "book_id": book_id, "state": "running",
           "processed": 0, "todo": 0, "error": None, "result": None,
           "started_at": time.time()}
    _INGEST_JOBS[job_id] = job
    threading.Thread(
        target=_ingest_worker,
        args=(job_id, book_id,
              RunOptions(limit=b.limit, use_llm=b.use_llm, force=b.force,
                         embed=b.embed, dry_run=b.dry_run,
                         concurrency=max(1, b.concurrency),
                         pieces_per_call=max(1, b.pieces_per_call),
                         retries=max(1, b.retries), repair=b.repair,
                         max_rounds=max(1, b.max_rounds),
                         retry_empty=b.retry_empty,
                         retry_fallback=b.retry_fallback,
                         strict=b.strict, keep_best=b.keep_best)),
        daemon=True).start()
    return job


@router.get("/ingest")
def ingest_jobs():
    return {"jobs": list(_INGEST_JOBS.values()),
            "running": any(j["state"] == "running"
                           for j in _INGEST_JOBS.values())}


@router.get("/ingest/{book_id}")
def ingest_status(book_id: str):
    jobs = [j for j in _INGEST_JOBS.values() if j["book_id"] == book_id]
    return {"status": book_status(book_id, store=store), "jobs": jobs[-5:]}


@router.post("/ingest/{book_id}/apply")
def ingest_apply(book_id: str):
    """把离线产物 data/ingest/<book_id>.json 导入为 Book.extracts（**不挂载**）。

    离线跑 scripts/ingest_one.py（--no-store 只写 DuckDB 与产物）之后，
    用这个接口把提案接回线上，再走 /api/mounts/start → commit 人工确认。
    """
    from app.ingestion.pipeline import proposals_from_json

    art = Path(config.DATA_DIR) / "ingest" / f"{book_id}.json"
    if not art.exists():
        raise HTTPException(404, f"没有离线产物：{art}"
                                 "（先跑 scripts/ingest_one.py 生成）")
    data = json.loads(art.read_text(encoding="utf-8"))
    proposals = proposals_from_json(data)[0]
    book = next((b for b in store.books if b.id == book_id), None)
    if book is None:
        from app.ingestion.bookrun import book_from_manifest, load_manifest
        meta = load_manifest().get(book_id) or {}
        book = book_from_manifest(book_id, meta, proposals)
        store.books.append(book)
    else:
        if book.mounted:
            raise HTTPException(409, "该书已挂载，请先卸载再导入新提案")
        book.extracts = proposals
    store.save()
    cov = (data.get("stats") or {}).get("coverage")
    warns = []
    if isinstance(cov, (int, float)) and cov < 100:
        warns.append(f"离线产物只跑完 {cov}% 的抽取块：提案来自部分原文，"
                     "不是全书结果（跑全了再挂载更稳妥）")
    return {"ok": True, "book_id": book_id, "proposals": len(proposals),
            "mounted": book.mounted, "warnings": warns}


class MountStartIn(BaseModel):
    book_id: str


@router.post("/mounts/start")
def mount_start(body: MountStartIn):
    book = next((b for b in store.books if b.id == body.book_id), None)
    if book is None:
        raise HTTPException(404, "book not found")
    if book.mounted:
        raise HTTPException(400, "book already mounted; unmount first")
    thread_id = f"mount-{book.id}-{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread_id}}
    result = mount_graph.invoke({"book_id": book.id}, cfg)
    # 图在 await_approval 处中断；prepare 产物（proposals）已在状态中
    proposals = result.get("proposals", [])
    # 完整性提示：只跑了部分块的书，提案并不代表全书，挂载前让人看见
    st = book_status(book.id, store=store)
    rep = st.get("integrity") or {}
    warns: list[str] = []
    if rep and not rep.get("complete"):
        warns.append(f"《{book.title}》只跑完 {rep.get('done')}/{rep.get('total')}"
                     f" 块（{rep.get('coverage')}%）：当前提案来自部分原文")
    if st.get("stage") not in ("prepared", "mounted"):
        warns.append(f"构建状态为 {st.get('stage')}：建议先补齐再挂载")

    # 本体校验（SHACL）：挂载前把不合规的提案摆到人工确认界面上
    try:
        from app.ontology.validate import summarize, validate_proposals
        chk = validate_proposals(proposals) if proposals else None
    except Exception as e:                      # 校验器故障不阻塞挂载流程
        chk = None
        warns.append(f"本体校验不可用（{e.__class__.__name__}）")
    if chk and not chk.get("conforms"):
        warns.append(f"本体校验未通过：{summarize(chk)}")

    return {"thread_id": thread_id, "proposals": proposals,
            "pipeline_stages": [s[1] for s in PIPELINE_STAGES],
            "shacl": ({"conforms": chk["conforms"], "count": chk["count"],
                       "violations": chk["violations"][:20]} if chk else None),
            "warnings": warns}


class MountCommitIn(BaseModel):
    thread_id: str
    accepted: list[bool]
    strict: bool = Field(
        False, description="本体校验未通过时拒绝挂载（默认只告警）")


@router.post("/mounts/commit")
def mount_commit(body: MountCommitIn):
    cfg = {"configurable": {"thread_id": body.thread_id}}
    state = mount_graph.get_state(cfg)
    if not state.tasks:
        raise HTTPException(400, "no pending proposal for this thread")

    # 严格模式：本体校验未通过则拒绝挂载（默认只告警，由人工在界面上判断）
    if body.strict:
        try:
            from app.ontology.validate import summarize, validate_proposals
            proposals = (state.values or {}).get("proposals") or []
            if proposals:
                chk = validate_proposals(proposals)
                if not chk.get("conforms"):
                    raise HTTPException(
                        409, f"本体校验未通过，已拒绝挂载：{summarize(chk)}")
        except HTTPException:
            raise
        except Exception:
            pass                                # 校验器故障不阻断人工确认流程
    # commit 时书可能已被旁路删除：拒绝恢复
    bid_pre = state.values.get("book_id")
    if bid_pre and next((b for b in store.books if b.id == bid_pre), None) is None:
        raise HTTPException(400, "目标书籍已不存在，无法完成挂载")
    final = mount_graph.invoke(Command(resume=body.accepted), cfg)
    bid = final.get("book_id") or state.values.get("book_id")
    if bid and final.get("stage") == "committed":
        action_log.log("book.mount", {"book_id": bid},
                       final.get("result") or {})
    return {"result": final.get("result"), "stage": final.get("stage")}


class MountUnmountIn(BaseModel):
    book_id: str


@router.post("/mounts/unmount")
def mount_unmount(body: MountUnmountIn):
    book = next((b for b in store.books if b.id == body.book_id), None)
    if book is None:
        raise HTTPException(404, "book not found")
    if not book.mounted:
        raise HTTPException(400, "book not mounted")
    store.uncommit_book(book)
    action_log.log("book.unmount", {"book_id": book.id})
    return {"ok": True}


# ---------------- 上传 / 删除（trash 回收） ----------------
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("/library/upload")
async def library_upload(file: UploadFile = File(...),
                         title: str | None = Form(None)):
    """上传电子书：校验 → 落盘 → 解析 → 切块（在线并算向量）→ 本体抽取 → 建 Book（未挂载）。"""
    fname = file.filename or ""
    suffix = Path(fname).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, f"不支持的文件类型：{suffix or '（无扩展名）'}")
    token = uuid.uuid4().hex
    stored_name = f"{token}{suffix}"
    dest = _LIB_FILES / stored_name
    size = 0
    try:
        with open(dest, "wb") as f:
            while True:
                blob = await file.read(1 << 20)
                if not blob:
                    break
                size += len(blob)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "文件超过 50MB 限制")
                f.write(blob)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    try:
        parsed = await run_in_threadpool(parse_document, dest)
    except ParseError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"解析失败：{e}")
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "文档解析失败")
    text = parsed["text"]

    # 切块 / 向量化 / 抽取 / 落库：同步重活全部线程池化；任何异常都清理孤儿文件
    try:
        llm = get_llm()
        bid = "up_" + token[:12]
        # 与离线 CLI 同一条管线：仅 align_mode="link"（已知人物转对齐提案，
        # 不造重复节点）+ 前缀式 id，其余参数完全一致
        embed_fn = get_embedder()
        opts = BuildOptions(id_prefix=bid, person_id_mode="prefix",
                            align_mode="link", extract_mode="full",
                            embed_fn=embed_fn)
        result = await run_in_threadpool(
            build_from_text, text, known=known_from_store(store),
            llm=llm, resolver=resolver, opts=opts)

        chunks = result.chunks
        proposals = result.proposals
        ev_years = [p.year for p in proposals
                    if p.kind == "event" and p.year is not None]
        valid_dyns = {d.id for d in store.dynasties}
        did = result.stats.get("dynasty_hint") or "qing"
        if did not in valid_dyns:
            did = "qing"

        book_title = (title or Path(fname).stem).strip()[:40] or "未命名书籍"
        span = (min(ev_years), max(ev_years)) if ev_years else (0, 0)
        book = BookModel(
            id=bid, title=book_title, author="（上传书源）",
            year=ev_years[0] if ev_years else 0, level=3, color="#0f766e",
            dynasty_id=did, span=span,
            tags=["上传", parsed["kind"]],
            description=text[:120].replace("\n", " ").strip(),
            extracts=proposals, mounted=False,
            origin="upload", file=stored_name)

        store.set_chunks(bid, chunks)
        store.books.append(book)
        store.save()
        action_log.log("book.upload",
                       {"book_id": bid, "filename": fname, "size": size})
    except HTTPException:
        raise
    except Exception:
        # 落库前任何异常：删除已写入文件，避免孤儿
        dest.unlink(missing_ok=True)
        raise
    return {"book": book_dict(book), "chunks": len(chunks),
            "proposals": len(proposals),
            "extraction_mode": result.mode}


@router.delete("/library/{book_id}")
def library_delete(book_id: str):
    """删除上传书：文件与数据移入 trash（支持 undo 恢复）；内置书与已挂载书不可删。"""
    book = next((b for b in store.books if b.id == book_id), None)
    if book is None:
        raise HTTPException(404, "book not found")
    if getattr(book, "origin", "builtin") != "upload":
        raise HTTPException(403, "内置书籍不可删除，仅可卸载")
    try:
        tdir = store.trash_book(book)
    except ValueError as e:
        raise HTTPException(400, str(e))
    action_log.log("book.delete", {"book_id": book_id,
                                   "trash": str(tdir)})
    return {"ok": True, "trash": str(tdir)}


# ---------------- Action Types / 融合治理 ----------------
resolver = Resolver(store)


@router.get("/actions")
def actions():
    """动作定义目录 + 操作日志（seq/动作/参数/undone）。"""
    return action_log.payload()


@router.post("/actions/{seq}/undo")
def actions_undo(seq: int):
    try:
        return action_log.undo(seq)
    except KeyError:
        raise HTTPException(404, "action not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/merges")
def merges():
    """实体融合记录 + 疑似重复提案（kind=merge 卡片）。"""
    return resolver.payload()


# ---- 撤销处理器：挂载 ↔ 卸载 ----
def _undo_mount(entry: dict) -> dict:
    bid = entry["params"].get("book_id")
    book = next((b for b in store.books if b.id == bid), None)
    if book is None:
        raise ValueError("书籍已不存在，无法撤销挂载")
    if book.mounted:
        store.uncommit_book(book)
    return {"book_id": bid, "effect": "unmounted"}


def _undo_unmount(entry: dict) -> dict:
    bid = entry["params"].get("book_id")
    book = next((b for b in store.books if b.id == bid), None)
    if book is None:
        raise ValueError("书籍已不存在，无法撤销卸载")
    if not book.mounted:
        # 按用户最近一次确认的原位图重放；旧数据无位图时才回退全接受
        if book.accepted and len(book.accepted) == len(book.extracts):
            accepted = book.accepted
        else:
            accepted = [True] * len(book.extracts)
        store.commit_book(book, accepted)
    return {"book_id": bid, "effect": "mounted"}


action_log.set_handler("book.mount", _undo_mount)
action_log.set_handler("book.unmount", _undo_unmount)


def _undo_delete(entry: dict) -> dict:
    bid = entry["params"].get("book_id")
    try:
        store.restore_book(bid)
    except KeyError as e:
        raise ValueError(str(e))
    return {"book_id": bid, "effect": "restored"}


action_log.set_handler("book.delete", _undo_delete)


# ---------------- Agent 管理 ----------------
@router.get("/agents/tools")
def agent_tools():
    """工具勾选目录。"""
    return [{"id": k, **v} for k, v in TOOLS.items()]


@router.get("/agents")
def agents_list():
    return agent_registry.list()


class AgentIn(BaseModel):
    name: str
    system_prompt: str
    tools: list[str]


@router.post("/agents")
def agents_create(body: AgentIn):
    try:
        return agent_registry.create(body.name, body.system_prompt, body.tools)
    except ValueError as e:
        raise HTTPException(400, str(e))


class AgentPatch(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None


@router.put("/agents/{agent_id}")
def agents_update(agent_id: str, body: AgentPatch):
    try:
        updated = agent_registry.update(
            agent_id, name=body.name, system_prompt=body.system_prompt,
            tools=body.tools)
    except KeyError:
        raise HTTPException(404, "agent not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.tools is not None:
        pending_ops.cancel_unauthorized(agent_id)
    return updated


@router.delete("/agents/{agent_id}")
def agents_delete(agent_id: str):
    try:
        agent_registry.delete(agent_id)
    except KeyError:
        raise HTTPException(404, "agent not found")
    pending_ops.cancel_for_agent(agent_id)
    return {"ok": True}


# ---------------- 待确认操作（确认卡） ----------------
@router.get("/operations")
def operations_list(status: str | None = None):
    if status:
        return [o for o in pending_ops.ops if o["status"] == status]
    return pending_ops.ops


@router.post("/operations/{op_id}/confirm")
def operations_confirm(op_id: str):
    try:
        return confirm_operation(op_id)
    except KeyError:
        raise HTTPException(404, "operation not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/operations/{op_id}/cancel")
def operations_cancel(op_id: str):
    try:
        return cancel_operation(op_id)
    except KeyError:
        raise HTTPException(404, "operation not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
