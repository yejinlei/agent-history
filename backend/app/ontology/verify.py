# -*- coding: utf-8 -*-
"""反幻觉校验（Grounding Verification）——本体技术栈的最终用途

引入本体不是为了把图"画好看"，而是给 Agent 的推理加上**可判定的约束**：
一句话能不能说，不取决于模型自不自信，而取决于它能不能落到
「本体允许的类型组合 + 史料原文片段 + 外部锚点」上。

五道闸门（G1/G2 在既有代码里，G3/G4 在本模块，G5 依赖 external_ids）：

    G1 模式闸门  只有本体声明的 Object Type 与 REL_PAIRS 端点组合可写
                 （store.add_edge 已强制，越界直接 ValueError）
    G2 证据闸门  每个 Source/Claim 必带 from_book + 原文片段；无证据不入库
    G3 接地闸门  回答生成后校验：年份与专名必须在证据原文里出现
                 （grounding_report），未接地的部分标红而不是静默放行
    G4 分歧闸门  同一 about 上多书说法不一致 → detect_disputes 原样并列，
                 不做"二选一"，避免用一只史源的口吻抹掉另一只
    G5 锚点闸门  external_ids 对齐 Wikidata/CBDB/CHGIS 等外部事实，
                 与抽取结果冲突时标记 needs_review（人工核）
"""
from __future__ import annotations

import re
from typing import Iterable

_CJK = r"\u4e00-\u9fa5"
_YEAR_RE = re.compile(r"(公元前\s*)?(-?\d{1,4})\s*年")

# 允许的外部标识命名空间（external_ids 的键白名单）
EXTERNAL_NAMESPACES = ("wikidata", "cbdb", "chgis", "tgn", "geonames",
                       "periodo", "viaf", "bibframe", "wikisource")


def parse_years(text: str) -> list[int]:
    """从文本抽取年份（支持「公元前221年」「221年」）。"""
    out: list[int] = []
    for m in _YEAR_RE.finditer(text or ""):
        try:
            v = int(m.group(2))
        except ValueError:
            continue
        out.append(-abs(v) if m.group(1) else v)
    return out


def grounding_report(answer: str, evidence_texts: Iterable[str],
                     known_names: Iterable[str] = (),
                     known_years: Iterable[int] = (),
                     tolerance: int = 2) -> dict:
    """G3：判断回答里的年份与专名是否被证据原文支持。

    known_names 传图谱中已知实体名（人物/事件/地点）；只有答案里
    真正出现的名字才参与校验，避免把无关实体算进分母。
    known_years 传结构化年份（实体卡自带的 year/生卒年）——来自本体
    的数值不是幻觉，不该被误判。
    返回 score=1 表示全部接地；unsupported_* 供前端标红。
    """
    ev = "\n".join(t for t in (evidence_texts or []) if t)
    ev_years = set(parse_years(ev)) | {int(y) for y in (known_years or ())
                                       if isinstance(y, (int, float))}

    ans_years = parse_years(answer or "")
    unsupported_years = [
        y for y in ans_years
        if not any(abs(y - e) <= tolerance for e in ev_years)]

    names = {n for n in (known_names or ()) if n and len(n) >= 2}
    used = [n for n in names if n in (answer or "")]
    unsupported_terms = [n for n in used if n not in ev]

    total = len(ans_years) + len(used)
    bad = len(unsupported_years) + len(unsupported_terms)
    return {
        "score": 1.0 if total == 0 else round(1 - bad / total, 3),
        "grounded": bad == 0,
        "checked": total,
        "unsupported_years": unsupported_years,
        "unsupported_terms": unsupported_terms[:20],
        "has_evidence": bool(ev),
    }


def verify_claim(claim: str, quote: str, threshold: float = 0.34) -> dict:
    """抽取阶段自检：一条 claim 是否被原文片段支持（字面包覆 + 年份一致）。

    低于阈值不自动丢弃，而是降低 confidence 并标记，交给人工/二次复核，
    避免"过滤太狠"把正确但表述不同的说法也删掉。
    """
    claim = claim or ""
    quote = quote or ""
    if not claim:
        return {"supported": False, "coverage": 0.0, "reason": "空断言"}
    grams = {claim[i:i + 2] for i in range(len(claim) - 1)}
    hit = sum(1 for g in grams if g in quote)
    coverage = round(hit / len(grams), 3) if grams else 0.0
    years_ok = True
    cy, qy = set(parse_years(claim)), set(parse_years(quote))
    if cy and qy:
        years_ok = any(abs(a - b) <= 2 for a in cy for b in qy)
    supported = coverage >= threshold and (not cy or not qy or years_ok)
    return {"supported": supported, "coverage": coverage,
            "years_ok": years_ok, "reason": "" if supported else "原文支持不足"}


def detect_disputes(claims: list[dict]) -> list[dict]:
    """G4：同一 about 上不同书/不同立场的说法并列成「分歧」，不做裁决。

    claim 结构：{about, year, stance, from_book, text, confidence}
    """
    by_key: dict[str, list[dict]] = {}
    for c in claims or []:
        key = c.get("about")
        if not key:
            continue
        by_key.setdefault(key, []).append(c)
    out: list[dict] = []
    for about, items in by_key.items():
        if len(items) < 2:
            continue
        years = {c.get("year") for c in items if c.get("year") is not None}
        stances = {c.get("stance") for c in items if c.get("stance")}
        if len(years) > 1 or len(stances) > 1:
            out.append({
                "about": about,
                "books": sorted({c.get("from_book") for c in items
                                 if c.get("from_book")}),
                "years": sorted(y for y in years if y is not None),
                "stances": sorted(s for s in stances if s),
                "claims": items,
            })
    return out


def normalize_external_ids(ids: dict | None) -> dict:
    """external_ids 白名单过滤（G5 的输入规范化）。"""
    if not ids:
        return {}
    return {k: str(v).strip() for k, v in ids.items()
            if k in EXTERNAL_NAMESPACES and str(v).strip()}
