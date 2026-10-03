# -*- coding: utf-8 -*-
"""LLM 抽象：
- StubLLM：离线确定性模式，无需密钥，按本体数据与受众直接组织回答（默认）
- OpenAILLM：配置 OPENAI_API_KEY 后启用真实生成，回答仍强制基于检索证据（RAG）
"""
from __future__ import annotations

import os
import threading
import time

from app import config

# 网关实测：RPS 配额很低（并发 8 直接 429 "rps exhausted"），且空响应率高。
# 因此默认做两件事：① 调用之间强制最小间隔（限速）；② 空响应/异常退避重试。
_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "1.5"))
_MAX_RETRY = int(os.getenv("LLM_MAX_RETRY", "2"))
# 该网关的模型默认是「推理型」：token 全被 reasoning 吃掉，content 恒为空。
# 必须显式关闭思考（extra_body.thinking.type=disabled），否则抽取永远拿不到结果。
_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "1") != "0"
_gate_lock = threading.Lock()
_last_call = 0.0


def _wait_turn() -> None:
    """保证全局调用间隔 ≥ _MIN_INTERVAL（并发线程间也生效）。"""
    global _last_call
    with _gate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_call)
        _last_call = max(now, _last_call + _MIN_INTERVAL)
    if wait > 0:
        time.sleep(wait)


class StubLLM:
    name = "stub"

    def narrate(self, *, question: str, entity, audience: str, evidence) -> str:
        # 有命中实体：以该实体的结构化叙述作答（真实 LLM 模式会改写成自然对话）
        if entity is not None:
            text = getattr(entity.narrative, audience)
            if getattr(entity, "dispute", None):
                text += "\n\n【学术分歧】" + entity.dispute
            return text
        if evidence:
            return evidence[0].text
        return ""

    def chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        return ""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("stub 模式不提供向量 embedding（RAG 自动走 BM25）")


class OpenAILLM:
    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "") -> None:
        from openai import OpenAI
        # 超时必须显式：否则网络抖动会让同步调用无限挂起，进而卡死事件循环
        # 实测：结构化抽取单次生成可达 20s；超时 30s + 默认 2 次重试会把
        # 一次失败放大到 ~90s，进而让整本抽取看似"卡死"。故放宽超时、只重试 1 次。
        self.client = OpenAI(api_key=api_key or config.OPENAI_API_KEY,
                             base_url=base_url or config.OPENAI_BASE_URL,
                             timeout=60.0, max_retries=1)
        self.model = model or config.OPENAI_MODEL

    def narrate(self, *, question: str, entity, audience: str, evidence) -> str:
        audience_hint = {"student": "面向中学生，语言通俗、脉络清楚",
                         "scholar": "面向学者，严谨克制、保留争议",
                         "fan": "面向历史爱好者，可以讲故事、生动一些"}[audience]
        proof = "\n".join(f"[{i+1}]《{s.work}》：{s.text}" for i, s in enumerate(evidence))
        system = (
            "你是中国史研究助手。只能依据下列史料证据回答，不得编造；"
            "无法确证的内容要明确标注「存在争议/史料不足」，并在结尾给出引用编号。\n"
            f"受众要求：{audience_hint}\n\n可用史料证据：\n{proof}"
        )
        kw = {"model": self.model,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": question}],
              "temperature": 0.2}
        if _DISABLE_THINKING:      # 推理型模型默认会把 token 全用在思考上
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            resp = self.client.chat.completions.create(**kw)
        except Exception:
            kw.pop("extra_body", None)      # 网关不认该参数时降级重试
            resp = self.client.chat.completions.create(**kw)
        return resp.choices[0].message.content or ""

    def chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        kw = {"model": self.model,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
              "temperature": 0.2}
        # 网关实测：不设上限时可能长时间不返回；给足上限反而稳定且更快
        if max_tokens:
            kw["max_tokens"] = int(max_tokens)
        if _DISABLE_THINKING:
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
        last = ""
        delay = 2.0
        for attempt in range(_MAX_RETRY + 1):
            _wait_turn()                      # 限速：避免触发 429 rps exhausted
            try:
                resp = self.client.chat.completions.create(**kw)
                out = (resp.choices[0].message.content or "").strip()
                if out:                        # 空响应视为失败，退避重试
                    return out
                last = ""
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                # 不是所有网关都认 extra_body；被拒时去掉它再试（不消耗重试次数）
                if "extra_body" in kw and ("400" in str(e)
                                           or "BadRequest" in type(e).__name__):
                    kw.pop("extra_body", None)
                    continue
                if "429" in str(e) or "RateLimit" in type(e).__name__:
                    delay = max(delay, 6.0)    # 限流要多等一会儿
            if attempt < _MAX_RETRY:
                time.sleep(delay)
                delay *= 2
        return last                            # 全部失败：返回空串，由上层回退

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(model=config.OPENAI_EMBED_MODEL, input=texts)
        return [d.embedding for d in resp.data]


_llm = None
_extract_llm = None


def get_llm():
    global _llm
    if _llm is None:
        if config.LLM_PROVIDER == "openai" and config.OPENAI_API_KEY:
            _llm = OpenAILLM()
        else:
            _llm = StubLLM()
    return _llm


def get_extract_llm():
    """抽取专用 LLM（量大、追求便宜快），未单独配置时回退到统一网关。

    换服务商只需改 .env 里的 EXTRACT_* 三个变量，不用动代码。
    """
    global _extract_llm
    if _extract_llm is None:
        if (config.LLM_PROVIDER == "openai"
                and (config.EXTRACT_API_KEY or config.OPENAI_API_KEY)):
            _extract_llm = OpenAILLM(api_key=config.EXTRACT_API_KEY,
                                     base_url=config.EXTRACT_BASE_URL,
                                     model=config.EXTRACT_MODEL)
        else:
            _extract_llm = StubLLM()
    return _extract_llm
