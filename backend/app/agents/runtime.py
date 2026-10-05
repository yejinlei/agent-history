# -*- coding: utf-8 -*-
"""Agent 运行时

- 按注册表中的工具授权执行读工具（图谱浏览 / RAG / 史料）；
- 写工具永不直接落地：生成 pending operation（确认卡），
  经 /operations/{id}/confirm 后才执行对应 executor，并写 ActionLog；
- 在线时由 LLM 按 Agent 系统提示词作答并在【OPS】段中提出写操作；
  stub 模式走确定性意图识别与证据摘要。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import Command

from app.ingestion.mount import mount_graph
from app.ingestion.rag import retrieve_chunks
from app.llm.base import get_llm
from app.llm.local_embed import get_embedder
from app.ontology.actions import action_log
from app import config
from app.agents.registry import TOOLS, AgentRegistry

_DATA = Path(config.DATA_DIR)
_OPS_FILE = _DATA / "pending_ops.json"

# action（确认卡动作）↔ tool id（注册表勾选；挂载与卸载共用 book_mount 授权）
_ACTION_TOOL = {
    "book.mount": "book_mount",
    "book.unmount": "book_mount",
    "book.delete": "book_delete",
}

# action（确认卡动作）↔ book.* ActionLog 动作名
_ACTION_LOG_MAP = {
    "book.mount": "book.mount",
    "book.unmount": "book.unmount",
    "book.delete": "book.delete",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PendingOps:
    def __init__(self) -> None:
        self.ops: list[dict] = []
        self._lock = threading.RLock()
        if _OPS_FILE.exists():
            try:
                self.ops = json.loads(_OPS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.ops = []

    def _persist(self) -> None:
        _OPS_FILE.write_text(
            json.dumps(self.ops, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def add(self, agent_id: str, action: str, params: dict,
            summary: str) -> dict:
        with self._lock:
            op = {"id": uuid.uuid4().hex[:12], "ts": _now(),
                  "agent_id": agent_id, "action": action,
                  "params": params, "summary": summary,
                  "status": "pending"}
            self.ops.append(op)
            self._persist()
            return op

    def get(self, op_id: str) -> dict | None:
        return next((o for o in self.ops if o["id"] == op_id), None)

    def set_status(self, op_id: str, status: str,
                   result: dict | None = None) -> dict:
        with self._lock:
            op = self.get(op_id)
            if op is None:
                raise KeyError("操作不存在")
            if op["status"] != "pending":
                raise ValueError("该操作已处理")
            op["status"] = status
            if result is not None:
                op["result"] = result
            self._persist()
            return op

    def pending(self) -> list[dict]:
        return [o for o in self.ops if o["status"] == "pending"]

    def cancel_for_agent(self, agent_id: str) -> int:
        """Agent 被删除：联动取消其全部 pending 卡。"""
        n = 0
        with self._lock:
            for op in self.ops:
                if op["agent_id"] == agent_id and op["status"] == "pending":
                    op["status"] = "cancelled"
                    n += 1
            if n:
                self._persist()
        return n

    def cancel_unauthorized(self, agent_id: str) -> int:
        """Agent 工具被收窄：取消其动作已不在当前授权内的 pending 卡。"""
        agent = AgentRegistry().get(agent_id)
        tools = set(agent.get("tools", [])) if agent else set()
        n = 0
        with self._lock:
            for op in self.ops:
                if (op["agent_id"] == agent_id and op["status"] == "pending"
                        and _ACTION_TOOL.get(op["action"]) not in tools):
                    op["status"] = "cancelled"
                    n += 1
            if n:
                self._persist()
        return n


pending_ops = PendingOps()


# ============================ 读工具 ============================
def _read_context(agent: dict, question: str) -> dict:
    """按工具授权收集上下文（store 由调用方注入以避免循环导入）。"""
    from app.store import store
    tools = set(agent.get("tools", []))
    out: dict = {"entities": [], "chunks": [], "sources": []}

    if {"kg_browse", "kg_subgraph"} & tools:
        out["entities"] = [
            {"kind": k, "id": o.id, "name": getattr(o, "name", None) or o.id}
            for k, o in store.search(question)]

    if "rag_search" in tools and store.chunks:
        docs = [(bid, ch["idx"], ch["text"]) for bid, ch in store.all_chunks]
        emb_index = {bid: {ch["idx"]: ch.get("embedding")
                           for ch in store.chunks_of(bid)}
                     for bid in store.chunks}
        has_vectors = any(v is not None for m in emb_index.values()
                          for v in m.values())
        try:
            hits, _ = retrieve_chunks(
                question, docs,
                embed_fn=get_embedder() if has_vectors else None,
                get_embedding=lambda bid, idx:
                    emb_index.get(bid, {}).get(idx))
            out["chunks"] = hits
        except Exception:
            pass

    if "source_quote" in tools:
        kind_obj = next(((h["kind"], h["id"]) for h in out["entities"]), None)
        if kind_obj:
            k, oid = kind_obj
            obj = store.get(k, oid)
            if obj is not None:
                out["sources"] = [s.model_dump()
                                  for s in store.sources_of(k, obj)][:4]
    return out


# ============================ 意图识别（stub） ============================
def _detect_intent(question: str) -> list[tuple[str, dict, str]]:
    """确定性识别书籍写意图：返回 [(action, params, summary)]。"""
    from app.store import store
    q = question.strip()
    out: list[tuple[str, dict, str]] = []

    rules = [
        (("删除", "删掉"), "book.delete", "删除书籍"),
        (("卸载", "卸下"), "book.unmount", "卸载书籍"),
        (("挂载", "挂上", "安装"), "book.mount", "挂载书籍"),
    ]
    action = None
    for words, act, _label in rules:
        if any(w in q for w in words):
            action = act
            break
    if action is None:
        return out

    for b in store.books:
        if b.title in q or b.id in q or (
                len(b.title) >= 3 and b.title[:3] in q):
            out.append((action, {"book_id": b.id},
                        f"{'删除' if action == 'book.delete' else ('卸载' if action == 'book.unmount' else '挂载')}《{b.title}》"))
            break
    return out


# ============================ Agent 执行 ============================
def run_agent(agent: dict, question: str) -> dict:
    ctx = _read_context(agent, question)
    llm = get_llm()
    answer = ""
    intent: list[tuple[str, dict, str]] = []

    if getattr(llm, "name", "stub") != "stub":
        system = (agent["system_prompt"] +
                  "\n\n可用工具：" +
                  ", ".join(f"{k}（{TOOLS[k]['label']}）" for k in agent["tools"]) +
                  "\n注意：你没有 function-calling 通道，读工具结果已由系统预先检索并附在用户消息中，"
                  "直接据此作答，禁止输出 <tool_call> 之类的工具调用标记；"
                  "证据不足时明说，可用自身知识但需注明「无书内证据」。\n"
                  "如需执行写操作，必须在回答正文之后另起一行，严格按格式输出：\n"
                  "【OPS】[{\"action\":\"book.mount|book.unmount|book.delete\","
                  "\"params\":{\"book_id\":\"...\"},\"summary\":\"给用户看的一句话\"}]")
        ref_text = "\n".join(
            [f"实体：{e['name']}" for e in ctx["entities"]] +
            [f"书摘：{h['text'][:200]}" for h in ctx["chunks"][:4]] +
            [f"史料：{s['work']} {s['text'][:120]}" for s in ctx["sources"]])
        raw = ""
        try:
            raw = llm.chat(system, f"问题：{question}\n可参考：\n{ref_text}")
        except Exception as e:
            # 在线生成失败不直接 500：回退规则答案（与抽取路径降级策略一致）
            answer = _stub_answer(agent, question, ctx)
            answer += f"\n（在线生成暂不可用：{type(e).__name__}）"
            return {"answer": answer, "operations": [],
                    "context": {"entities": ctx["entities"][:8],
                                "chunks": [{"book_id": h["book_id"], "idx": h["idx"],
                                            "text": h["text"][:240]}
                                           for h in ctx["chunks"][:4]],
                                "sources": ctx["sources"]}}
        answer, ops_raw = _split_ops(raw)
        for op in ops_raw:
            act = op.get("action")
            tool_id = _ACTION_TOOL.get(str(act))
            if tool_id and TOOLS[tool_id]["write"]:
                intent.append((act, op.get("params", {}),
                               str(op.get("summary", act))))
    else:
        answer = _stub_answer(agent, question, ctx)
        intent = _detect_intent(question)

    # 仅保留该 Agent 被授权的写工具
    allowed = {t for t in agent.get("tools", [])}
    operations = []
    for act, params, summary in intent:
        if _ACTION_TOOL.get(act) not in allowed:
            continue
        operations.append(pending_ops.add(agent["id"], act, params, summary))

    return {"answer": answer, "operations": operations,
            "context": {"entities": ctx["entities"][:8],
                        "chunks": [{"book_id": h["book_id"],
                                    "idx": h["idx"],
                                    "text": h["text"][:240]}
                                   for h in ctx["chunks"][:4]],
                        "sources": ctx["sources"]}}


def _split_ops(raw: str) -> tuple[str, list[dict]]:
    m = re.search(r"【OPS】", raw)
    if not m:
        return raw.strip(), []
    answer = raw[:m.start()].strip()
    tail = raw[m.end():].strip()
    # 以最后一个 ']' 为 JSON 数组边界（summary 文本里含 】 也不会截断）
    end = tail.rfind("]")
    if end < 0:
        return answer, []
    try:
        arr = json.loads(tail[:end + 1])
        return answer, arr if isinstance(arr, list) else []
    except Exception:
        return answer, []


def _stub_answer(agent: dict, question: str, ctx: dict) -> str:
    lines = [f"（{agent['name']}·规则模式）关于「{question}」："]
    if ctx["entities"]:
        lines.append("图谱中相关：" + "、".join(
            f"{e['name']}" for e in ctx["entities"][:6]))
    if ctx["chunks"]:
        h = ctx["chunks"][0]
        lines.append(f"书摘（{h['book_id']} 切块{h['idx'] + 1}）："
                     f"{h['text'][:120]}")
    if ctx["sources"]:
        s = ctx["sources"][0]
        lines.append(f"史料《{s['work']}》：{s['text'][:100]}")
    if len(lines) == 1:
        lines.append("未检索到直接证据；可尝试上传相关书籍后再问，或换个问法。")
    return "\n".join(lines)


# ============================ 确认执行 ============================
def _agent_now(agent_id: str) -> dict | None:
    # 独立注册表实例读当前授权（custom 持久化文件一致）
    return AgentRegistry().get(agent_id)


def confirm_operation(op_id: str, actor: str = "user") -> dict:
    op = pending_ops.get(op_id)
    if op is None:
        raise KeyError("操作不存在")

    # 人审防线（防提示注入/降权后遗留）：
    # 1) Agent 仍存在，且该动作所需工具在其【当前】授权内；
    agent = _agent_now(op["agent_id"])
    if agent is None:
        raise ValueError("Agent 已不存在；请取消该操作")
    tool_id = _ACTION_TOOL.get(op["action"])
    if tool_id is None or tool_id not in agent.get("tools", []):
        raise ValueError("该 Agent 当前已无此操作授权")
    # 2) 参数目标真实存在（summary 仅作说明，执行只认 params）；
    bid = op["params"].get("book_id")
    if not bid:
        raise ValueError("缺少 book_id 参数")
    from app.store import store
    if next((b for b in store.books if b.id == bid), None) is None:
        raise ValueError("目标书籍不存在")

    result = _EXECUTORS[op["action"]](op["params"])
    updated = pending_ops.set_status(op_id, "confirmed", result)
    log_action = _ACTION_LOG_MAP.get(op["action"])
    if log_action:
        action_log.log(log_action, op["params"], result or {}, actor)
    return updated  # 返回完整 op（前端卡片据此更新）


def cancel_operation(op_id: str) -> dict:
    return pending_ops.set_status(op_id, "cancelled")


# ---- executors ----
def _exec_mount(params: dict) -> dict:
    from app.store import store
    bid = params["book_id"]
    book = next((b for b in store.books if b.id == bid), None)
    if book is None:
        raise ValueError("书籍不存在")
    if book.mounted:
        return {"already": True}
    thread_id = f"mount-{bid}-{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread_id}}
    mount_graph.invoke({"book_id": bid}, cfg)
    state = mount_graph.get_state(cfg)
    n = len(state.values.get("proposals", []))
    final = mount_graph.invoke(Command(resume=[True] * n), cfg)
    return final.get("result") or {}


def _exec_unmount(params: dict) -> dict:
    from app.store import store
    bid = params["book_id"]
    book = next((b for b in store.books if b.id == bid), None)
    if book is None:
        raise ValueError("书籍不存在")
    if not book.mounted:
        return {"already": True}
    store.uncommit_book(book)
    return {"book_id": bid, "unmounted": True}


def _exec_delete(params: dict) -> dict:
    from app.store import store
    bid = params["book_id"]
    book = next((b for b in store.books if b.id == bid), None)
    if book is None:
        raise ValueError("书籍不存在")
    tdir = store.trash_book(book)
    return {"book_id": bid, "trash": str(tdir)}


_EXECUTORS = {
    "book.mount": _exec_mount,
    "book.unmount": _exec_unmount,
    "book.delete": _exec_delete,
}
