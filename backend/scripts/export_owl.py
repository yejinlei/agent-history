# -*- coding: utf-8 -*-
"""把本体（app/schema/ontology.py）导出为 OWL，并声明到外部本体。

产物：app/ontology/chronosino.owl（Turtle，体积小，入库）

包含：
  · 类：8 个本体类 → owl:Class，并映射到 CIDOC CRM（Person→E21 等）
  · 属性：REL_TYPES → owl:ObjectProperty，逆属性 / 传递性一并表达
  · 时间：chrono:TimeSpan → OWL-Time time:Interval
    （注：OWL-Time 没有 TimeSpan，那是 CIDOC E52 的叫法，这里用 seeAlso 标明）
注意：域/值域约束不写在 OWL 里（多值 domain 在 OWL 中语义为交集，易误读），
     统一由 SHACL shapes 表达（app/ontology/shapes.py）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from rdflib import Graph, Literal, Namespace, RDF, URIRef  # noqa: E402
from rdflib.namespace import OWL, RDFS  # noqa: E402

from app.schema.ontology import REL_PAIRS, REL_TYPES  # noqa: E402

EX = Namespace("http://chronosino.local/ontology#")
CRM = Namespace("http://www.cidoc-crm.org/cidoc-crm/")
TIME = Namespace("http://www.w3.org/2006/time#")

Q = lambda s: quote(str(s), safe="")            # noqa: E731

# 本体类 → CIDOC CRM 类（对齐 docs/ONTOLOGY_ALIGN.md）
CLS_MAP = {
    "Dynasty": "E4",        # Period
    "Event": "E5",          # Event
    "Person": "E21",        # Person
    "Place": "E53",         # Place
    "Book": "E31",          # Document
    "Institution": "E74",   # Group
    "Artifact": "E22",      # Man-Made Object
    "Concept": "E28",       # Conceptual Object
}
CLASSES = tuple(CLS_MAP)


def main() -> int:
    g = Graph()
    g.bind("ex", EX)
    g.bind("crm", CRM)
    g.bind("time", TIME)
    g.bind("owl", OWL)

    onto = URIRef(EX + "")
    g.add((onto, RDF.type, OWL.Ontology))
    g.add((onto, RDFS.comment, Literal(
        "ChronoSino 本体：历史时序知识图谱的类与关系目录。"
        f"导出于 {datetime.now():%Y-%m-%d}。", lang="zh")))

    for cls in CLASSES:
        c = EX[Q(cls)]
        g.add((c, RDF.type, OWL.Class))
        g.add((c, RDFS.label, Literal(cls)))
        g.add((c, RDFS.subClassOf, CRM[Q(CLS_MAP[cls])]))

    n_prop = 0
    for rt in REL_TYPES:
        rel = getattr(rt, "rel", str(rt))
        p = EX[Q(rel)]
        g.add((p, RDF.type, OWL.ObjectProperty))
        g.add((p, RDFS.label, Literal(rel)))
        desc = getattr(rt, "desc", "") or ""
        if desc:
            g.add((p, RDFS.comment, Literal(desc)))
        srcs = sorted({a for a, _ in REL_PAIRS})
        tgts = sorted({b for _, b in REL_PAIRS})
        if len(srcs) == 1:
            g.add((p, RDFS.domain, EX[Q(srcs[0])]))
        if len(tgts) == 1:
            g.add((p, RDFS.range, EX[Q(tgts[0])]))
        inv = getattr(rt, "inverse", None)
        if inv:
            g.add((p, OWL.inverseOf, EX[Q(inv)]))
        if getattr(rt, "transitive", False):
            g.add((p, RDF.type, OWL.TransitiveProperty))
        n_prop += 1

    ts = EX["TimeSpan"]
    g.add((ts, RDF.type, OWL.Class))
    g.add((ts, RDFS.label, Literal("时期区间")))
    g.add((ts, RDFS.subClassOf, TIME["Interval"]))
    g.add((ts, RDFS.seeAlso, CRM["E52"]))

    out = _BACKEND / "app" / "ontology" / "chronosino.owl"
    out.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(out.as_posix(), format="turtle")
    print(f"导出 {len(g)} 三元组：{len(CLASSES)} 个类，{n_prop} 个对象属性")
    print(f"→ {out}（{out.stat().st_size/1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
