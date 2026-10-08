# -*- coding: utf-8 -*-
"""Agent 注册表

- 内置：领航员（系统操纵 Agent）、太史（历史 Agent）；
- 自定义：名称 + 系统提示词 + 工具勾选，持久化到 app/data/agents.json；
- 工具目录即注册表里的 TOOLS（前端勾选；读工具自由调用，写工具只能生成确认卡）。
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from app import config

_DATA = Path(config.DATA_DIR)
_AGENTS_FILE = _DATA / "agents.json"

# ---- 工具目录 ----
TOOLS: dict[str, dict] = {
    "kg_browse": {"label": "图谱浏览", "write": False,
                  "desc": "检索朝代/事件/人物/地点/机构/文物/概念"},
    "kg_subgraph": {"label": "邻域展开", "write": False,
                    "desc": "查看某节点的一阶关系"},
    "rag_search": {"label": "RAG 检索", "write": False,
                   "desc": "在全部书籍切块中 BM25/向量检索"},
    "source_quote": {"label": "史料引用", "write": False,
                     "desc": "调取并引用实体关联史料"},
    # Phase 6：确定性结构化查询工具（实现见 app/agents/tools.py）
    "time_query": {"label": "时间查询", "write": False,
                   "desc": "某年/某区间/某朝代发生的事件"},
    "geo_query": {"label": "空间查询", "write": False,
                  "desc": "某地名或坐标附近的地点与事件"},
    "book_query": {"label": "书证查询", "write": False,
                   "desc": "某书对某事/某人的记载"},
    "stance_compare": {"label": "立场比较", "write": False,
                       "desc": "关于同一对象的各方立场并列"},
    "conflict_detect": {"label": "分歧检测", "write": False,
                        "desc": "找出互斥立场（不裁决谁对）"},
    "book_mount": {"label": "挂载/卸载", "write": True,
                   "desc": "改变书籍挂载状态（需确认卡）"},
    "book_delete": {"label": "删除书籍", "write": True,
                    "desc": "删除上传书籍到回收区（需确认卡）"},
    "graph_annotate": {"label": "图谱标注", "write": True,
                       "desc": "为实体/关系添加注记（需确认卡）"},
}

# ---- 内置 Agent ----
_BUILTIN = [
    {
        "id": "navigator",
        "name": "领航员",
        "builtin": True,
        "color": "#1d4ed8",
        "system_prompt": (
            "你是「领航员」，本中国史知识系统的操纵 Agent。职责：\n"
            "1) 理解用户意图，必要时先调用图谱浏览与 RAG 检索澄清；\n"
            "2) 调度历史 Agent 与本体工具，组织回答；\n"
            "3) 任何写操作（挂载、卸载、删除、标注）都不得自行执行，"
            "必须生成确认卡交用户批准后才可落地。\n"
            "风格：简洁、直接，先答问题，再给依据。"),
        "tools": list(TOOLS),
    },
    {
        "id": "taishi",
        "name": "太史",
        "builtin": True,
        "color": "#b45309",
        "system_prompt": (
            "你是「太史」，历史考据与叙事 Agent。纪律：\n"
            "1) 言必有据：引用史料注明出处（书名/篇目/切块），区分原始史料、"
            "现代研究与文学作品的层级；\n"
            "2) 纪年存疑、异说分歧必须标注，不臆断；\n"
            "3) 可按受众切换叙事深浅，但史实部分不得为讲故事而虚构；\n"
            "4) 不执行写操作，只回答与考据。"),
        "tools": ["kg_browse", "kg_subgraph", "rag_search", "source_quote"],
    },
]


class AgentRegistry:
    def __init__(self) -> None:
        _DATA.mkdir(parents=True, exist_ok=True)
        self.custom: list[dict] = []
        self._lock = threading.RLock()
        if _AGENTS_FILE.exists():
            try:
                self.custom = json.loads(
                    _AGENTS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.custom = []

    def _persist(self) -> None:
        _AGENTS_FILE.write_text(
            json.dumps(self.custom, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def list(self) -> list[dict]:
        return _BUILTIN + self.custom

    def get(self, agent_id: str) -> dict | None:
        return next((a for a in self.list() if a["id"] == agent_id), None)

    def create(self, name: str, system_prompt: str,
               tools: list[str]) -> dict:
        with self._lock:
            name = name.strip()
            if not name:
                raise ValueError("Agent 名称不能为空")
            if any(a["name"] == name for a in self.list()):
                raise ValueError("已存在同名 Agent")
            valid = [t for t in tools if t in TOOLS]
            agent = {"id": "agent_" + uuid.uuid4().hex[:10],
                     "name": name[:20], "builtin": False,
                     "color": "#0d9488",
                     "system_prompt": system_prompt[:4000],
                     "tools": valid}
            self.custom.append(agent)
            self._persist()
            return agent

    def update(self, agent_id: str, *, name: str | None = None,
               system_prompt: str | None = None,
               tools: list[str] | None = None) -> dict:
        with self._lock:
            agent = next((a for a in self.custom if a["id"] == agent_id), None)
            if agent is None:
                raise KeyError("自定义 Agent 不存在（内置 Agent 不可编辑）")
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("名称不能为空")
                agent["name"] = name[:20]
            if system_prompt is not None:
                agent["system_prompt"] = system_prompt[:4000]
            if tools is not None:
                agent["tools"] = [t for t in tools if t in TOOLS]
            self._persist()
            return agent

    def delete(self, agent_id: str) -> None:
        with self._lock:
            before = len(self.custom)
            self.custom = [a for a in self.custom if a["id"] != agent_id]
            if len(self.custom) == before:
                raise KeyError("自定义 Agent 不存在（内置 Agent 不可删除）")
            self._persist()
