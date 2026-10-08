# -*- coding: utf-8 -*-
"""Agent 的确定性查询工具（Phase 6）。

与 kg_browse / rag_search 这类"通用检索"不同，这五个工具各自回答一类
**结构化问题**，返回的是可直接喂给 LLM 的证据包（含出处），不是原文片段：

  time_query      某年 / 某区间 / 某朝代发生了什么
  geo_query       某地 / 某坐标附近有什么
  book_query      某书对某事/某人是怎么记的
  stance_compare  关于同一对象，各方分别持什么立场（Phase 7 的 Claim）
  conflict_detect 关于同一对象，是否存在互斥立场（分歧并列，不做裁决）

设计取舍：**不做 LLM function calling**。
网关（token.sensenova.cn）对 tool_calls 的支持不稳定，而这几个查询本身是
确定的——先用规则路由选工具、执行、把结果塞进上下文。等确定网关支持
function calling，再在 run_agent 里把 select_tools() 换成模型返回的 tool_calls，
工具函数本身不用改。
"""

from __future__ import annotations

import re
from typing import Any

# store 是模块级单例，import 即触发全量加载；工具模块被 runtime 引用，
# 而 runtime 又可能被 store 侧间接引用，所以一律在函数内延迟 import 避免环。
def _store():
    from app.store import store
    return store


def _ev_dict(e) -> dict:
    return {
        "id": e.id, "name": e.name, "year": e.year,
        "month": e.month, "day": e.day,
        "dynasty_id": e.dynasty_id, "place": e.place,
        "lon": e.lon, "lat": e.lat, "from_book": e.from_book,
        "kind": getattr(e, "kind", None),
    }


# ---------------------------------------------------------------- time_query
def time_query(year: int | None = None, frm: int | None = None,
               to: int | None = None, dynasty_id: str | None = None,
               limit: int = 20) -> dict:
    s = _store()
    evs = s.events
    if dynasty_id:
        evs = [e for e in evs if e.dynasty_id == dynasty_id]
    if year is not None:
        evs = [e for e in evs if e.year == year]
    else:
        if frm is not None:
            evs = [e for e in evs if e.year is not None and e.year >= frm]
        if to is not None:
            evs = [e for e in evs if e.year is not None and e.year <= to]
    # 年份未知的（None）永远不进结果：它们落不到时间轴上
    evs = [e for e in evs if e.year is not None]
    evs.sort(key=lambda e: (e.year, e.month or 0, e.day or 0))
    items = [_ev_dict(e) for e in evs[:limit]]
    span = (f"{year} 年" if year is not None
            else f"{frm if frm is not None else '?'}—{to if to is not None else '?'}")
    return {
        "tool": "time_query", "scope": span, "total": len(evs),
        "items": items,
        "summary": f"{span}命中 {len(evs)} 条事件"
                   + (f"，取前 {len(items)} 条" if len(evs) > len(items) else ""),
    }


# ----------------------------------------------------------------- geo_query
def geo_query(place: str | None = None, lon: float | None = None,
              lat: float | None = None, radius_km: float = 200.0,
              limit: int = 20) -> dict:
    s = _store()
    items: list[dict] = []
    if place:
        for p in s.places:
            if place in (p.name or "") or (p.ancient and place in p.ancient):
                items.append({"type": "place", "id": p.id, "name": p.name,
                              "lon": p.lon, "lat": p.lat,
                              "modern": p.modern})
    if lon is not None and lat is not None:
        for e in s.events:
            if e.lon is None or e.lat is None:
                continue
            # 粗略球面距离（km）：中国大陆纬度跨度下 1°≈111km
            d = ((e.lon - lon) * 111.0 * abs(__import__("math").cos(lat / 57.3))) ** 2
            d += ((e.lat - lat) * 111.0) ** 2
            if d <= radius_km ** 2:
                it = _ev_dict(e)
                it["type"] = "event"
                items.append(it)
    if place and not (lon is not None and lat is not None):
        for e in s.events:
            if e.place and place in e.place:
                it = _ev_dict(e)
                it["type"] = "event"
                items.append(it)
    return {
        "tool": "geo_query", "scope": place or f"{lon},{lat}±{radius_km}km",
        "total": len(items), "items": items[:limit],
        "summary": f"「{place or '坐标'}」命中 {len(items)} 条",
    }


