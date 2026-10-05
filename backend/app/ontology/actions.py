# -*- coding: utf-8 -*-
"""Action Types 层（对标 Palantir Foundry ontology action modeling）

职责：
1. ACTION_DEFS —— 规范动作定义：前置条件、逆动作；
2. ActionLog  —— 每次写操作落盘 app/data/action_log.json，
   支持单步 Undo（执行逆动作处理器），undone 标记防止重复撤销。
不做快照分支（id 即 RID，版本迭代由对象 version 字段承担）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app import config

_DATA = Path(config.DATA_DIR)
_LOG_FILE = _DATA / "action_log.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActionDef:
    def __init__(self, name: str, label: str, precondition: str,
                 inverse: str | None = None, desc: str = "") -> None:
        self.name = name
        self.label = label
        self.precondition = precondition
        self.inverse = inverse
        self.desc = desc

    def dump(self) -> dict:
        return {"name": self.name, "label": self.label,
                "precondition": self.precondition,
                "inverse": self.inverse, "desc": self.desc}


ACTION_DEFS: dict[str, ActionDef] = {
    "book.upload": ActionDef(
        "book.upload", "上传新书",
        "文件 ≤50MB；格式为 PDF/TXT/EPUB/MD；同书名需确认",
        inverse="book.delete"),
    "book.mount": ActionDef(
        "book.mount", "挂载书籍",
        "书籍存在且未挂载；提炼提案已逐项确认",
        inverse="book.unmount"),
    "book.unmount": ActionDef(
        "book.unmount", "卸载书籍",
        "书籍当前为已挂载状态",
        inverse="book.mount"),
    "book.delete": ActionDef(
        "book.delete", "删除书籍",
        "仅 origin=upload 的书籍可删；删除前先卸载",
        inverse="book.restore"),
    "book.restore": ActionDef(
        "book.restore", "恢复书籍",
        "文件仍在 library/trash 回收区内"),
    "action.undo": ActionDef(
        "action.undo", "撤销动作",
        "目标动作存在、未撤销，且为最近一条可撤销记录"),
}


class ActionLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._handlers: dict[str, Callable[[dict], dict | None]] = {}
        self._lock = threading.RLock()
        _DATA.mkdir(parents=True, exist_ok=True)
        if _LOG_FILE.exists():
            try:
                self.entries = json.loads(
                    _LOG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.entries = []

    def _persist(self) -> None:
        _LOG_FILE.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def set_handler(self, action: str, fn: Callable[[dict], dict | None]) -> None:
        """注册某动作的逆动作执行器（由业务层装配，避免反向依赖）。"""
        self._handlers[action] = fn

    def log(self, action: str, params: dict | None = None,
            result: dict | None = None, actor: str = "user") -> dict:
        with self._lock:
            if action not in ACTION_DEFS:
                raise ValueError(f"未注册的 Action Type：{action}")
            seq = (self.entries[-1]["seq"] + 1) if self.entries else 1
            entry = {"seq": seq, "ts": _now(), "action": action,
                     "actor": actor, "params": params or {},
                     "result": result or {}, "undone": False}
            self.entries.append(entry)
            self._persist()
            return entry

    def undo(self, seq: int, actor: str = "user") -> dict:
        """单步撤销：仅最新一条未撤销记录可撤销；执行注册的逆动作处理器。"""
        with self._lock:
            target = next((e for e in self.entries if e["seq"] == seq), None)
            if target is None:
                raise KeyError(f"动作不存在：{seq}")
            if target["undone"]:
                raise ValueError("该动作已撤销，不可重复撤销")
            # 单步约束：其后不得存在未撤销的动作
            if any(e["seq"] > seq and not e["undone"]
                   and e["action"] != "action.undo" for e in self.entries):
                raise ValueError("只能撤销最近一步操作")
            handler = self._handlers.get(target["action"])
            if handler is None:
                raise ValueError(f"动作 {target['action']} 暂不支持撤销")
            inv_result = handler(target) or {}
            target["undone"] = True
            out = self.log("action.undo", {"undo_seq": seq}, inv_result, actor)
            return out

    def payload(self) -> dict:
        return {"defs": [d.dump() for d in ACTION_DEFS.values()],
                "entries": self.entries}


action_log = ActionLog()
