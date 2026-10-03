# -*- coding: utf-8 -*-
"""组装 LangGraph 多 Agent 问答图：
对话路由 → 实体解析 → 史料召回(RAG) → 考据 → 叙事 → 校验(独立质检Agent) → 审阅
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from app.store import Store
from app.llm import get_llm
from .state import ChatState
from .nodes import make_nodes


def build_chat_graph(store: Store):
    nodes = make_nodes(store, get_llm())
    g = StateGraph(ChatState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.add_edge(START, "route")
    g.add_edge("route", "resolve")
    g.add_edge("resolve", "retrieve")
    g.add_edge("retrieve", "verify")
    g.add_edge("verify", "narrate")
    g.add_edge("narrate", "validate")
    g.add_edge("validate", "review")
    g.add_edge("review", END)
    return g.compile()
