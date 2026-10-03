# -*- coding: utf-8 -*-
"""端到端冒烟：启服后一条命令验证「书 → 抽取 → 挂载 → 检索 / 对话 / 图谱 / 地图」全链路。

只读校验（不写库、不挂载、不调写接口），可反复执行；
每加一本书后跑一遍，确认叠加没有打断既有链路。

用法：
    python scripts/e2e_flow.py                       # 默认 http://127.0.0.1:8000
    python scripts/e2e_flow.py --book shiji          # 额外校验指定书的状态
    python scripts/e2e_flow.py --chat                # 连 /api/chat 一起验（会调 LLM，慢）
    python scripts/e2e_flow.py --json                # 机器可读输出
退出码：0 全通过 / 1 有 FAIL / 2 服务不可达
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

FAILS: list[str] = []
WARNS: list[str] = []
ROWS: list[dict] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    ROWS.append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    WARNS.append(name)
    ROWS.append({"name": name, "ok": True, "warn": True, "detail": detail})
    print("WARN " + name + (f"  {detail}" if detail else ""))


class Api:
    def __init__(self, base: str, timeout: float):
        self.base = base.rstrip("/")
        self.cli = httpx.Client(base_url=self.base, timeout=timeout,
                                headers={"Accept-Encoding": "identity"})

    def get(self, path: str, **params):
        return self.cli.get("/api" + path, params={k: v for k, v in params.items()
                                                   if v is not None})

    def post(self, path: str, payload: dict):
        return self.cli.post("/api" + path, json=payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--book", default="", help="额外校验的书 id（如 shiji）")
    ap.add_argument("--query", default="", help="检索/对话用的关键词，默认自动取一个事件名")
    ap.add_argument("--chat", action="store_true", help="校验 /api/chat（会调 LLM）")
    ap.add_argument("--tiles", action="store_true", help="校验本地瓦片代理")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    api = Api(a.base, a.timeout)

    # ---- 1. 服务活着 ----
    try:
        r = api.get("/health")
    except Exception as e:                                  # 服务没起来
        print(f"FAIL health  服务不可达：{e.__class__.__name__}")
        print(f"\n提示：先启服（scripts/serve.ps1 或 uvicorn app.main:app --port 8000）")
        return 2
    health = r.json() if r.status_code == 200 else {}
    rec("health", r.status_code == 200 and health.get("ok") is True,
        f"llm={health.get('llm')}")

    # ---- 2. 书与队列 ----
    books = api.get("/books").json() if api.get("/books").status_code == 200 else {}
    blist = books.get("books", [])
    rec("books", len(blist) > 0, f"{len(blist)} 本")
    mounted = [b for b in blist if b.get("ingest", {}).get("mounted")]
    rec("books/mounted", len(mounted) > 0, f"{len(mounted)} 本已挂载")

    if a.book:
        b = next((x for x in blist if x.get("id") == a.book), None)
        st = (b or {}).get("ingest", {})
        rec(f"book[{a.book}]", b is not None,
            f"stage={st.get('stage')} {st.get('done')}/{st.get('pieces')} "
            f"提案{st.get('proposals')} mounted={st.get('mounted')}")
        integ = st.get("integrity") or {}
        if integ and not integ.get("complete"):
            warn(f"book[{a.book}]/coverage",
                 f"{integ.get('coverage')}%（缺{integ.get('missing')} 空{integ.get('empty')} "
                 f"退{integ.get('fallback')} 错{integ.get('error')}）")

    q = api.get("/books/queue").json() if api.get("/books/queue").status_code == 200 else {}
    rec("books/queue", len(q.get("queue", [])) > 0, f"{len(q.get('queue', []))} 本在队列")

    # ---- 3. 时间线 ----
    dyns = api.get("/dynasties").json() if api.get("/dynasties").status_code == 200 else []
    rec("dynasties", len(dyns) > 0, f"{len(dyns)} 个政权")
    if not dyns:
        rec("timeline", False, "没有朝代数据")
        return 1

    r = api.get("/events")
    ev_all = r.json() if r.status_code == 200 else []
    rec("events", r.status_code == 200 and len(ev_all) > 0, f"{len(ev_all)} 条")

    # 取全量事件的中位年份做样本：旧石器这类极早年没有实体，
    # 用它当过滤条件会误判成「链路断了」。
    year = int(ev_all[len(ev_all) // 2].get("year") or -200) if ev_all else -200

    tl = api.get("/timeline", year=year)
    tld = tl.json() if tl.status_code == 200 else {}
    rec("timeline", tl.status_code == 200 and bool(tld.get("dynasty")),
        f"year={year} {tld.get('dynasty', {}).get('name')} events={len(tld.get('events', []))}")
    if tl.status_code == 200 and not tld.get("events"):
        warn("timeline/events", f"{year} 年无事件（该年可能确实无史料事件）")
    dynasty_id = (tld.get("dynasty") or {}).get("id") or dyns[0]["id"]

    # ---- 4. 检索 → 实体卡 ----
    term = a.query or (ev_all[0]["name"] if ev_all else "")
    s = api.get("/search", q=term)
    hits = s.json() if s.status_code == 200 else []
    rec("search", s.status_code == 200 and len(hits) > 0, f"q={term!r} {len(hits)} 命中")
    if hits:
        h = hits[0]
        e = api.get(f"/entity/{h['kind']}/{h['id']}")
        ed = e.json() if e.status_code == 200 else {}
        rec("entity", e.status_code == 200 and ed.get("kind") == h["kind"],
            f"{h['kind']}/{h['id']} 边{len(ed.get('edges', []))} "
            f"证据{len(ed.get('sources', ed.get('evidence', [])) or [])}")
    else:
        rec("entity", False, "无命中，无法校验实体卡")

    # ---- 5. 图库 ----
    sch = api.get("/kg/schema").json() if api.get("/kg/schema").status_code == 200 else {}
    tables = [t for t in sch.get("tables", []) if t.get("count")]
    rec("kg/schema", bool(tables) and sch.get("edge_count", 0) > 0,
        f"{len(tables)} 张非空表 边{sch.get('edge_count')} 悬空{sch.get('dangling')}")
    if sch.get("dangling"):
        warn("kg/dangling", f"{sch['dangling']} 条悬空边（端点节点缺失）")

    if tables:
        t = tables[0]["label"]
        n = api.get("/kg/nodes", table=t, limit=5)
        rec("kg/nodes", n.status_code == 200 and len(n.json()) > 0, f"table={t}")

    # 统一查询入口：时间窗 + 来源书两个维度都要能查到（时间轴/地图/图谱共用）
    gq = api.get("/graph/query", year_from=year - 100, year_to=year + 100, limit=50)
    gq_items = gq.json().get("items", []) if gq.status_code == 200 else []
    rec("graph/query/time", gq.status_code == 200 and len(gq_items) > 0,
        f"{year}±100 → {len(gq_items)} 项")
    bq = api.get("/graph/query", book_id=a.book or None, limit=20) if a.book else None
    if bq is not None:
        bq_items = bq.json().get("items", []) if bq.status_code == 200 else []
        rec("graph/query/book", bq.status_code == 200 and len(bq_items) > 0,
            f"book={a.book} → {len(bq_items)} 项")

    whole = api.get("/kg/whole")
    wd = whole.json() if whole.status_code == 200 else {}
    rec("kg/whole", whole.status_code == 200 and len(wd.get("nodes", [])) > 0,
        f"节点{len(wd.get('nodes', []))} 边{len(wd.get('edges', []))}")

    # ---- 6. 地图 ----
    maps = api.get("/maps").json() if api.get("/maps").status_code == 200 else {}
    rec("maps", bool(maps.get("catalog")), f"{len(maps.get('catalog', {}))} 组")
    dm = api.get(f"/maps/{dynasty_id}")
    rec("maps/{dynasty}", dm.status_code == 200 and len(dm.json()) > 0,
        f"{dynasty_id} {len(dm.json()) if dm.status_code == 200 else 0} 幅")

    if a.tiles:
        tile = (dm.json()[0]["tile"] if dm.status_code == 200 and dm.json() else "")
        tr = api.cli.get(f"/api/tiles/{tile}/3/1/1.png")
        if tr.status_code == 200:
            rec("tiles", True, f"{tile} {len(tr.content)}B")
        else:
            warn("tiles", f"{tile} z3/1/1 → HTTP {tr.status_code}（该瓦片可能未缓存）")

    # ---- 7. 本体论 ----
    ont = api.get("/ontology")
    rec("ontology", ont.status_code == 200 and bool(ont.json()),
        f"{len(ont.json()) if ont.status_code == 200 else 0} 个顶层键")

    # ---- 8. 动作日志 / Agent ----
    rec("actions", api.get("/actions").status_code == 200)
    rec("agents", api.get("/agents").status_code == 200)

    # ---- 9. 对话（可选）----
    if a.chat:
        c = api.post("/chat", {"question": term or "这一年发生了什么？",
                               "audience": "scholar"})
        cd = c.json() if c.status_code == 200 else {}
        rec("chat", c.status_code == 200 and bool(cd.get("answer")),
            f"证据{len(cd.get('evidence', []))} 接地{cd.get('grounding', {}).get('score')}")
        if cd.get("warnings"):
            warn("chat/warnings", "；".join(cd["warnings"])[:120])
        if cd.get("evidence") and not cd.get("grounding", {}).get("grounded"):
            warn("chat/grounding", "有年份/专名未在史料原文中出现（G3 告警）")

    if a.json:
        print(json.dumps({"rows": ROWS, "fails": FAILS, "warns": WARNS},
                         ensure_ascii=False, indent=2))
    total = len(ROWS)
    print(f"\n{total - len(FAILS)}/{total} PASS"
          + (f"，{len(FAILS)} FAIL：{','.join(FAILS)}" if FAILS else "")
          + (f"，{len(WARNS)} WARN" if WARNS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