# ---------------------------------------------------------------- book_query
def book_query(book_id: str | None = None, keyword: str | None = None,
               limit: int = 20) -> dict:
    s = _store()
    evs = [e for e in s.events if e.from_book]
    if book_id:
        evs = [e for e in evs if e.from_book == book_id
               or book_id in (e.from_book or "")]
    if keyword:
        evs = [e for e in evs if keyword in (e.name or "")
               or keyword in (e.place or "")]
    items = [_ev_dict(e) for e in evs[:limit]]
    books = sorted({e.from_book for e in evs if e.from_book})
    return {
        "tool": "book_query", "scope": book_id or keyword or "(全部书)",
        "total": len(evs), "items": items, "books": books[:20],
        "summary": (f"命中的书：{', '.join(books[:8])}" if books else "无命中")
                   + f"；事件 {len(evs)} 条",
    }


# ------------------------------------------------------------ stance_compare
def stance_compare(about_id: str | None = None, keyword: str | None = None,
                   limit: int = 20) -> dict:
    """各方立场并列（Phase 7 Claim）。同一对象的主张按 stance 分组返回。"""
    s = _store()
    cs = list(getattr(s, "claims", []))
    if about_id:
        cs = [c for c in cs if c.about_id == about_id]
    elif keyword:
        cs = [c for c in cs if keyword in (c.name or "")
              or keyword in (c.about_id or "")]
    groups: dict[str, list[dict]] = {}
    for c in cs:
        groups.setdefault(c.stance, []).append({
            "id": c.id, "name": c.name, "claimant_id": c.claimant_id,
            "claimant_kind": c.claimant_kind, "about_id": c.about_id,
            "confidence": c.confidence, "from_book": c.from_book,
            "kind": c.kind,
        })
    return {
        "tool": "stance_compare", "scope": about_id or keyword or "(全部)",
        "total": len(cs), "groups": {k: v[:limit] for k, v in groups.items()},
        "summary": "；".join(f"{k} {len(v)} 条" for k, v in groups.items()),
    }


# ----------------------------------------------------------- conflict_detect
_MUTEX = (("肯定", "否定"), ("支持", "反对"))


def conflict_detect(about_id: str | None = None, limit: int = 20) -> dict:
    """分歧并列：同一对象上出现互斥立场。只摆出来，不裁决谁对。"""
    s = _store()
    by: dict[str, list] = {}
    for c in getattr(s, "claims", []):
        by.setdefault(c.about_id, []).append(c)
    out: list[dict] = []
    for aid, cs in by.items():
        if about_id and aid != about_id:
            continue
        stances = {c.stance for c in cs}
        hit = [(a, b) for a, b in _MUTEX if a in stances and b in stances]
        if hit:
            out.append({
                "about_id": aid, "stances": sorted(stances),
                "mutex": hit,
                "claims": [{"id": c.id, "name": c.name, "stance": c.stance,
                            "claimant_id": c.claimant_id,
                            "confidence": c.confidence} for c in cs],
            })
    out.sort(key=lambda x: -len(x["claims"]))
    return {
        "tool": "conflict_detect", "scope": about_id or "(全部)",
        "total": len(out), "items": out[:limit],
        "summary": f"发现 {len(out)} 组互斥立场",
    }


# ------------------------------------------------------------------ 路由/分发
_TOOLS: dict[str, Any] = {
    "time_query": time_query,
    "geo_query": geo_query,
    "book_query": book_query,
    "stance_compare": stance_compare,
    "conflict_detect": conflict_detect,
}


def call_tool(name: str, **kw) -> dict:
    fn = _TOOLS.get(name)
    if fn is None:
        return {"tool": name, "error": f"未知工具 {name}"}
    try:
        return fn(**kw)
    except Exception as e:  # noqa: BLE001
        return {"tool": name, "error": str(e)}


