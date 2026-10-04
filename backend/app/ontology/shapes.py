# -*- coding: utf-8 -*-
"""SHACL shapes：本体约束的可声明形式。

两部分组成：
  1. **自动生成**：由 `REL_TYPES` / `REL_PAIRS` 派生域/值域约束
     （哪种关系只能从哪类指向哪类）——本体改了约束自动跟着改，不会双写漂移
  2. **手写规则**：数据质量底线（必须有名称、端点必须真的存在、
     源于书的实体必须有史料证据、external_ids 命名空间白名单、provenance 取值合法）

校验入口见 `app/ontology/validate.py`。
"""
from __future__ import annotations

from urllib.parse import quote

import rdflib
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.collection import Collection

from app.schema.ontology import REL_PAIRS, REL_TYPES

EX = Namespace("http://chronosino.local/ontology#")
SH = Namespace("http://www.w3.org/ns/shacl#")
XSD = Namespace("http://www.w3.org/2001/XMLSchema#")

Q = lambda s: quote(str(s), safe="")            # noqa: E731  URI 安全转义
_CLASSES = ("Dynasty", "Event", "Person", "Place", "Book",
            "Institution", "Artifact", "Concept")

# G5 外部锚点命名空间白名单（与 verify.py 保持一致）
EXTERNAL_NS = ("wikidata", "cbdb", "chgis", "tgn", "geonames", "periodo",
               "viaf", "lcna")
# 来源层级取值（可废止推理的优先级依据）
PROVENANCE_OK = ("core", "seed", "book", "llm")


def rel_names() -> list[str]:
    return [getattr(r, "rel", str(r)) for r in REL_TYPES]


def build_shapes(include_domain_range: bool = True) -> Graph:
    """生成 SHACL shapes 图。"""
    g = Graph()
    g.bind("sh", SH)
    g.bind("ex", EX)
    g.bind("xsd", XSD)

    # ---- 1. 域 / 值域（由关系目录自动生成）----
    if include_domain_range:
        for rel in rel_names():
            for src in sorted({a for a, _ in REL_PAIRS}):
                targets = sorted({b for a, b in REL_PAIRS if a == src})
                if not targets:
                    continue
                tag = f"{rel}::{src}"
                node = URIRef("urn:shape:" + Q(tag))
                g.add((node, RDF.type, SH.NodeShape))
                g.add((node, SH.targetClass, EX[Q(src)]))
                ps = URIRef("urn:ps:" + Q(tag))
                g.add((node, SH.property, ps))
                g.add((ps, SH.path, EX[Q(rel)]))
                if len(targets) == 1:
                    g.add((ps, SH["class"], EX[Q(targets[0])]))
                else:
                    sub = Graph()
                    items = []
                    for t in targets:
                        n = URIRef("urn:t:" + Q(f"{tag}:{t}"))
                        sub.add((n, SH["class"], EX[Q(t)]))
                        items.append(n)
                    g.add((ps, SH["or"],
                           Collection(sub, URIRef("urn:or:" + Q(tag)),
                                      items).uri))
                    g += sub
                g.add((ps, SH.severity, SH.Violation))

    # ---- 2. 端点必须真的存在（悬空边检测）----
    for rel in rel_names():
        tag = Q(rel)
        node = URIRef("urn:shape:endpoint:" + tag)
        g.add((node, RDF.type, SH.NodeShape))
        g.add((node, SH.targetSubjectsOf, EX[tag]))
        ps = URIRef("urn:ps:endpoint:" + tag)
        g.add((node, SH.property, ps))
        g.add((ps, SH.path, EX[tag]))
        # 对象必须有 rdf:type（即已作为实体存在）
        inner = URIRef("urn:shape:objtyped:" + tag)
        g.add((ps, SH.node, inner))
        g.add((inner, RDF.type, SH.NodeShape))
        ips = URIRef("urn:ps:objtyped:" + tag)
        g.add((inner, SH.property, ips))
        g.add((ips, SH.path, RDF.type))
        g.add((ips, SH.minCount, Literal(1)))
        g.add((ps, SH.severity, SH.Violation))

    # ---- 3. 实体必须有名称 ----
    for cls in _CLASSES:
        node = URIRef("urn:shape:name:" + cls)
        g.add((node, RDF.type, SH.NodeShape))
        g.add((node, SH.targetClass, EX[Q(cls)]))
        ps = URIRef("urn:ps:name:" + cls)
        g.add((node, SH.property, ps))
        g.add((ps, SH.path, EX.name))
        g.add((ps, SH.minCount, Literal(1)))
        g.add((ps, SH.datatype, XSD.string))
        g.add((ps, SH.severity, SH.Violation))

    # ---- 4. G2：来源于书的实体必须有史料证据 ----
    # targetSubjectsOf：选中数据图里所有带 ex:fromBook 的实体
    node = URIRef("urn:shape:evidence")
    g.add((node, RDF.type, SH.NodeShape))
    g.add((node, SH.targetSubjectsOf, EX.fromBook))
    ps = URIRef("urn:ps:evidence")
    g.add((node, SH.property, ps))
    g.add((ps, SH.path, EX.hasSource))
    g.add((ps, SH.minCount, Literal(1)))
    g.add((ps, SH.severity, SH.Violation))

    # ---- 5. G5：external_ids 命名空间必须合法 ----
    node = URIRef("urn:shape:external-ns")
    g.add((node, RDF.type, SH.NodeShape))
    ps = URIRef("urn:ps:external-ns")
    g.add((node, SH.property, ps))
    g.add((ps, SH.path, EX.externalNs))
    g.add((ps, SH["in"], Collection(g, URIRef("urn:list:external-ns"),
                                    [Literal(x) for x in EXTERNAL_NS]).uri))
    g.add((ps, SH.severity, SH.Warning))

    # ---- 6. provenance 取值必须合法 ----
    node = URIRef("urn:shape:provenance")
    g.add((node, RDF.type, SH.NodeShape))
    ps = URIRef("urn:ps:provenance")
    g.add((node, SH.property, ps))
    g.add((ps, SH.path, EX.provenance))
    g.add((ps, SH["in"], Collection(g, URIRef("urn:list:provenance"),
                                    [Literal(x) for x in PROVENANCE_OK]).uri))
    g.add((ps, SH.severity, SH.Warning))

    return g
