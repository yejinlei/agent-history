# -*- coding: utf-8 -*-
"""FastAPI 入口：启动 uvicorn app.main:app --reload"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router
from app.store import store
from app.data.books_sync import sync_builtin_books

app = FastAPI(title="华夏时空 ChronoSino API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def _startup_sync_builtin_books() -> None:
    """启动时**按需**构建内置古籍：只处理 INGEST_BOOKS 里显式列出的书。

    内置 27 本书是「待构建队列」，逐本叠加，不再一次性全量入库：
        INGEST_BOOKS=shiji python -m uvicorn app.main:app
        INGEST_BOOKS=shiji,zizhi_tongjian AUTO_MOUNT=1 ...（谨慎：自动全接受）
    常规用法是停服跑 scripts/ingest_one.py，或在线 POST /api/ingest/{book_id}。
    """
    bids = os.getenv("INGEST_BOOKS", "").strip()
    if not bids:
        return
    sync_builtin_books(store, bids.split(","), verbose=True,
                       auto_mount=os.getenv("AUTO_MOUNT", "") == "1")


@app.get("/")
def root():
    return {"name": "ChronoSino API", "docs": "/docs"}
