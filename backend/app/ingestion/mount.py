# -*- coding: utf-8 -*-
"""新书挂载流水线（LangGraph + human-in-the-loop）

    prepare（解析→抽取→归一→对齐→评估，产物为提案）
        ↓
    await_approval ── interrupt：图在此暂停，等待前端人工勾选
        ↓ resume(accepted: list[bool])
    commit（写入本体：Store 的 Action Type 写操作）

使用 MemorySaver 持久化中断状态（生产换 PostgresSaver）。
"""
from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from app.store import store


class MountState(TypedDict, total=False):
    book_id: str
    stage: str
    proposals: list[dict]
    accepted: list[bool]
    result: dict


PIPELINE_STAGES = [
    ("parse", "文档解析（PDF/EPUB/OCR 切块）"),
    ("extract", "Agent 抽取（实体 · 关系 · 观点）"),
    ("normalize", "时空归一化（年号→公元 · 地名→坐标）"),
    ("align", "实体对齐（与主干消歧 ER）"),
    ("score", "置信度评估（生成挂载提案）"),
]


def _prepare(state: MountState):
    """真实系统中此节点串联五个 Agent；原型直接读取预先结构化的书源，
    但保留 stage 轨迹，供前端展示流水线进展。"""
    book = next(b for b in store.books if b.id == state["book_id"])
    proposals = [
        {
            "kind": x.kind,
            "name": x.name if x.kind != "link" else x.name,
            "summary": x.summary,
            "confidence": x.confidence,
            "new_id": x.new_id,
            "target_id": x.target_id,
            "rel": x.rel,
            "default_accepted": x.confidence >= 0.4,
        }
        for x in book.extracts
    ]
    return {"stage": "awaiting_approval", "proposals": proposals}


def _await_approval(state: MountState):
    # interrupt：图暂停并把提案抛给调用方；前端确认后以 Command(resume=...) 恢复
    accepted = interrupt({"stage": "await_approval", "proposals": state["proposals"]})
    return {"accepted": accepted}


def _commit(state: MountState):
    book = next(b for b in store.books if b.id == state["book_id"])
    result = store.commit_book(book, state["accepted"])
    return {"stage": "committed", "result": result}


def build_mount_graph():
    g = StateGraph(MountState)
    g.add_node("prepare", _prepare)
    g.add_node("await_approval", _await_approval)
    g.add_node("commit", _commit)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "await_approval")
    g.add_edge("await_approval", "commit")
    g.add_edge("commit", END)
    return g.compile(checkpointer=MemorySaver())


mount_graph = build_mount_graph()
