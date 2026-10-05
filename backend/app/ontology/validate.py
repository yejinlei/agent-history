# -*- coding: utf-8 -*-
"""SHACL 校验入口：把内存对象/提案转成 RDF 数据图，再用 shapes 校验。

用法：
    >>> from app.ontology.validate import validate_proposals, validate_store
    >>> res = validate_proposals(proposals)     # 挂载前校验提案
    >>> res = validate_store(store)             # 全量体检（含悬空边）
    >>> res["conforms"], res["violations"]
"""
from __future__ import annotations

from collections import Counter
from urllib.parse import quote

from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD

from app.ontology.shapes import EX, build_shapes

Q = lambda s: quote(str(s), safe="")            # noqa: E731
SH = Namespace("http://www.w3.org/ns/shacl#")

# 提案 kind → 本体类
_KIND2CLS = {
    "person": "Person", "event": "Event", "place": "Place",
    "dynasty": "Dynasty", "institution": "Institution",
    "artifact": "Artifact", "concept": "Concept", "book": "Book",
}

# 不是节点、而是「边」的提案类型（见 data_graph_from_proposals）
_EDGE_KINDS = {"link", "relation"}


def _dump(p) -> dict:
    if isinstance(p, dict):
        return p
    if hasattr(p, "model_dump"):
        try:
            return p.model_dump()
        except Exception:
            pass
    return dict(getattr(p, "__dict__", {}) or {})


def _add_node(g: Graph, oid: str, cls: str, d: dict) -> None:
    n = URIRef(EX[Q(oid)])
    g.add((n, RDF.type, EX[Q(cls)]))
    if d.get("name"):
        g.add((n, EX.name, Literal(str(d["name"]), datatype=XSD.string)))
    if d.get("provenance"):
        g.add((n, EX.provenance, Literal(str(d["provenance"]))))
    ext = d.get("external_ids") or d.get("externals") or {}
    if isinstance(ext, dict):
        for k in ext:
            g.add((n, EX.externalNs, Literal(str(k))))
    # G2：来自书的实体必须有史料引文，两者分别标记才能校验
    if d.get("from_book"):
        g.add((n, EX.fromBook, Literal(True)))
    if d.get("sources") or d.get("src_ids"):
        g.add((n, EX.hasSource, Literal(True)))


def data_graph_from_proposals(proposals: list) -> Graph:
    """提案列表 → RDF 数据图（校验 G2 证据、名称、锚点命名空间）。"""
    g = Graph()
    g.bind("ex", EX)
    # 节点在数据图里按 name 标识（历史约定），关系边却用 new_id / target_id。
    # 两套 id 必须先对齐，否则三元组会指向一个没有 rdf:type 的 URI，SHACL 会把
    # 这批新关系整批判成违规（实测 584972 处，全是这个错配）。
    oid_of_newid: dict[str, str] = {}
    edges: list[tuple[str, dict]] = []
    for p in proposals:
        d = _dump(p)
        kind = str(d.get("kind") or d.get("type") or "").lower()
        if kind in _EDGE_KINDS:
            # 关系类提案不是节点：link=把史料挂到主干实体，relation=实体间语义边。
            # 早期把它们当成 Concept 节点塞进数据图，等于拿边冒充实体去做体检。
            edges.append((kind, d))
            continue
        cls = _KIND2CLS.get(kind, "Concept")
        oid = str(d.get("oid") or d.get("id") or d.get("name") or "")
        if oid:
            _add_node(g, oid, cls, d)
            nid = d.get("new_id")
            if nid:
                oid_of_newid[str(nid)] = oid
    for kind, d in edges:
        if kind != "relation":
            continue
        src, dst = str(d.get("new_id") or ""), str(d.get("target_id") or "")
        # 只体检两端都在这批提案里的关系：指向外部实体的本来就落成不了边，
        # 放进来只会制造噪音
        if src not in oid_of_newid or dst not in oid_of_newid:
            continue
        g.add((URIRef(EX[Q(oid_of_newid[src])]),
               URIRef(EX[Q(str(d.get("rel") or "related"))]),
               URIRef(EX[Q(oid_of_newid[dst])])))
    return g


def data_graph_from_store(store) -> Graph:
    """内存图谱 → RDF 数据图（全量体检：节点 + KGREL 边）。"""
    g = Graph()
    g.bind("ex", EX)

    def add(obj, cls):
        if not getattr(obj, "id", None):
            return
        _add_node(g, str(obj.id), cls, {
            "name": getattr(obj, "name", "") or getattr(obj, "title", ""),
            "provenance": getattr(obj, "provenance", ""),
            "external_ids": getattr(obj, "external_ids", {}) or {},
            "from_book": getattr(obj, "from_book", None),
            "sources": getattr(obj, "sources", None),
            "src_ids": getattr(obj, "src_ids", None),
        })

    for attr, cls in (("dynasties", "Dynasty"), ("events", "Event"),
                      ("people", "Person"), ("places", "Place"),
                      ("institutions", "Institution"),
                      ("artifacts", "Artifact"), ("concepts", "Concept"),
                      ("books", "Book")):
        for o in getattr(store, attr, []):
            add(o, cls)

    for e in getattr(store, "edges", []):
        if isinstance(e, dict):
            a, b, rel = e.get("a"), e.get("b"), e.get("rel")
        else:
            a = getattr(e, "a", None)
            b = getattr(e, "b", None)
            rel = getattr(e, "rel", None)
        if a and b and rel:
            g.add((URIRef(EX[Q(a)]), EX[Q(rel)], URIRef(EX[Q(b)])))
    return g


def _run(data: Graph, shapes: Graph | None = None) -> dict:
    from pyshacl import validate as _validate

    conforms, _, report = _validate(
        data_graph=data, shacl_graph=shapes or build_shapes(),
        inference="none", abort_on_first=False, advanced=True)
    if conforms:                      # 通过时 report 是纯文本，无需解析
        return {"conforms": True, "triples": len(data), "count": 0,
                "violations": []}
    txt = report if isinstance(report, str) else report.decode(
        "utf-8", "replace")
    violations: list[dict] = []
    try:
        rg = Graph()
        rg.parse(data=txt, format="turtle")
        for r in rg.subjects(RDF.type, SH.ValidationResult):
            violations.append({
                "focus": str(rg.value(r, SH.focusNode) or "")
                .rsplit("/", 1)[-1],
                "path": str(rg.value(r, SH.resultPath) or "")
                .rsplit("/", 1)[-1],
                "value": str(rg.value(r, SH.value) or "").rsplit("/", 1)[-1],
                "severity": str(rg.value(r, SH.resultSeverity) or "")
                .rsplit("#", 1)[-1],
                "message": str(rg.value(r, SH.resultMessage) or "")[:160],
            })
    except Exception:
        for line in txt.splitlines():
            if line.strip():
                violations.append({"raw": line.strip()[:160]})
    return {"conforms": bool(conforms), "triples": len(data),
            "count": len(violations), "violations": violations}


def validate_proposals(proposals: list) -> dict:
    return _run(data_graph_from_proposals(proposals))


def validate_store(store) -> dict:
    return _run(data_graph_from_store(store))


def summarize(res: dict, limit: int = 6) -> str:
    """给人看的一句话摘要。"""
    if res["conforms"]:
        return f"OK（{res['triples']} 三元组，0 违规）"
    head = f"{res['count']} 处违规（{res['triples']} 三元组）："
    kinds = Counter(v.get("path") or v.get("raw", "")[:20]
                    for v in res["violations"])
    detail = "；".join(f"{k}×{n}" for k, n in kinds.most_common(limit))
    return f"{head} {detail}"
