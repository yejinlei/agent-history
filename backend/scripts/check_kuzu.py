# -*- coding: utf-8 -*-
"""只读验证 Kuzu 图库（后端运行中也可执行）"""
from pathlib import Path

import kuzu

DB = Path(__file__).resolve().parent.parent / "app" / "data" / "kg" / "kuzu_db"
db = kuzu.Database(str(DB), read_only=True)
c = kuzu.Connection(db)

def q(sql, params=None):
    r = c.execute(sql, params) if params else c.execute(sql)
    rows = []
    while r.has_next():
        rows.append(r.get_next())
    return rows

print("唐朝 KGREL 邻接:")
for row in q("MATCH (d:Dynasty {id:'tang'})-[r:KGREL]-(n) RETURN n.name, r.rel"):
    print(" ", row)

print("张骞 -> 事件:")
for row in q("MATCH (p:Person {id:'zhangqian'})-[r:KGREL]->(e:Event) RETURN e.name, r.rel"):
    print(" ", row)

print("逐鹿/涿鹿之战 参与者:")
for row in q("MATCH (p:Person)-[r:KGREL]->(e:Event {id:'zhuolu'}) RETURN p.name, r.rel"):
    print(" ", row)

print("边类型分布:")
for row in q("MATCH ()-[r:KGREL]->() RETURN r.rel AS rel, count(*) AS c ORDER BY c DESC"):
    print(" ", row)

print("各节点表规模:")
for t in ["Dynasty", "Event", "Person", "Place", "Source", "Book"]:
    print(" ", t, q(f"MATCH (n:{t}) RETURN count(n)")[0][0])
