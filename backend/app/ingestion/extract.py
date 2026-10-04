# -*- coding: utf-8 -*-
"""新书本体抽取器

两条路径：
- 离线/默认：规则启发式（年号纪年→公元、「X，字Y」人物模式、句内共现关系）；
- 在线：LLM 按 JSON schema 抽取，失败自动回退启发式。

输出统一结构（dict）：
    {"persons":[{name,aliases,summary,confidence,dynasty_id}],
     "events":[{name,year,month,day,place,place_id,lon,lat,summary,confidence,dynasty_id}],
     "links":[{src,dst,rel,confidence}],
     "mode":"heuristic|llm"}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import config
from app.ingestion.resolve import normalize_name

_DATA = Path(__file__).resolve().parents[1] / "data"

_SENT_SPLIT = re.compile(r"[。！？；!?;\n]+")
_CJK = r"\u4e00-\u9fa5"

_CN_DIGIT = {"零": 0, "○": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_year(s: str) -> int | None:
    """中文纪年数字 → 整数：元=1，十/二十/卅 等。"""
    if s == "元":
        return 1
    if s in ("廿",):
        return 20
    if s in ("卅",):
        return 30
    if s.isdigit():
        return int(s)
    total, sec = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            sec = _CN_DIGIT[ch]
        elif ch == "十":
            total += (sec or 1) * 10
            sec = 0
        elif ch == "百":
            total += (sec or 1) * 100
            sec = 0
        else:
            return None
    return total + sec


# ---- 年号表：era → (元年公元, dynasty_id) ----
def _load_eras() -> dict[str, tuple[int, str]]:
    p = _DATA / "emperors.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    eras: dict[str, tuple[int, str]] = {}
    for did, emps in raw.items():
        for e in emps:
            era = e.get("era")
            if era and era not in eras:
                eras[era] = (e.get("es") or e.get("s"), did)
    return eras


_ERAS = _load_eras()
_ERA_RE = re.compile(
    "((?:" + "|".join(sorted(_ERAS, key=len, reverse=True)) + r")"
    r"([元○零一二三四五六七八九两十百\d]{1,4})年)")

# 人物：X，字 Y
# 只认「字」：史书里「号/號」绝大多数是封号、称号（"号曰傅说""号为始皇"），
# 用它当人名线索会抽出「傅险姓之」「世贬帝号」这类碎片（已实测）。
_PERSON_RE = re.compile(
    rf"(?P<name>[{_CJK}]{{2,3}})[，,]\s*字(?P<alias>[{_CJK}]{{1,4}})")
# 籍贯：X，XX人（也可与上条并列；轻量）
_NATIVE_RE = re.compile(rf"(?P<name>[{_CJK}]{{2,3}})[，,](?P<home>[{_CJK}]{{2,6}})人")

_ROLE_WORDS = {"皇帝", "天子", "太子", "皇后", "将军", "丞相", "刺史", "国家",
               "天下", "陛下", "大人", "百姓", "诸侯", "郡县", "尚书", "都护"}
_EVENT_VERBS = ("伐", "征", "攻", "破", "杀", "盟", "迁", "即位", "崩", "卒",
                "变法", "改制", "起兵", "称帝", "灭", "降", "朝贡", "遣使")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


# ============================ 启发式 ============================
def _find_era_years(text: str) -> list[tuple[int, str, int, int]]:
    out = []
    for m in _ERA_RE.finditer(text):
        n = _cn_year(m.group(2))
        if n is None:
            continue
        es, did = _ERAS[m.group(1)[:-len(m.group(2)) - 1]]
        year = es + n - 1
        out.append((year, did, m.start(), m.end()))
    return out


def extract_heuristic(text: str, known: dict | None = None) -> dict:
    known = known or {}
    k_people = known.get("people", [])
    k_places = known.get("places", [])

    persons: dict[str, dict] = {}
    events: dict[str, dict] = {}
    links: list[dict] = []

    def add_person(name: str, aliases: list[str] | None = None,
                   summary: str = "", conf: float = 0.5,
                   dynasty_id: str | None = None) -> str:
        key = normalize_name(name)
        if key in persons:
            for a in aliases or []:
                if a not in persons[key]["aliases"]:
                    persons[key]["aliases"].append(a)
            return key
        if name in _ROLE_WORDS or key in _ROLE_WORDS:
            return key
        persons[key] = {"name": name, "aliases": aliases or [],
                        "summary": summary, "confidence": conf,
                        "dynasty_id": dynasty_id}
        return key

    # 人物模式
    for m in _PERSON_RE.finditer(text):
        add_person(m.group("name"), [m.group("alias")],
                   text[max(0, m.start() - 10):m.end() + 20], 0.6)

    # 纪年 → 事件
    sents = _sentences(text)
    offsets: list[tuple[int, str]] = []
    pos = 0
    for s in sents:
        i = text.find(s, pos)
        offsets.append((i, s))
        pos = i + len(s)

    for year, did, ms, me in _find_era_years(text):
        sent = next((s for i, s in offsets if i <= ms < i + len(s) + 2), None)
        if not sent:
            continue
        # 事件名：纪年之后到首个逗号（跳过紧邻的标点），或取含动作词的短句
        tail = sent[sent.find("年") + 1:] if "年" in sent else sent
        tail = tail.lstrip("，,、 ")
        name = re.split(r"[，,、]", tail, 1)[0][:14]
        if not name or not any(v in sent for v in _EVENT_VERBS):
            continue
        key = f"{did}:{year}:{name}"
        if key not in events:
            place, place_id, lon, lat = None, None, None, None
            for pl in k_places:
                if pl["name"] in sent or pl.get("ancient", "") in sent:
                    place, place_id = pl["name"], pl["id"]
                    lon, lat = pl.get("lon"), pl.get("lat")
                    break
            events[key] = {"name": name, "year": year, "month": None,
                           "day": None, "place": place, "place_id": place_id,
                           "lon": lon, "lat": lat,
                           "summary": sent[:140], "confidence": 0.4,
                           "dynasty_id": did}

    # 句内共现：人物 — 参与 → 事件
    for ev in events.values():
        sent = ev["summary"]
        for key, p in persons.items():
            if p["name"] in sent:
                links.append({"src": p["name"], "dst": ev["name"],
                              "rel": "参与", "confidence": 0.45})
                if not p["dynasty_id"]:
                    p["dynasty_id"] = ev["dynasty_id"]
        if ev["place"]:
            links.append({"src": ev["name"], "dst": ev["place"],
                          "rel": "发生于", "confidence": 0.5})

    # 既有实体在文中出现（仅记录，供挂载时对齐）
    for p in k_people:
        if p["name"] in text:
            add_person(p["name"], p.get("aliases", []), "", 0.7)
    return {"persons": list(persons.values()),
            "events": list(events.values()),
            "links": links, "mode": "heuristic"}


# ============================ LLM ============================
_LLM_SCHEMA = """{
  "persons": [{"name":"姓名","aliases":["字/号"],"summary":"一句话","confidence":0.0,"dynasty_id":"可空"}],
  "events":  [{"name":"事件名","year":公元整数或null,"month":null,"day":null,"place":"可空","summary":"一句话","confidence":0.0,"dynasty_id":"可空"}],
  "links":   [{"src":"人物或事件名","dst":"对象名","rel":"参与/发生于/触发/任职于/推行 等","confidence":0.0}]
}"""

# 实测（sensenova 网关）：把长 JSON 结构放在 **system** 里会让请求挂死
# （>180s 超时），因此 system 只留一句硬性约束，schema 与规则放 user。
_LLM_SYS = ("你是中国史领域的本体抽取器。严格只输出 JSON：不要解释，"
            "不要 markdown 代码块，不要省略号，字段用双引号。")

_LLM_RULES = (
    "从下列史籍文本中抽取人物、事件及其关系。\n"
    "要求：1) 纪年换算为公元整数，无法确定用 null；"
    "2) 治世/事变（如文景之治、安史之乱）归入 events；"
    "3) 每条给出 0–1 的 confidence，证据不足宁可不抽；"
    "4) 无内容时各数组留空。\nJSON 结构：\n" + _LLM_SCHEMA + "\n\n史籍文本：\n")


def _iter_objects(s: str):
    """扫描出所有顶层平衡 {...} 片段（忽略字符串内的括号）。"""
    depth, start, in_q, esc = 0, None, False, False
    for i, c in enumerate(s):
        if in_q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_q = False
            continue
        if c == '"':
            in_q = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                yield s[start:i + 1]
                start = None


def _loads_tolerant(raw: str) -> dict:
    """JSON 解析，容忍被 max_tokens 截断的输出。

    截断时整段 json.loads 会失败，但已完成的对象是完整的；
    逐个对象解析可以救回这部分内容，而不是整块退化成启发式抽取。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    persons, events, links = [], [], []
    for frag in _iter_objects(raw):
        try:
            obj = json.loads(frag)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if {"src", "dst", "rel"} <= obj.keys():
            links.append(obj)
        elif "year" in obj:
            events.append(obj)
        elif "name" in obj:
            persons.append(obj)
    return {"persons": persons, "events": events, "links": links,
            "_truncated": True}


