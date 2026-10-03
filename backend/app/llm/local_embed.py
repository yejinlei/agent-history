# -*- coding: utf-8 -*-
"""本地嵌入模型（ONNX，离线可用）

为什么需要它：当前 LLM 服务（Sensenova 兼容协议）**不提供 embedding 模型**，
而 RAG 的语义检索依赖向量。本地模型的好处：
- 离线可用、无调用成本、无速率限制
- 与云端 LLM 解耦：换 LLM 供应商不影响检索

选型约束：磁盘与依赖体积。`sentence-transformers` 需 torch（2GB+），
因此走 ONNX 路线的 fastembed（onnxruntime 约 14MB + 模型 90MB）。

默认模型：`BAAI/bge-small-zh-v1.5`（512 维，90MB，中文）。
存放位置：项目根 `models/`（可用 LOCAL_EMBED_DIR 覆盖）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from app import config

log = logging.getLogger("chronosino.local_embed")


class LocalEmbedder:
    """fastembed 封装：懒加载模型，失败降级由调用方处理。"""

    name = "local"

    def __init__(self, model_name: str | None = None,
                 cache_dir: str | None = None) -> None:
        self.model_name = model_name or config.LOCAL_EMBED_MODEL
        self.cache_dir = cache_dir or config.LOCAL_EMBED_DIR
        self._model = None

    # ---------------- 可用性 ----------------
    def available(self) -> bool:
        """模型文件是否已下载到本地（不联网判断）。"""
        if not config.LOCAL_EMBED_ENABLED:
            return False
        d = Path(self.cache_dir)
        return d.exists() and any(d.rglob("*.onnx"))

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(
                model_name=self.model_name, cache_dir=self.cache_dir)
        return self._model

    # ---------------- 嵌入 ----------------
    @property
    def dim(self) -> int:
        return config.LOCAL_EMBED_DIM

    def embed_texts(self, texts: list[str],
                    batch_size: int = 16) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        return [[float(x) for x in v]
                for v in model.embed(texts, batch_size=batch_size)]


local_embedder = LocalEmbedder()

# 本地模型可用性探测结果（只探一次，避免每次检索都跑一次推理）
_local_ok: Optional[bool] = None


def get_embedder() -> Optional[Callable[[list[str]], list[list[float]]]]:
    """返回可用的 embed_fn：本地 ONNX 优先 → 云端 LLM → None（BM25 回退）。

    返回 None 时调用方按约定回退 BM25，不会报错。
    """
    global _local_ok
    if config.LOCAL_EMBED_ENABLED and _local_ok is not False \
            and local_embedder.available():
        try:
            if _local_ok is None:
                # 首次做一次最小调用，确认模型能真正加载（缺依赖时不会拖到运行时才炸）
                local_embedder.embed_texts(["测试"])
                _local_ok = True
            return local_embedder.embed_texts
        except Exception as e:
            _local_ok = False
            log.warning("本地嵌入模型不可用，回退云端/BM25: %s", e)

    from app.llm.base import get_llm
    llm = get_llm()
    if getattr(llm, "name", "stub") != "stub":
        return llm.embed_texts
    return None
