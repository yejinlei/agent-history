# -*- coding: utf-8 -*-
"""DuckDB 数仓（OLAP 层）

分工（见 docs/PLAN.md §2「图 + 表混合」）：
- Kuzu   ：实体与关系的**图真值源**（邻域 / 子图 / 路径遍历）
- DuckDB ：书籍原文、切块、时间/空间索引的**分析真值源**
           （时间切片、空间过滤、全文检索、聚合统计）

单一真值源约定：
- 实体与关系 → Kuzu 为准（DuckDB 侧为可重建副本）
- 切块与原文 → **DuckDB 为准**（Kuzu 不再存 Chunk，减轻 save 全量重写负担）

约束：DuckDB 与 Kuzu 均为**单进程写**，离线脚本落库前需停服（与既有约束一致）。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

_DATA_DIR = Path(__file__).resolve().parent / "data"
_WAREHOUSE_DIR = _DATA_DIR / "warehouse"
_DB_FILE = _WAREHOUSE_DIR / "chronosino.duckdb"

_DDL = [
    # ---- ODS：书籍元数据 ----
    """CREATE TABLE IF NOT EXISTS books_raw(
        book_id VARCHAR PRIMARY KEY,
        title VARCHAR,
        author VARCHAR,
        level INTEGER,              -- 史料层级 1 原始 | 2 现代史学 | 3 大众
        stance VARCHAR,             -- 立场：正史/私家史/史评/现代史学/文学
        author_context VARCHAR,     -- 作者身份与时代背景
        dynasty_hint VARCHAR,
        span_start INTEGER,
        span_end INTEGER,
        calendar VARCHAR,           -- 该书主用纪年体系
        char_count BIGINT,
        source_path VARCHAR,
        mounted BOOLEAN,
        ingested_at TIMESTAMP
    )""",
    # ---- ODS：切块（原文真值源）----
    """CREATE TABLE IF NOT EXISTS chunks(
        book_id VARCHAR,
        idx BIGINT,
        text TEXT,
        char_start BIGINT,
        char_end BIGINT,
        token_count INTEGER,
        embedding TEXT,             -- JSON 数组；无 embedding 服务时为 NULL
        PRIMARY KEY(book_id, idx)
    )""",
    # 注：**不要**在 chunks.book_id 上建索引。实测在批量 executemany 写入
    # （迁移 3.6 万条）后，索引会与数据不一致——等值查询走索引返回 0，
    # 而 LIKE（不走索引）能查到正确结果。DuckDB 列式扫描 3.6 万行是毫秒级，
    # 无需该索引，去掉以避免这类静默错误。
    # ---- DWD：实体明细（Kuzu 的分析副本，供时间/空间切片查询）----
    # 真值源仍是 Kuzu；本表由 sync_entities 全量重建，可随时重生成。
    # 主键为 (kind, entity_id)：同一名称可能既是人物也是事件（如「张骞」）。
    """CREATE TABLE entities(
        kind VARCHAR,
        entity_id VARCHAR,
        name VARCHAR,
        year INTEGER,           -- Event 年份；其余类型为 NULL
        dynasty_id VARCHAR,
        place_id VARCHAR,
        lon DOUBLE,             -- 未解析为 NULL，禁止 0.0 占位
        lat DOUBLE,
        from_book VARCHAR,
        confidence DOUBLE,
        PRIMARY KEY(kind, entity_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_entities_year ON entities(year)""",
    """CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind)""",
    # ---- 增量编排：抽取进度（逐本叠加，支持断点续跑）----
    # 主键含 model：换抽取模型（heuristic/llm-xxx）时自动重跑，不与旧结果混淆。
    """CREATE TABLE IF NOT EXISTS ingest_progress(
        book_id VARCHAR,
        piece_idx BIGINT,
        model VARCHAR,
        n_persons INTEGER,
        n_events INTEGER,
        n_links INTEGER,
        ts TIMESTAMP,
        PRIMARY KEY(book_id, piece_idx, model)
    )""",
    # ---- 增量编排：每块的原始抽取结果（落盘后可用不同 TopN 重算提案，不重烧 token）----
    """CREATE TABLE IF NOT EXISTS extract_raw(
        book_id VARCHAR,
        piece_idx BIGINT,
        model VARCHAR,
        payload VARCHAR,
        ts TIMESTAMP,
        PRIMARY KEY(book_id, piece_idx, model)
    )""",
    # ---- 增量编排：每本书的构建状态（供 CLI / API 展示进度）----
    """CREATE TABLE IF NOT EXISTS book_runs(
        book_id VARCHAR PRIMARY KEY,
        stage VARCHAR,             -- pending|chunked|extracting|prepared|mounted
        chunks INTEGER,
        pieces INTEGER,
        done INTEGER,
        model VARCHAR,
        mode VARCHAR,              -- heuristic|llm
        proposals INTEGER,
        mounted BOOLEAN,
        updated TIMESTAMP
    )""",
    # ---- 增量编排：失败台账（可靠性/完整性）----
    # 与 ingest_progress 分开存：失败的块**不写进度**，只在这里记一笔，
    # 这样「没跑过」「跑失败」「跑成功但为空」三者可以区分，补跑才不会白跑或漏跑。
    # kind: error=抛异常 | empty=返回空（多为网关空响应/限流）| fallback=大模型回退启发式
    """CREATE TABLE IF NOT EXISTS ingest_failures(
        book_id VARCHAR,
        piece_idx BIGINT,
        model VARCHAR,
        kind VARCHAR,
        error VARCHAR,
        attempt INTEGER,
        ts TIMESTAMP
    )""",
    # ---- 增量编排：构建参数指纹（完整性）----
    # 切块/抽取参数或书源文件一变，块边界就变了，旧进度与旧 raw 全部失效；
    # 记下指纹，下次开跑时自动发现漂移并提示/重跑，避免"进度在、数据已错"。
    """CREATE TABLE IF NOT EXISTS book_run_params(
        book_id VARCHAR PRIMARY KEY,
        signature VARCHAR,
        params VARCHAR,
        updated TIMESTAMP
    )""",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Warehouse:
    """DuckDB 封装：书籍原文与切块的读写。

    连接非线程安全（与 Kuzu 同），统一用锁串行化。
    """

    def __init__(self, db_path: Path = _DB_FILE) -> None:
        _WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = duckdb.connect(str(db_path))
        self._init_schema()

    # ---------------- 基础 ----------------
    def _init_schema(self) -> None:
        with self._lock:
            # entities 是 Kuzu 的**可重建副本**，启动时即全量重建，
            # 因此每次直接 DROP 重建，避免 schema 演进带来的迁移负担。
            # 注意：chunks 是真值源，绝不可 DROP。
            self._conn.execute("DROP TABLE IF EXISTS entities")
            # 清理历史遗留的 chunks 索引（会与数据不一致，见 _DDL 注释）
            self._conn.execute("DROP INDEX IF EXISTS idx_chunks_book")
            for ddl in _DDL:
                self._conn.execute(ddl)

    def execute(self, sql: str, params: Iterable | None = None):
        with self._lock:
            return (self._conn.execute(sql, list(params))
                    if params is not None else self._conn.execute(sql))

    def query(self, sql: str, params: Iterable | None = None) -> list[tuple]:
        with self._lock:
            r = self.execute(sql, params)
            return r.fetchall()

    @contextmanager
    def transaction(self):
        """与 Store.transaction 用法一致：退出时提交，异常回滚。"""
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            try:
                yield self
                self._conn.execute("COMMIT")
            except Exception:
                # DuckDB 在语句出错时会自动中止事务；此时再 ROLLBACK 会抛
                # "cannot rollback - no transaction is active"，把真正的错误顶掉，
                # 只留下一句看不懂的话（实体副本曾被这样静默同步失败过）。
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ---------------- 书籍 ----------------
    def upsert_book(self, meta: dict) -> None:
        sql = """INSERT OR REPLACE INTO books_raw
                 (book_id, title, author, level, stance, author_context,
                  dynasty_hint, span_start, span_end, calendar, char_count,
                  source_path, mounted, ingested_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        span = meta.get("span") or (None, None)
        self.execute(sql, [
            meta.get("id") or meta.get("book_id"),
            meta.get("title"), meta.get("author"),
            meta.get("level"), meta.get("stance"), meta.get("author_context"),
            meta.get("dynasty_hint") or meta.get("dynasty_id"),
            span[0], span[1], meta.get("calendar"),
            meta.get("char_count"), meta.get("source_path"),
            bool(meta.get("mounted", False)), _now(),
        ])

    def book_ids(self) -> set[str]:
        return {r[0] for r in self.query("SELECT book_id FROM books_raw")}

    # ---------------- 切块 ----------------
    def chunk_count(self, book_id: str | None = None) -> int:
        if book_id is None:
            rows = self.query("SELECT count(*) FROM chunks")
        else:
            rows = self.query("SELECT count(*) FROM chunks WHERE book_id=?",
                              [book_id])
        return int(rows[0][0]) if rows else 0

    def replace_chunks(self, book_id: str,
                       chunks: list[dict]) -> int:
        """整本替换（先删后插），返回写入条数。"""
        with self.transaction():
            self.execute("DELETE FROM chunks WHERE book_id=?", [book_id])
            if not chunks:
                return 0
            rows = []
            for c in chunks:
                emb = c.get("embedding")
                rows.append([
                    book_id, int(c["idx"]), c.get("text", ""),
                    c.get("char_start"), c.get("char_end"),
                    c.get("token_count"), _j(emb) if emb is not None else None,
                ])
            self._conn.executemany(
                """INSERT INTO chunks
                   (book_id, idx, text, char_start, char_end, token_count, embedding)
                   VALUES (?,?,?,?,?,?,?)""", rows)
        return len(rows)

    def drop_chunks(self, book_id: str) -> None:
        self.execute("DELETE FROM chunks WHERE book_id=?", [book_id])

    def chunks_of(self, book_id: str) -> list[dict]:
        rows = self.query(
            """SELECT idx, text, embedding FROM chunks
               WHERE book_id=? ORDER BY idx""", [book_id])
        return [{"idx": int(i), "text": t,
                 "embedding": json.loads(e) if e else None}
                for i, t, e in rows]

    def all_chunks(self) -> list[tuple[str, dict]]:
        rows = self.query(
            "SELECT book_id, idx, text, embedding FROM chunks ORDER BY book_id, idx")
        return [(b, {"idx": int(i), "text": t,
                     "embedding": json.loads(e) if e else None})
                for b, i, t, e in rows]

    def search_text(self, term: str, limit: int = 50) -> list[dict]:
        """全文粗筛（SQL LIKE）。中文精确检索仍走 BM25，此处用于按词定位。"""
        rows = self.query(
            """SELECT book_id, idx, text FROM chunks
               WHERE text LIKE ? LIMIT ?""", [f"%{term}%", limit])
        return [{"book_id": b, "idx": int(i), "text": t} for b, i, t in rows]

    # ---------------- 实体（时间 / 空间切片）----------------
    def replace_entities(self, rows: list[dict]) -> int:
        """全量重建实体副本（真值源为 Kuzu，本表可随时重生成）。

        空 rows 一律原样返回、**不删表**：这里曾经「先 DELETE 全表再 executemany」，
        一旦载入流程出现异常（例外被上层吞掉），表就被擦成 0 行而没人知道——
        实测出现过整个 entities 表空掉、时间窗/空间窗查询拿不到数据的情况。
        副本是随时可重建的衍生数据，宁可留着旧行，也不能被一次失误清空。
        """
        if not rows:
            return 0
        with self.transaction():
            self.execute("DELETE FROM entities")
            self._conn.executemany(
                """INSERT INTO entities
                   (entity_id, kind, name, year, dynasty_id, place_id,
                    lon, lat, from_book, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [[r.get("entity_id"), r.get("kind"), r.get("name"),
                  r.get("year"), r.get("dynasty_id"), r.get("place_id"),
                  r.get("lon"), r.get("lat"), r.get("from_book"),
                  r.get("confidence")] for r in rows])
        return len(rows)

    def upsert_entities(self, rows: list[dict]) -> int:
        """按 entity_id 增量覆盖：只删再插传入的实体，不动其余行。

        与 replace_entities 的区别是全表删除 vs 精确删除。挂载一本书时只有
        该书的实体变化，全表重灌实测要 117 秒，而增量只写变化的那几千条。
        """
        if not rows:
            return 0
        ids = [r.get("entity_id") for r in rows]
        with self.transaction():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.execute("DELETE FROM entities USING "
                             "(SELECT unnest(?) AS eid) t "
                             "WHERE entity_id = t.eid", [chunk])
            self._conn.executemany(
                """INSERT INTO entities
                   (entity_id, kind, name, year, dynasty_id, place_id,
                    lon, lat, from_book, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [[r.get("entity_id"), r.get("kind"), r.get("name"),
                  r.get("year"), r.get("dynasty_id"), r.get("place_id"),
                  r.get("lon"), r.get("lat"), r.get("from_book"),
                  r.get("confidence")] for r in rows])
        return len(rows)

    def delete_entities(self, ids: list[str]) -> int:
        """按 entity_id 精确删除（卸载一本书时用）。"""
        if not ids:
            return 0
        n = 0
        with self.transaction():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.execute("DELETE FROM entities USING "
                             "(SELECT unnest(?) AS eid) t "
                             "WHERE entity_id = t.eid", [chunk])
                n += len(chunk)
        return n

    def entity_count(self) -> int:
        rows = self.query("SELECT count(*) FROM entities")
        return int(rows[0][0]) if rows else 0

    def query_entities(self, *, year_from: int | None = None,
                       year_to: int | None = None,
                       bbox: tuple[float, float, float, float] | None = None,
                       kind: str | None = None, book_id: str | None = None,
                       limit: int = 200) -> list[dict]:
        """按时间窗 / 空间窗 / 类型 / 来源书过滤实体。

        bbox 为 (west, south, east, north)。
        """
        sql = ("SELECT entity_id, kind, name, year, dynasty_id, place_id,"
               " lon, lat, from_book, confidence FROM entities WHERE 1=1")
        args: list = []
        if year_from is not None:
            sql += " AND year >= ?"
            args.append(year_from)
        if year_to is not None:
            sql += " AND year <= ?"
            args.append(year_to)
        if bbox:
            w, s, e, n = bbox
            sql += " AND lon IS NOT NULL AND lon BETWEEN ? AND ?" \
                   " AND lat BETWEEN ? AND ?"
            args += [w, e, s, n]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if book_id:
            sql += " AND from_book = ?"
            args.append(book_id)
        sql += " ORDER BY year NULLS LAST LIMIT ?"
        args.append(limit)
        rows = self.query(sql, args)
        return [{"entity_id": a, "kind": b, "name": c, "year": d,
                 "dynasty_id": e, "place_id": f, "lon": g, "lat": h,
                 "from_book": i, "confidence": j}
                for a, b, c, d, e, f, g, h, i, j in rows]


    # ---------------- 增量编排进度（逐本叠加）----------------
    def mark_piece_done(self, book_id: str, piece_idx: int, model: str,
                        out: dict | None = None) -> None:
        """记录一个抽取块已完成（幂等：同 (book, idx, model) 覆盖写）。"""
        out = out or {}
        self.execute(
            """INSERT OR REPLACE INTO ingest_progress
               (book_id, piece_idx, model, n_persons, n_events, n_links, ts)
               VALUES (?,?,?,?,?,?,?)""",
            [book_id, int(piece_idx), model,
             len(out.get("persons", []) or []),
             len(out.get("events", []) or []),
             len(out.get("links", []) or []), _now()])

    def done_pieces(self, book_id: str, model: str) -> set[int]:
        rows = self.query(
            "SELECT piece_idx FROM ingest_progress WHERE book_id=? AND model=?",
            [book_id, model])
        return {int(r[0]) for r in rows}

    def reset_progress(self, book_id: str, model: str | None = None) -> int:
        """清空某本书（某模型）的抽取进度；返回删除条数。"""
        if model is None:
            rows = self.query(
                "SELECT count(*) FROM ingest_progress WHERE book_id=?", [book_id])
            self.execute("DELETE FROM ingest_progress WHERE book_id=?", [book_id])
        else:
            rows = self.query(
                "SELECT count(*) FROM ingest_progress WHERE book_id=? AND model=?",
                [book_id, model])
            self.execute(
                "DELETE FROM ingest_progress WHERE book_id=? AND model=?",
                [book_id, model])
        return int(rows[0][0]) if rows else 0

    def save_extract_raw(self, book_id: str, piece_idx: int, model: str,
                         out: dict) -> None:
        """落盘单块原始抽取结果（含空结果，便于区分"没抽到"与"没跑过"）。"""
        self.execute(
            """INSERT OR REPLACE INTO extract_raw
               (book_id, piece_idx, model, payload, ts) VALUES (?,?,?,?,?)""",
            [book_id, int(piece_idx), model,
             json.dumps(out, ensure_ascii=False), _now()])

    def load_extract_raw(self, book_id: str, model: str) -> list[tuple[int, dict]]:
        rows = self.query(
            """SELECT piece_idx, payload FROM extract_raw
               WHERE book_id=? AND model=? ORDER BY piece_idx""",
            [book_id, model])
        out = []
        for idx, payload in rows:
            try:
                out.append((int(idx), json.loads(payload)))
            except Exception:
                continue
        return out

    def extract_models(self, book_id: str) -> list[str]:
        """该书已落盘原始抽取结果的模型列表（供 --finalize-only 自动选模型）。"""
        rows = self.query(
            "SELECT DISTINCT model FROM extract_raw WHERE book_id=?", [book_id])
        return [r[0] for r in rows if r[0]]

    def reset_failed_pieces(self, book_id: str, model: str | None = None) -> int:
        """只清掉「抽取为空」的块（多为网关空响应/限流导致的回退），保留成功块。

        这样补跑时不会白白重跑已经成功的块，省钱省时间。
        """
        empty = "n_persons=0 AND n_events=0 AND n_links=0"
        if model is None:
            rows = self.query(
                f"SELECT count(*) FROM ingest_progress WHERE book_id=? AND {empty}",
                [book_id])
            self.execute(
                f"DELETE FROM ingest_progress WHERE book_id=? AND {empty}",
                [book_id])
        else:
            rows = self.query(
                f"SELECT count(*) FROM ingest_progress WHERE book_id=? AND model=? AND {empty}",
                [book_id, model])
            self.execute(
                f"DELETE FROM ingest_progress WHERE book_id=? AND model=? AND {empty}",
                [book_id, model])
        return int(rows[0][0]) if rows else 0

    # ---------------------------------------------- 失败台账 / 精准补跑
    def mark_piece_failed(self, book_id: str, piece_idx: int, model: str,
                          kind: str, error: str = "", attempt: int = 0) -> None:
        """记一笔失败：kind = error(异常) | empty(空结果) | fallback(大模型回退)。"""
        self.execute(
            """INSERT INTO ingest_failures
               (book_id, piece_idx, model, kind, error, attempt, ts)
               VALUES (?,?,?,?,?,?,?)""",
            [book_id, int(piece_idx), model, kind, (error or "")[:300],
             int(attempt), _now()])

    def failed_pieces(self, book_id: str, model: str,
                      kinds: Iterable[str] | None = None) -> dict[int, str]:
        """失败块 {piece_idx: kind}（同一块多次失败取最后一条）。"""
        sql = """SELECT piece_idx, kind FROM ingest_failures
                 WHERE book_id=? AND model=?"""
        args: list = [book_id, model]
        if kinds:
            ks = list(kinds)
            sql += " AND kind IN (%s)" % ",".join(["?"] * len(ks))
            args += ks
        out: dict[int, str] = {}
        for idx, kind in self.query(sql, args):
            out[int(idx)] = kind
        return out

    def failure_counts(self, book_id: str, model: str) -> dict[tuple[int, str], int]:
        """{(piece_idx, kind): 累计次数}——用来判断「这块已经补过几次还是空」。"""
        rows = self.query(
            """SELECT piece_idx, kind, count(*) FROM ingest_failures
               WHERE book_id=? AND model=? GROUP BY piece_idx, kind""",
            [book_id, model])
        return {(int(i), k): int(n) for i, k, n in rows}

    def clear_failures(self, book_id: str, model: str,
                       idxs: Iterable[int]) -> int:
        idxs = [int(i) for i in idxs]
        if not idxs:
            return 0
        ph = ",".join(["?"] * len(idxs))
        rows = self.query(
            f"""SELECT count(*) FROM ingest_failures
                WHERE book_id=? AND model=? AND piece_idx IN ({ph})""",
            [book_id, model] + idxs)
        self.execute(
            f"""DELETE FROM ingest_failures
                WHERE book_id=? AND model=? AND piece_idx IN ({ph})""",
            [book_id, model] + idxs)
        return int(rows[0][0]) if rows else 0

    def reset_pieces(self, book_id: str, model: str,
                     idxs: Iterable[int]) -> int:
        """清掉指定块的进度 + 失败台账（补跑前调用），返回清除的进度条数。"""
        idxs = [int(i) for i in idxs]
        if not idxs:
            return 0
        ph = ",".join(["?"] * len(idxs))
        rows = self.query(
            f"""SELECT count(*) FROM ingest_progress
                WHERE book_id=? AND model=? AND piece_idx IN ({ph})""",
            [book_id, model] + idxs)
        with self.transaction():
            self.execute(
                f"""DELETE FROM ingest_progress
                    WHERE book_id=? AND model=? AND piece_idx IN ({ph})""",
                [book_id, model] + idxs)
            self.execute(
                f"""DELETE FROM ingest_failures
                    WHERE book_id=? AND model=? AND piece_idx IN ({ph})""",
                [book_id, model] + idxs)
        return int(rows[0][0]) if rows else 0

    def reset_failures(self, book_id: str, model: str | None = None) -> int:
        """清空失败台账（整本重跑 / 参数漂移时用）。"""
        if model is None:
            rows = self.query(
                "SELECT count(*) FROM ingest_failures WHERE book_id=?",
                [book_id])
            self.execute("DELETE FROM ingest_failures WHERE book_id=?",
                         [book_id])
        else:
            rows = self.query(
                """SELECT count(*) FROM ingest_failures
                   WHERE book_id=? AND model=?""", [book_id, model])
            self.execute(
                "DELETE FROM ingest_failures WHERE book_id=? AND model=?",
                [book_id, model])
        return int(rows[0][0]) if rows else 0

    def raw_pieces(self, book_id: str, model: str) -> set[int]:
        rows = self.query(
            "SELECT piece_idx FROM extract_raw WHERE book_id=? AND model=?",
            [book_id, model])
        return {int(r[0]) for r in rows}

    def raw_modes(self, book_id: str, model: str) -> dict[int, str]:
        """已落盘每块的实际抽取模式（用来找「大模型静默回退启发式」的块）。"""
        try:
            rows = self.query(
                """SELECT piece_idx, json_extract_string(payload, '$.mode')
                   FROM extract_raw WHERE book_id=? AND model=?""",
                [book_id, model])
            return {int(i): (m or "heuristic") for i, m in rows}
        except Exception:
            return {i: (o.get("mode") or "heuristic")
                    for i, o in self.load_extract_raw(book_id, model)}

    def reset_raw(self, book_id: str, model: str | None = None) -> int:
        """清空已落盘的原始抽取结果（参数漂移 / 整本重跑时用）。"""
        if model is None:
            rows = self.query(
                "SELECT count(*) FROM extract_raw WHERE book_id=?", [book_id])
            self.execute("DELETE FROM extract_raw WHERE book_id=?", [book_id])
        else:
            rows = self.query(
                "SELECT count(*) FROM extract_raw WHERE book_id=? AND model=?",
                [book_id, model])
            self.execute("DELETE FROM extract_raw WHERE book_id=? AND model=?",
                         [book_id, model])
        return int(rows[0][0]) if rows else 0

    def piece_report(self, book_id: str, model: str, total: int) -> dict:
        """完整性自检：没跑 / 失败 / 为空 / 缺原始结果，各是多少块。"""
        rows = self.query(
            """SELECT piece_idx FROM ingest_progress
               WHERE book_id=? AND model=? AND piece_idx>=0 AND piece_idx<?""",
            [book_id, model, int(total)])
        done = {int(r[0]) for r in rows}
        raw = {i for i in self.raw_pieces(book_id, model) if i < total}
        fails = {i: k for i, k in self.failed_pieces(book_id, model).items()
                 if i < total}
        missing = [i for i in range(total) if i not in done]
        no_raw = [i for i in sorted(done) if i not in raw]
        return {
            "total": total, "done": len(done), "raw": len(raw),
            "missing": missing, "missing_n": len(missing),
            "no_raw": no_raw, "no_raw_n": len(no_raw),
            "empty": [i for i, k in sorted(fails.items()) if k == "empty"],
            "error": [i for i, k in sorted(fails.items()) if k == "error"],
            "fallback": [i for i, k in sorted(fails.items()) if k == "fallback"],
            "empty_n": sum(1 for k in fails.values() if k == "empty"),
            "error_n": sum(1 for k in fails.values() if k == "error"),
            "fallback_n": sum(1 for k in fails.values() if k == "fallback"),
            "coverage": round(len(done) * 100.0 / total, 2) if total else 0.0,
            "complete": (not missing and not no_raw
                         and not any(k == "error" for k in fails.values())),
        }

    # ---------------------------------------------------------- 参数指纹
    def get_run_params(self, book_id: str) -> dict | None:
        rows = self.query(
            "SELECT signature, params FROM book_run_params WHERE book_id=?",
            [book_id])
        if not rows:
            return None
        try:
            params = json.loads(rows[0][1]) if rows[0][1] else {}
        except Exception:
            params = {}
        return {"signature": rows[0][0], "params": params}

    def set_run_params(self, book_id: str, signature: str,
                       params: dict) -> None:
        self.execute(
            """INSERT OR REPLACE INTO book_run_params
               (book_id, signature, params, updated) VALUES (?,?,?,?)""",
            [book_id, signature, _j(params), _now()])

    def set_book_run(self, book_id: str, **fields) -> None:
        """增量更新一本书的构建状态（缺省字段保留原值）。"""
        cur = self.book_run(book_id) or {
            "book_id": book_id, "stage": "pending", "chunks": 0, "pieces": 0,
            "done": 0, "model": "", "mode": "", "proposals": 0,
            "mounted": False, "updated": None}
        cur.update({k: v for k, v in fields.items() if v is not None})
        self.execute(
            """INSERT OR REPLACE INTO book_runs
               (book_id, stage, chunks, pieces, done, model, mode,
                proposals, mounted, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [book_id, cur.get("stage"), int(cur.get("chunks") or 0),
             int(cur.get("pieces") or 0), int(cur.get("done") or 0),
             cur.get("model") or "", cur.get("mode") or "",
             int(cur.get("proposals") or 0), bool(cur.get("mounted")), _now()])

    def book_run(self, book_id: str) -> dict | None:
        rows = self.query(
            """SELECT book_id, stage, chunks, pieces, done, model, mode,
                      proposals, mounted, updated FROM book_runs WHERE book_id=?""",
            [book_id])
        if not rows:
            return None
        return self._run_row(rows[0])

    def all_book_runs(self) -> dict[str, dict]:
        rows = self.query(
            """SELECT book_id, stage, chunks, pieces, done, model, mode,
                      proposals, mounted, updated FROM book_runs""")
        return {r[0]: self._run_row(r) for r in rows}

    @staticmethod
    def _run_row(r: tuple) -> dict:
        return {"book_id": r[0], "stage": r[1], "chunks": r[2],
                "pieces": r[3], "done": r[4], "model": r[5], "mode": r[6],
                "proposals": r[7], "mounted": bool(r[8]),
                "updated": r[9].isoformat() if r[9] else None}


# 单例（与 store 一样在进程内共享；单写者）
warehouse = Warehouse()