def extract_llm(text: str, llm) -> dict:
    # 输入越长输出越长：12000 字原文常产出近万字符 JSON，token 预算吃满会被
    # 截断（finish_reason=length），解析失败后整块退化成启发式抽取。
    # 这里把输入收到 6000 字并给足输出预算，再叠加截断容错兜底。
    raw = llm.chat(_LLM_SYS, _LLM_RULES + text[:config.LLM_EXTRACT_INPUT_CHARS],
                   max_tokens=config.LLM_EXTRACT_MAX_TOKENS)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*|```$", "", raw).strip()
    if not raw.startswith("{"):        # 前面有解释文字时截取 JSON 主体
        i = raw.find("{")
        if i > 0:
            raw = raw[i:]
    data = _loads_tolerant(raw) or {}
    persons, events, links = [], [], []
    # 模型常把可选字段写成 null（"aliases": null / "persons": null），
    # 统一兜底成空值，否则一次迭代崩溃就整块退化成启发式抽取。
    for p in (data.get("persons") or [])[:30]:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        persons.append({"name": str(p["name"])[:20],
                        "aliases": [str(a) for a in (p.get("aliases") or [])][:5],
                        "summary": str(p.get("summary", ""))[:200],
                        "confidence": _clamp(p.get("confidence", 0.6)),
                        "dynasty_id": p.get("dynasty_id") or None})
    for e in (data.get("events") or [])[:30]:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        year = e.get("year")
        events.append({"name": str(e["name"])[:30],
                       "year": int(year) if isinstance(year, (int, float)) else None,
                       "month": e.get("month"), "day": e.get("day"),
                       "place": e.get("place"), "place_id": None,
                       "lon": None, "lat": None,
                       "summary": str(e.get("summary", ""))[:240],
                       "confidence": _clamp(e.get("confidence", 0.6)),
                       "dynasty_id": e.get("dynasty_id") or None})
    for l in (data.get("links") or [])[:40]:
        if not isinstance(l, dict):
            continue
        if l.get("src") and l.get("dst") and l.get("rel"):
            links.append({"src": str(l["src"])[:20], "dst": str(l["dst"])[:20],
                          "rel": str(l["rel"])[:12],
                          "confidence": _clamp(l.get("confidence", 0.6))})
    return {"persons": persons, "events": events, "links": links,
            "mode": "llm"}


def _clamp(x, default: float = 0.6) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(0.05, min(0.98, v))


# ============================ 统一入口 ============================
def extract(text: str, *, llm=None, known: dict | None = None) -> dict:
    if llm is not None and getattr(llm, "name", "stub") != "stub":
        try:
            return extract_llm(text, llm)
        except Exception as e:  # 诊断期：把真实的回退原因打出来
            import sys as _s
            print(f"[extract] LLM 失败回退启发式：{type(e).__name__}: "
                  f"{str(e)[:200]}", file=_s.stderr, flush=True)
            out = extract_heuristic(text, known)
            out["mode"] = "heuristic"
            return out
    return extract_heuristic(text, known)
