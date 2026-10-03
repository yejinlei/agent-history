# -*- coding: utf-8 -*-
"""RAG 子系统：切块 + BM25 关键词检索（离线）+ 向量检索（在线可插拔）

- chunk_text：约 800 字一块、重叠约 100 字，尽量在句末/换行处切分
- tokenize：中文单字 + 相邻二元组；拉丁字母/数字整词；剔除高频虚字
- BM25：零依赖实现，docs 由调用方提供（生产为 store.all_chunks）
- retrieve_chunks：有 embedding 能力时走向量（余弦），否则/异常自动回退 BM25
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional

# ---------------- 切块 ----------------
def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    boundary_re = re.compile(r"[。！？!?\n；;]")
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[max(start, end - 140):end]
            cuts = list(boundary_re.finditer(window))
            if cuts:
                end = max(start, end - 140) + cuts[-1].end()
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)  # 保证前进，避免短段死循环
    return chunks


# ---------------- 分词 ----------------
# 高频虚字（作为单字时几乎无区分度，二元组仍保留它们）
_STOP_CHARS = set("的了是在我你他她它们和与及而于以也就都很被把将个之其所则为者不有这那一")
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks: list[str] = []
    toks.extend(_LATIN_RE.findall(text))
    cjk = _CJK_RE.findall(text)
    for ch in cjk:
        if ch not in _STOP_CHARS:
            toks.append(ch)
    for a, b in zip(cjk, cjk[1:]):
        toks.append(a + b)
    return toks


# ---------------- BM25 ----------------
class BM25:
    def __init__(self, docs: list[tuple[str, int, str]], k1: float = 1.5, b: float = 0.75):
        """docs: [(book_id, idx, text), ...]"""
        self.docs = docs
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d[2]) for d in docs]
        self.n = len(docs)
        self.avgdl = (sum(len(t) for t in self.tokens) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        for t in self.tokens:
            for w in set(t):
                df[w] = df.get(w, 0) + 1
        self.idf = {w: math.log(1 + (self.n - n + 0.5) / (n + 0.5)) for w, n in df.items()}

    def _score(self, q: list[str], toks: list[str]) -> float:
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        dl = len(toks)
        norm = 1 - self.b + self.b * (dl / self.avgdl) if self.avgdl else 1.0
        s = 0.0
        for w in q:
            if w not in tf or w not in self.idf:
                continue
            s += self.idf[w] * (tf[w] * (self.k1 + 1)) / (tf[w] + self.k1 * norm)
        return s

    def search(self, query: str, book_ids: Optional[set[str]] = None, topk: int = 6) -> list[dict]:
        q = tokenize(query)
        if not q:
            return []
        scored: list[dict] = []
        for (bid, idx, text), toks in zip(self.docs, self.tokens):
            if book_ids is not None and bid not in book_ids:
                continue
            s = self._score(q, toks)
            if s > 0:
                scored.append({"book_id": bid, "idx": idx, "text": text, "score": s})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:topk]


# ---------------- 向量检索 ----------------
def cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def vector_search(query_vec: list[float], docs: list[tuple[str, int, str, Optional[list[float]]]],
                  book_ids: Optional[set[str]] = None, topk: int = 6) -> list[dict]:
    """docs: [(book_id, idx, text, embedding), ...]；仅含有向量的文档参与。"""
    scored: list[dict] = []
    for bid, idx, text, emb in docs:
        if emb is None or (book_ids is not None and bid not in book_ids):
            continue
        s = cosine(query_vec, emb)
        if s > 0:
            scored.append({"book_id": bid, "idx": idx, "text": text, "score": float(s)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:topk]


# ---------------- 统一入口 ----------------
def retrieve_chunks(query: str,
                    docs: list[tuple[str, int, str]],
                    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
                    get_embedding: Optional[Callable[[str, int], Optional[list[float]]]] = None,
                    book_ids: Optional[set[str]] = None,
                    topk: int = 6) -> tuple[list[dict], str]:
    """返回 (hits, mode)。embed_fn: 批量把文本转向量（用于 query）；
    get_embedding(book_id, idx): 取已持久化的文档向量。
    任一条件不满足/调用异常 → BM25 回退。"""
    if embed_fn is not None and get_embedding is not None:
        try:
            qv = embed_fn([query])[0]
            vdocs = [(bid, idx, text, get_embedding(bid, idx)) for bid, idx, text in docs]
            if any(d[3] is not None for d in vdocs):
                hits = vector_search(qv, vdocs, book_ids, topk)
                if hits:
                    return hits, "vector"
        except Exception:
            pass
    return BM25(docs).search(query, book_ids, topk), "bm25"
