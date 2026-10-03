# -*- coding: utf-8 -*-
"""问答 Agent 图的共享状态"""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class ChatState(TypedDict, total=False):
    question: str
    audience: str
    # resolve 节点产物
    entity_kind: Optional[str]
    entity_id: Optional[str]
    entity: Any
    # retrieve 节点产物（RAG + KG）
    evidence: list   # list[Source]
    # verify 节点产物
    lowest_confidence: float
    warnings: list[str]
    # narrate / validate / review 产物
    draft: str
    check_issues: list[str]   # 校验 Agent 质检结果
    answer: str