_YEAR_IN_Q = re.compile(r"\d{1,4}\s*年")
_RANGE_IN_Q = re.compile(r"\d{1,4}\s*(?:到|至|—|-)\s*\d{1,4}")


def _has_place_name(q: str) -> bool:
    """问题里出现了任何一个已知地名（比堆关键词准：长安/凉州/赤壁 都算）。"""
    if not q:
        return False
    for p in _store().places:
        nm = (p.name or "").strip()
        if len(nm) >= 2 and nm in q:
            return True
    return False


def select_tools(q: str) -> list[str]:
    """按问题里的信号选工具（规则路由，见模块文档）。

    返回按顺序尝试的工具名；调用方（run_agent）依次执行，把非空结果并入上下文。
    """
    s = q or ""
    picked: list[str] = []
    # 时间：问题里出现「755 年」「1911 至 1912」这类显式年份，或时间问法
    if (_YEAR_IN_Q.search(s) or _RANGE_IN_Q.search(s)
            or any(k in s for k in ("哪一年", "几年", "年间", "当时", "同期",
                                    "同时", "元年", "世纪", "什么年代"))):
        picked.append("time_query")
    if (any(k in s for k in ("哪里", "何地", "在哪", "附近", "地方", "地区",
                             "位置", "地图", "周边"))
            or _has_place_name(s)):
        picked.append("geo_query")
    if any(k in s for k in ("记载", "书上", "某某书", "史书", "据", "出自")):
        picked.append("book_query")
    if any(k in s for k in ("看法", "评价", "立场", "观点", "怎么看待",
                            "认为", "主张")):
        picked.append("stance_compare")
    if any(k in s for k in ("分歧", "争议", "矛盾", "冲突", "异说", "谁对",
                            "不同说法")):
        picked.append("conflict_detect")
    return picked


# ------------------------------------------------------------ 参数提取 / 执行
_YEAR_RE = re.compile(r"(-?\d{1,4})\s*年")
_RANGE_RE = re.compile(r"(\d{1,4})\s*(?:到|至|-|—)\s*(\d{1,4})")


def _args_for(name: str, q: str) -> dict:
    """从自然语言问题里抠出工具参数。抠不出就给空 dict——工具会按全量跑，
    再由调用方按 total 决定是否采用，抠错参数比不抠更危险。"""
    s = _store()
    if name == "time_query":
        m = _RANGE_RE.search(q)
        if m:
            return {"frm": int(m.group(1)), "to": int(m.group(2))}
        m = _YEAR_RE.search(q)
        if m:
            return {"year": int(m.group(1))}
        return {}
    if name == "geo_query":
        for p in s.places:
            nm = (p.name or "").strip()
            if len(nm) >= 2 and nm in q:
                return {"place": nm}
        return {}
    if name == "book_query":
        for b in s.books:
            t = (b.title or "").strip()
            if len(t) >= 2 and t in q:
                return {"book_id": b.id}
        return {}
    if name == "stance_compare":
        return {"keyword": q[:10]}
    return {}


def run_tools(q: str, limit: int = 3) -> list[dict]:
    """选出并执行工具，只返回**有结果**的（空结果不占上下文）。"""
    out: list[dict] = []
    for name in select_tools(q):
        r = call_tool(name, **_args_for(name, q))
        if r.get("error") or not r.get("total"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def format_tool_results(results: list[dict], per_tool: int = 6) -> list[str]:
    """把工具结果压成给 LLM 看的几行证据（含出处，不含原始长文）。"""
    lines: list[str] = []
    for r in results:
        lines.append(f"[{r['tool']}] {r.get('summary', '')}"
                     f"（范围：{r.get('scope', '')}）")
        for it in (r.get("items") or [])[:per_tool]:
            nm = it.get("name")
            if not nm:
                continue
            y = it.get("year")
            tag = f"（{y} 年）" if y else ""
            src = it.get("from_book")
            src = f"『{src}』" if src else ""
            lines.append(f"   - {nm}{tag}{src}")
        for st, cs in (r.get("groups") or {}).items():
            for c in cs[:4]:
                lines.append(f"   - [{st}] {c.get('name', '')}")
    return lines
