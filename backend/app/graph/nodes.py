# -*- coding: utf-8 -*-
"""问答图各节点（对应方案中的 Agent 分工）"""
from __future__ import annotations

from app.store import Store
from app.llm.base import StubLLM, get_llm
from app.llm.local_embed import get_embedder
from app.schema.ontology import Audience, Source
from app.ingestion.rag import retrieve_chunks


def make_nodes(store: Store, llm):
    def route_node(state):
        """对话路由 Agent：规范化问题；叙述统一采用学术模式（不再区分受众角色）"""
        q = (state.get("question") or "").strip()
        return {"question": q, "audience": Audience.scholar.value}

    def resolve_node(state):
        """时空/实体解析 Agent：把问题归一到本体实体（KG 召回）。
        匹配规则：实体名（含拆分形式，如「秦始皇 嬴政」→「秦始皇」「嬴政」）
        出现在问题中即命中，取名称最短的高显著实体。"""
        q = state["question"]

        def aliases(obj) -> list[str]:
            parts = [obj.name]
            parts.extend(obj.name.split(" "))
            return [p for p in parts if len(p) >= 2]

        candidates: list[tuple[str, object, int]] = []
        for kind, table in (("person", store.people),
                            ("event", store.events),
                            ("place", store.places)):
            for obj in table:
                if any(a in q for a in aliases(obj)):
                    candidates.append((kind, obj, min(len(a) for a in aliases(obj) if a in q)))
        if candidates:
            # 名称匹配越短越核心（秦始皇 > 秦始皇本纪…），优先人物/事件
            candidates.sort(key=lambda c: c[2])
            kind, obj, _ = candidates[0]
            return {"entity_kind": kind, "entity_id": obj.id, "entity": obj}
        return {"entity_kind": None, "entity_id": None, "entity": None}

    def retrieve_node(state):
        """RAG 召回 Agent：取该实体的全部史料证据（生产环境为向量+BM25 混合检索）；
        同时在全部上传书籍切块中检索相关段落，即使问题未命中主干实体也能据书作答。"""
        q = state["question"]
        entity = state.get("entity")
        evidence = []
        if entity is not None:
            evidence = store.sources_of(state["entity_kind"], entity)

        # 切块 RAG（在线走 embedding，离线/异常自动回退 BM25）
        chunk_evidence: list[Source] = []
        if store.chunks:
            docs = [(bid, ch["idx"], ch["text"]) for bid, ch in store.all_chunks]
            emb_index = {bid: {ch["idx"]: ch.get("embedding")
                               for ch in store.chunks_of(bid)}
                         for bid in store.chunks}
            book_of = {b.id: b for b in store.books}
            has_vectors = any(
                ch is not None
                for m in emb_index.values() for ch in m.values())
            try:
                hits, _mode = retrieve_chunks(
                    q, docs,
                    embed_fn=get_embedder() if has_vectors else None,
                    get_embedding=lambda bid, idx: emb_index.get(bid, {}).get(idx))
            except Exception:
                hits = []
            if hits:
                top = hits[0]["score"] or 1.0
                for h in hits:
                    b = book_of.get(h["book_id"])
                    if b is None:
                        continue
                    conf = 0.45 + 0.4 * (h["score"] / top)
                    chunk_evidence.append(Source(
                        level=b.level,
                        work=f"《{b.title}》·切块{h['idx'] + 1}",
                        text=h["text"],
                        confidence=min(0.9, conf),
                        from_book=b.id))
        evidence = evidence + chunk_evidence
        # 去重（按 work+text）
        seen, uniq = set(), []
        for s in evidence:
            key = (s.work, s.text)
            if key not in seen:
                seen.add(key)
                uniq.append(s)
        uniq.sort(key=lambda s: s.confidence, reverse=True)
        return {"evidence": uniq}

    def verify_node(state):
        """考据 Agent：置信度校验，标注低置信与无证据情况"""
        evidence = state.get("evidence", [])
        warnings = []
        lowest = 1.0
        for s in evidence:
            lowest = min(lowest, s.confidence)
            if s.level == 3:
                warnings.append(f"《{s.work}》属三级文学/大众材料，不能单独作为史实依据")
            if s.confidence < 0.4:
                warnings.append(f"《{s.work}》置信度偏低（{s.confidence:.0%}），建议核对上游史料")
        if not evidence:
            warnings.append("未检索到可溯源史料，不予作答以避免幻觉")
        return {"lowest_confidence": lowest if evidence else 0.0, "warnings": warnings}

    def narrate_node(state):
        """叙事 Agent：按受众组织回答（强制基于证据）"""
        try:
            draft = llm.narrate(
                question=state["question"],
                entity=state.get("entity"),
                audience=state["audience"],
                evidence=state.get("evidence", []),
            )
        except Exception:
            # 在线叙事失败：降级为证据拼接，不阻断整条问答流水线
            ent = state.get("entity") or {}
            head = f"关于{ent.get('name', '所问')}：" if ent else ""
            ev = state.get("evidence", [])
            body = "\n".join(f"[{i+1}] {s.work}：{s.text[:160]}"
                             for i, s in enumerate(ev)) or "暂未检索到可引用史料。"
            draft = head + "\n" + body
        return {"draft": draft}

    def validate_node(state):
        """校验 Agent（独立质检角色）：对叙事草稿做事实一致性检查。
        - 有证据时，草稿必须带引用编号 [n]，否则视为未遵守 RAG 约束；
        - 草稿不得为空；
        - 三级史料证据必须在告警中显式标注。
        问题写入 check_issues，由审阅 Agent 统一裁决。"""
        import re
        issues: list[str] = []
        draft = state.get("draft") or ""
        evidence = state.get("evidence", [])
        if evidence:
            if not draft.strip():
                issues.append("叙事草稿为空，疑似生成失败")
            elif not re.search(r"\[\d+\]", draft):
                issues.append("叙事缺少引用编号，已要求叙事 Agent 基于证据作答")
            if any(s.level == 3 for s in evidence) and not any(
                    "三级" in w or "文学" in w for w in state.get("warnings", [])):
                issues.append("含三级文学材料但未在考据提示中标注")
        return {"check_issues": issues}

    def review_node(state):
        """审阅 Agent：无证据则拦截；合并校验 Agent 的质检结果；附引用与告警"""
        if not state.get("evidence"):
            return {"answer": "抱歉，未在当前知识库中找到可溯源的史料依据，为避免编造，暂不回答该问题。"}
        answer = state["draft"]
        cites = "\n".join(
            f"[{i+1}] {s.work}（{'一二三'[s.level-1]}级史料 · 置信度 {s.confidence:.0%}）"
            for i, s in enumerate(state["evidence"])
        )
        answer = answer + "\n\n— 引用 —\n" + cites
        if state.get("warnings"):
            answer += "\n\n— 考据提示 —\n" + "\n".join("· " + w for w in state["warnings"])
        if state.get("check_issues"):
            answer += "\n\n— 校验 Agent 提示 —\n" + "\n".join("· " + w for w in state["check_issues"])
        return {"answer": answer}

    return {
        "route": route_node,
        "resolve": resolve_node,
        "retrieve": retrieve_node,
        "verify": verify_node,
        "narrate": narrate_node,
        "validate": validate_node,
        "review": review_node,
    }
