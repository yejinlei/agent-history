# -*- coding: utf-8 -*-
"""临时可行性验证：本体论工具（rdflib / pySHACL）能做什么、有多快。

验证四点：
  1. 能否解析已下载的 CIDOC CRM OWL 与 OWL-Time RDF（外部本体可用）
  2. 能否从 app/schema/ontology.py 的 REL_PAIRS 自动生成 SHACL shapes（域/值域约束）
  3. SHACL 能否自动检出真实问题（悬空边 / 类型不匹配 / 缺证据）
  4. 性能：对真实规模数据跑校验要多久
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import rdflib
from rdflib import Graph, Namespace, RDF, URIRef, Literal

print("=" * 60)
print("1) 外部本体可解析性")

cidoc = _BACKEND / "app/data/core/raw/cidoc/cidoc_crm.owl"
owltime = _BACKEND / "app/data/core/raw/owltime/owl_time.rdf"

for p, fmt in ((cidoc, "xml"), (owltime, "xml")):
    if not p.exists():
        print(f"  缺少 {p}")
        continue
    t0 = time.time()
    g = Graph()
    try:
        g.parse(p.as_posix(), format=fmt)
    except Exception as e:
        print(f"  {p.name}: 解析失败 {e.__class__.__name__}: {str(e)[:80]}")
        continue
    classes = set(g.subjects(RDF.type, rdflib.OWL.Class)) | \
        set(g.subjects(RDF.type, rdflib.RDFS.Class))
    obj_props = set(g.subjects(RDF.type, rdflib.OWL.ObjectProperty))
    print(f"  {p.name}: {len(g)} 三元组，{len(classes)} 个类，"
          f"{len(obj_props)} 个对象属性，用时 {time.time()-t0:.1f}s")
    if "owl_time" in p.name:
        names = sorted({str(s).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                        for s in classes})
        print(f"     OWL-Time 类名：{names[:24]}")
    # 找我们对齐要用到的类/属性
    want = ["E21", "E5", "E53", "P11", "TimeSpan", "TimeInstant"]
    hit = {w: any(w in str(s) for s in classes | obj_props
                  | set(g.subjects())) for w in want}
    print(f"     关键类/属性命中：{hit}")

print()
print("=" * 60)
print("2) 从 REL_PAIRS 自动生成 SHACL（域/值域约束）")

try:
    from app.schema.ontology import REL_PAIRS, REL_TYPES
    print(f"  本体关系目录：REL_TYPES {len(REL_TYPES)} 种，"
          f"REL_PAIRS {len(REL_PAIRS)} 组端点组合")
    sample = list(REL_PAIRS)[:5]
    print(f"  示例组合：{sample}")
except Exception as e:
    print(f"  读取 ontology.py 失败：{e.__class__.__name__}: {e}")
    REL_PAIRS = set()
    REL_TYPES = []

# 生成 shapes：对每种 (域, 值域) 组合声明属性约束
SH = Namespace("http://www.w3.org/ns/shacl#")
EX = Namespace("http://chronosino.local/ontology#")
shapes_g = Graph()
shapes_g.bind("sh", SH)
shapes_g.bind("ex", EX)

made = 0
try:
    from urllib.parse import quote
    from app.schema.ontology import REL_TYPES as _RT

    rel_names = [getattr(r, "rel", r if isinstance(r, str) else str(r))
                 for r in REL_TYPES]
    src_classes = sorted({a for a, _ in REL_PAIRS})
    for rel_id in rel_names:
        prop = EX[quote(rel_id.replace(" ", "_"), safe="")]
        for src in src_classes:
            targets = [b for a, b in REL_PAIRS if a == src]
            if not targets:
                continue
            node = URIRef("urn:shape:" + quote(f"{rel_id}:{src}", safe=""))
            shapes_g.add((node, RDF.type, SH.NodeShape))
            shapes_g.add((node, SH.targetClass, EX[src]))
            ps = URIRef("urn:ps:" + quote(f"{rel_id}:{src}", safe=""))
            shapes_g.add((node, SH.property, ps))
            shapes_g.add((ps, SH.path, prop))
            # sh:or：允许的目标类集合（等价于 REL_PAIRS 的域/值域约束）
            or_list = Graph()
            items = []
            for t in targets:
                n = URIRef("urn:t:" + quote(f"{rel_id}:{src}:{t}", safe=""))
                or_list.add((n, SH["class"], EX[t]))
                items.append(n)
            lst = rdflib.collection.Collection(
                or_list,
                URIRef("urn:or:" + quote(f"{rel_id}:{src}", safe="")), items)
            shapes_g.add((ps, SH["or"], lst.uri))
            shapes_g += or_list
            made += 1
except Exception as e:
    print(f"  生成 shapes 失败：{e.__class__.__name__}: {e}")
print(f"  自动生成 shape 数：{made}（shapes 图 {len(shapes_g)} 三元组）")

print()
print("=" * 60)
print("3) SHACL 能否检出真实问题")

# 用真实悬空边 + 合法边构造数据图
data = Graph()
data.bind("ex", EX)
dang_p = _BACKEND / "app/data/kg/dangling_edges.json"
dangling = []
if dang_p.exists():
    dangling = json.loads(dang_p.read_text(encoding="utf-8"))
print(f"  真实悬空边：{len(dangling)} 条")

# 合法节点
for nid, cls in (("xiangyu_demo", "Person"), ("gaixia", "Event")):
    data.add((EX[nid], RDF.type, EX[cls]))
# 一条合法边 + 若干悬空边（端点无类型声明 = 端点不存在）
data.add((EX["xiangyu_demo"], EX["发动"], EX["gaixia"]))
for d in dangling[:50]:
    a, b, rel = d.get("a"), d.get("b"), d.get("rel")
    if a and b and rel:
        data.add((EX[a.replace(" ", "_")], EX[rel.replace(" ", "_")],
                  EX[b.replace(" ", "_")]))
print(f"  数据图：{len(data)} 三元组")

# 简单 shape：所有边对象必须是有类型的节点（等价于「端点必须存在」）
shapes_g2 = Graph()
shapes_g2.bind("sh", SH)
shapes_g2.bind("ex", EX)
for rel in ("发动", "败于", "建立", "出使"):
    for dom, rng in (("Person", "Event"), ("Person", "Person")):
        node = URIRef(f"urn:s:{rel}:{dom}->{rng}")
        shapes_g2.add((node, RDF.type, SH.NodeShape))
        shapes_g2.add((node, SH.targetSubjectsOf, EX[rel]))
        ps = URIRef(f"urn:ps2:{rel}:{dom}->{rng}")
        shapes_g2.add((node, SH.property, ps))
        shapes_g2.add((ps, SH.path, EX[rel]))
        shapes_g2.add((ps, SH["class"], EX[rng]))
        shapes_g2.add((ps, SH.severity, SH.Warning))

try:
    from pyshacl import validate
    t0 = time.time()
    conforms, _, report = validate(
        data_graph=data, shacl_graph=shapes_g2,
        inference="none", abort_on_first=False,
        advanced=True, serialize_report_graph=False)
    dt = time.time() - t0
    txt = report if isinstance(report, str) else str(report)
    n = txt.count("sh:result") or txt.count("Validation Result")
    print(f"  pySHACL 校验：conforms={conforms}，用时 {dt*1000:.0f} ms")
    print(f"  报告片段：{txt[:400]}")
except Exception as e:
    print(f"  pySHACL 校验失败：{e.__class__.__name__}: {e}")

print()
print("=" * 60)
print("4) 性能：真实规模（构造 N 条边）校验耗时")
try:
    from pyshacl import validate as _v
    for n in (1000, 10000, 50000):
        big = Graph()
        big.bind("ex", EX)
        big.add((EX["xiangyu_demo"], RDF.type, EX["Person"]))
        big.add((EX["gaixia"], RDF.type, EX["Event"]))
        for i in range(n):
            big.add((EX[f"p{i}"], RDF.type, EX["Person"]))
            big.add((EX[f"e{i}"], RDF.type, EX["Event"]))
            big.add((EX[f"p{i}"], EX["发动"], EX[f"e{i}"]))
        t0 = time.time()
        _v(data_graph=big, shacl_graph=shapes_g2, inference="none",
           abort_on_first=False)
        print(f"  {n:>6} 条边（{len(big)} 三元组）：{time.time()-t0:.2f}s")
except Exception as e:
    print(f"  性能测试失败：{e.__class__.__name__}: {e}")
