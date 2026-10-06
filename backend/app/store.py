# -*- coding: utf-8 -*-
"""本地本体 + 知识图谱持久化仓储（Kuzu 嵌入式图库）

主存储：backend/app/data/kg/kuzu_db（Kuzu 数据库文件，随仓库版本化）
    节点表：Dynasty / Event / Person / Place / Source / Book
    关系表：KGREL（多源多目标类型化边，rel 属性区分语义：
            参与 / 任职于 / 师承 / 发生于 / 触发 / 奠定 / 书载观点 …）
            关系名一律以 REL_TYPES（app/schema/ontology.py）为准，不留别名写法。

行为：
    首次启动（Kuzu 库不存在）→ 旧 JSON 本体（ontology/）存在则一次性迁入，
    否则从 seed 冷启动；此后始终以 Kuzu 为主存储，
    挂载/卸载（Action Types）写回 Kuzu，重启不丢，新增书籍即增量叠加。
    ontology/*.json 仅作为遗留冷启动种子保留，不再写回。
可视化：Kuzu Explorer（docker run -p 8001:8000 kuzudb/explorer）加载同一库目录。
"""
from __future__ import annotations

import json
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

import kuzu

from app import config
from app.data import seed
from app.extended_books import CANONICAL, EXTENDED, scope_of
from app.warehouse import warehouse
from app.schema.ontology import (
    Book, Dynasty, Event, Extract, Institution, Artifact, Concept,
    Narrative, Person, Place, Source, REL_PAIRS,
)

_DATA_DIR = Path(config.DATA_DIR)
_ONT_DIR = _DATA_DIR / "ontology"          # 遗留 JSON（仅一次性迁移来源）
_KG_DIR = _DATA_DIR / "kg"
_KUZU_DB = _KG_DIR / "kuzu_db"
_LIB_DIR = _DATA_DIR / "library"           # 用户上传书籍区
_LIB_FILES = _LIB_DIR / "files"
_LIB_TRASH = _LIB_DIR / "trash"            # 删除回收区（供 action undo 恢复）
_F_ONTOLOGY_EXTRA = "ontology_extra.json"

_F_DYNASTIES = "dynasties.json"
_F_EVENTS = "events.json"
_F_PEOPLE = "people.json"
_F_PLACES = "places.json"
_F_SOURCES = "sources.json"
_F_BOOKS = "books.json"
_F_EDGES = "edges.json"

# ---------------- Kuzu Schema ----------------
_NODE_DDL = [
    """CREATE NODE TABLE Dynasty(
        id STRING, name STRING, span_s INT64, span_e INT64, color STRING,
        cap_name STRING, cap_lon DOUBLE, cap_lat DOUBLE,
        map_tile STRING, grp STRING, lane INT64, emperors STRING,
        external_ids STRING, provenance STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Event(
        id STRING, name STRING, year INT64, month INT64, day INT64,
        date_conf STRING, dynasty_id STRING, lon DOUBLE, lat DOUBLE,
        place STRING, actors STRING,
        n_student STRING, n_scholar STRING, n_fan STRING,
        significance STRING, dispute STRING,
        route STRING, route_label STRING,
        from_book STRING, book_sources STRING,
        external_ids STRING, provenance STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Person(
        id STRING, name STRING, years STRING, dynasty_id STRING, home STRING,
        lon DOUBLE, lat DOUBLE, aliases STRING,
        n_student STRING, n_scholar STRING, n_fan STRING,
        from_book STRING, book_sources STRING,
        external_ids STRING, provenance STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Place(
        id STRING, name STRING, ancient STRING, modern STRING,
        lon DOUBLE, lat DOUBLE, aliases STRING,
        n_student STRING, n_scholar STRING, n_fan STRING,
        external_ids STRING, provenance STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Source(
        sid STRING, owner STRING, level INT64, work STRING, text STRING,
        translation STRING, confidence DOUBLE, from_book STRING,
        PRIMARY KEY(sid))""",
    """CREATE NODE TABLE Book(
        id STRING, title STRING, author STRING, year INT64, level INT64,
        color STRING, dynasty_id STRING, span STRING, tags STRING,
        descr STRING, extracts STRING, mounted BOOL,
        origin STRING, file STRING, accepted STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Institution(
        id STRING, name STRING, kind STRING, function STRING,
        dynasty_id STRING, span_s INT64, span_e INT64, aliases STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Artifact(
        id STRING, name STRING, kind STRING, material STRING, era_text STRING,
        year INT64, date_conf STRING, place_id STRING, museum STRING,
        aliases STRING, ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Concept(
        id STRING, name STRING, kind STRING, definition STRING,
        dynasty_id STRING, origin_year INT64, aliases STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
]

_REL_DDL = """CREATE REL TABLE KGREL(
    FROM Person TO Event, FROM Person TO Dynasty,
    FROM Person TO Person, FROM Person TO Place, FROM Person TO Book,
    FROM Person TO Institution, FROM Person TO Concept, FROM Person TO Artifact,
    FROM Event TO Place, FROM Event TO Event,
    FROM Event TO Dynasty, FROM Event TO Person, FROM Event TO Book,
    FROM Event TO Concept,
    FROM Book TO Event, FROM Book TO Person,
    FROM Book TO Place, FROM Book TO Dynasty, FROM Book TO Book,
    FROM Book TO Institution, FROM Book TO Artifact, FROM Book TO Concept,
    FROM Dynasty TO Place, FROM Dynasty TO Dynasty,
    FROM Dynasty TO Event, FROM Dynasty TO Person, FROM Dynasty TO Concept,
    FROM Place TO Place, FROM Place TO Event,
    FROM Institution TO Institution, FROM Institution TO Dynasty,
    FROM Institution TO Place, FROM Institution TO Person,
    FROM Artifact TO Place, FROM Artifact TO Dynasty,
    FROM Artifact TO Source, FROM Artifact TO Institution,
    FROM Concept TO Dynasty, FROM Concept TO Concept,
    FROM Concept TO Person, FROM Concept TO Event,
    rel STRING, from_book STRING, confidence DOUBLE, src_ids STRING)"""

# 注：Chunk 已迁出 Kuzu（真值源为 DuckDB warehouse，见 docs/PLAN.md §2）
_ALL_TABLES = ["KGREL", "Source", "Concept", "Artifact", "Institution",
               "Book", "Dynasty", "Event", "Person", "Place"]

# 边端点 id → 节点表名（写入 KGREL 时需要具体标签）
_NODE_TABLES = ("Dynasty", "Event", "Person", "Place", "Book")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _as_int(v) -> int | None:
    """年份字符串转整数：形如 '-259'、'?-259'（史学中常见的存疑标记）。"""
    if v is None:
        return None
    try:
        return int(str(v).lstrip("?").strip())
    except Exception:
        return None


# 单条连接写满这么多行就重开一次库：不重开缓冲池的脏页释放不掉，
# 会在大书挂载时抛 "buffer pool is full"（见 _run_batch 注释）
_REOPEN_EVERY = 2000

# 提案条数超过这个数就不再内联进 Book 节点，改由产物文件承载
# （见 _render 里 extracts 的注释）
_INLINE_EXTRACTS_MAX = 300


def _artifact_proposals(book_id: str) -> list[dict]:
    """从离线产物 data/ingest/<id>.json 读回提案。"""
    p = _DATA_DIR / "ingest" / f"{book_id}.json"
    try:
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    for key in ("proposals", "extracts"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


def _strip_node(d: dict) -> dict:
    d.pop("_id", None)
    d.pop("_label", None)
    return d


def _loads_or(s: str | None, default):
    """json.loads 的容错版：解析不出来就返回默认值。

    Kuzu 没有显式事务，进程被杀/写崩时某一列可能留下半截字符串；这类坏数据
    不该把整个服务拖得起不来（实测：一本的 extracts 写坏 → 服务启动即崩，
    全套接口全挂）。丢一份可重算的提案，远比全站不可用划算。
    """
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        print(f"[warn] 坏 JSON 已按默认值处理（长度 {len(s)}）", flush=True)
        return default


class Store:
    def __init__(self) -> None:
        self.dynasties: list[Dynasty] = []
        self.events: list[Event] = []
        self.people: list[Person] = []
        self.places: list[Place] = []
        self.institutions: list[Institution] = []
        self.artifacts: list[Artifact] = []
        self.concepts: list[Concept] = []
        self.sources: dict[str, list[Source]] = {}
        self.books: list[Book] = []
        self.edges: list[dict] = []
        self.maps_catalog: dict = {}
        # 书籍切块：book_id -> [{idx, text, embedding}]
        self.chunks: dict[str, list[dict]] = {}

        _KG_DIR.mkdir(parents=True, exist_ok=True)
        _LIB_FILES.mkdir(parents=True, exist_ok=True)
        _LIB_TRASH.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # save 的整体临界区，杜绝并发 save 交叉破坏
        self._save_lock = threading.RLock()
        # 增量落盘快照：记录「上次写进磁盘的内容」，save 时据此做差异。
        # 只存**行指纹**（64 位 int），不存整行——整行快照实测一本书就要
        # 173.8 MB（节点）+ 75.8 MB（边），26 本线性外推 6 GB 以上；存指纹约 20 MB。
        # 节点：表名 → {主键: 指纹}；边：{(源标签, 目标标签, a, b, rel, fb): 指纹}
        self._kz_snap: dict[str, dict[str, int]] = {}
        self._kz_edge_snap: dict[tuple, int] = {}
        # 书 → 该书产生的节点 id（卸载时按书整批清快照用；指纹里查不到 fb 了）
        self._kz_node_book: dict[str, set] = {}
        # 表名 → (主键字段, CREATE 语句)：某表被清空时仍要能算出删除语句
        self._kz_node_sql: dict[str, tuple] = {}
        self._pending_dangling: list[dict] = []
        self._save_stats: dict[str, tuple[int, int, int]] = {}
        # 本次 save 的差异明细（供 DuckDB 副本增量同步用）
        self._last_diff: dict = {}
        # _purge_book_disk 删掉的实体 id：这些行已不在 Kuzu 里，因此不会进入
        # diff 明细，但仍要把 DuckDB 副本里的同 id 记录一并清掉（见 save()）
        self._purge_buffer: list[str] = []
        # buffer_pool_size 必须显式给：默认按机器内存比例分配，挂载大书会
        # bad allocation（见 config.KUZU_BUFFER_POOL_GB 注释）。
        self._db = kuzu.Database(
            str(_KUZU_DB),
            buffer_pool_size=int(config.KUZU_BUFFER_POOL_GB * 1024 ** 3))
        self._conn = kuzu.Connection(self._db)

        if self._kuzu_ready():
            self._load_kuzu()
            # 内存此刻等价于磁盘 → 快照对齐：下一次 save 零写入，不再整库重写
            self._build_snapshot()
            # 切块真值源迁移：旧库 Chunk 表数据一次性搬入 DuckDB
            self._sync_chunks_to_warehouse()
            # 实体副本同步到 DuckDB（查询用加速副本）：副本为空才全量重建，
            # 否则按差异增量同步，避免每次启动都全表重灌（实测 117s）
            try:
                self._sync_entities_to_warehouse(full=warehouse.entity_count() == 0)
            except Exception:
                self._sync_entities_to_warehouse(full=True)
            need_rebuild = False
            # 本体深化：旧库无 Institution 表，或三类扩展实体全空
            # （表已建但迁移未落数据/被中断）→ 重新播种样例后整体重建。
            # 以「三类全空」为条件而非单类，避免用户删空某一类后被反复重播。
            if not self._has_table("Institution") or not (
                    self.institutions or self.artifacts or self.concepts):
                self._load_ontology_extra()
                need_rebuild = True
            if need_rebuild:
                self.save()
        else:
            # 一次性迁移：旧 JSON 本体优先，否则 seed 冷启动
            if (_ONT_DIR / _F_DYNASTIES).exists():
                self._load_json()
            else:
                self._bootstrap()
            self.save()

        maps_path = _DATA_DIR / "maps.json"
        if maps_path.exists():
            self.maps_catalog = json.loads(maps_path.read_text(encoding="utf-8"))

    # ================= Kuzu 初始化 =================
    def _x(self, query: str, params: dict | None = None):
        """Kuzu 连接非线程安全，FastAPI 线程池下统一串行化。"""
        with self._lock:
            return self._conn.execute(query, params) if params else self._conn.execute(query)

    def _table_names(self) -> set[str]:
        """Kuzu 表名集合（对节点表与关系表都有效）。

        不能用 `MATCH (n:X)` 探测：关系表不是节点模式，会永远返回 False，
        进而误判"库未就绪"→ 触发 JSON 冷启动 → DROP 全库导致数据丢失。
        """
        try:
            r = self._x("CALL show_tables() RETURN name")
            names: set[str] = set()
            while r.has_next():
                names.add(r.get_next()[0])
            return names
        except Exception:
            return set()

    def _kuzu_ready(self) -> bool:
        """只要核心节点表存在就认为库已就绪（空库也按"已就绪"处理）。"""
        return bool(self._table_names() & {"Dynasty", "Event", "Person"})

    def _has_table(self, name: str) -> bool:
        return name in self._table_names()

    def _refuse_wipe(self) -> None:
        """数据丢失保险：内存为空、但图库还有数据时，拒绝重建（DROP 全表）。

        曾因 `_kuzu_ready()` 误判，用空/残缺内存覆盖过整个图库。
        """
        if self.events or self.people or self.dynasties or self.books:
            return
        try:
            r = self._x("MATCH (d:Dynasty) RETURN count(d)")
            n = r.get_next()[0] if r.has_next() else 0
        except Exception:
            n = 0
        if n:
            raise RuntimeError(
                f"拒绝落库：内存为空但图库已有 {n} 个朝代节点（重建会清空数据）")

    def _ensure_schema(self) -> None:
        """确保 schema 就位；**不再 DROP 表**。

        旧实现是「每次 save 都 DROP 全部表 + 全量重写」：写入量随书架线性累加
        （第 N 本书挂载要重写前 N 本的全部节点），且每次都产生整库废页——这是
        挂载大书时 Kuzu `bad allocation`（峰值 17.7GB）与库膨胀的根因。
        改为增量写后，这里只在表缺失时补建。
        """
        self._refuse_wipe()
        existing = self._table_names()
        if not (existing & {"Dynasty", "Event", "Person"}):     # 全新/空库
            for stmt in _NODE_DDL:
                self._x(stmt)
            self._x(_REL_DDL)
            self._kz_snap, self._kz_edge_snap = {}, {}
            self._kz_node_book, self._kz_node_sql = {}, {}
            return
        if "KGREL" not in existing:
            self._x(_REL_DDL)
        for stmt in _NODE_DDL:
            name = stmt.split("(")[0].replace("CREATE NODE TABLE", "").strip()
            if name not in existing:
                self._x(stmt)
                self._kz_snap.pop(name, None)   # 新表：磁盘无数据 → 整表写入

    # ---------------- 增量落盘（diff-based） ----------------
    # 执行顺序很讲究：**先删边 → 再删节点 → 再写节点 → 最后写边**。
    # 边先删掉之后，节点删除就不必级联扫描它的全部关系——DETACH DELETE 在
    # 上万条边上做级联时，实测会把进程顶到被原生层直接终止。

    def _run_batch(self, sql: str, params_list: list[dict],
                   batch: int = 300) -> int:
        """分批执行同一条语句。

        Kuzu 0.11 的 Python API **没有显式事务**（Connection 只有 execute），
        所以这里做不到「每批一个事务」；`CHECKPOINT` 语句也实测对内存没有帮助
        （默认 checkpoint_threshold=-1，且周期性 CHECKPOINT 在对照组里无效）。
        分批仅用于把一次性超大规模的提交拆成多轮，降低瞬时压力。

        真正能回收内存的是**重开库**：一条连接攒下上万次更新后，缓冲池的脏页
        一个都释放不掉，会直接抛
        `Buffer manager exception: Unable to allocate memory`（《读通鉴论》
        挂载 1.6 万条提案时必现，加大缓冲池只能推迟、不能消除）。所以写够
        一定行数就重开一次库，让 Kuzu 落盘并释放缓冲池。
        """
        n = 0
        big = len(params_list) >= _REOPEN_EVERY * 2
        for i in range(0, len(params_list), batch):
            for p in params_list[i:i + batch]:
                self._x(sql, p)
                n += 1
                if big and n % _REOPEN_EVERY == 0:
                    self._reset_kuzu_connection()
        return n

    @staticmethod
    def _update_sql(create_sql: str, label: str, key_field: str) -> str:
        """由 CREATE 语句派生出原地更新语句（`SET` 各属性）。

        为什么更新不能「删后重建」：DETACH DELETE 会遍历该节点的整条邻接表，
        对刚被整批删掉上万条关系的节点做这一步，实测会把 Kuzu 原生层直接打死
        （《资治通鉴》卸载时正是卡在 Book 节点的 DETACH DELETE 上）。SET 只改
        属性、不碰邻接表，语义等价且没有级联代价。
        """
        body = create_sql[create_sql.index("{") + 1: create_sql.rindex("}")]
        clause = []
        for seg in body.split(","):
            prop, _, param = seg.partition(":")
            prop, param = prop.strip(), param.strip()
            # 注意属性名与参数名不等价（如 span_s:$ss），须原样沿用 CREATE 里的参数名；
            # 参数也不会多传——Kuzu 把多余参数直接报 "Parameter X not found"
            if prop and prop != key_field:
                clause.append("n.%s=%s" % (prop, param))
        return f"MATCH (n:{label} {{{key_field}:$k}}) SET " + ", ".join(clause)

    @staticmethod
    def _row_fp(row: dict) -> int:
        """行内容指纹：只用来判断「这行相对上次落盘变没变」。

        快照必须留住全图，所以它存什么直接决定常驻内存的量级：存整行时
        《资治通鉴》一本书就是 173.8 MB（节点）+ 75.8 MB（边），26 本书线性
        外推 6 GB 以上。改成存 64 位指纹后降到约 20 MB，代价是理论上有
        n²/2^64 的碰撞概率（十万行量级约 5e-10），可接受——碰撞最坏也只是
        漏写一次属性更新，下一次 save 内容变了会重算。
        """
        return hash(json.dumps(row, sort_keys=True, ensure_ascii=False,
                               default=str))

    def _walk_rows(self, on_node, on_edge) -> list[dict]:
        """逐行渲染当前内存状态并回调，**不留存语句列表**。

        旧实现先录 (sql, params) 再分组，等于把整图多留一份（实测 13.3 万条
        语句 252 MB），而且这个量随图增长——挂第 N 本书时的瞬时内存按全图算，
        不只是本书的提案。这里改成回调，由调用方决定留什么（差分只留变动行）。
        """
        saved = self._x

        def rec(sql, params=None):
            params = params or {}
            if sql.startswith("CREATE (n:"):
                label = sql[len("CREATE (n:"):].split(" ")[0]
                on_node(label, "sid" if label == "Source" else "id", sql, params)
            else:                              # MATCH(a),(b) CREATE (a)-[..]->(b)
                la = sql.split("(a:")[1].split(" ")[0]
                lb = sql.split("(b:")[1].split(" ")[0]
                on_edge(la, lb, params)
            return None

        self._x = rec
        try:
            self._emit_statements()
        finally:
            self._x = saved
        return self._pending_dangling

    def _build_snapshot(self) -> None:
        """把快照对齐到当前内存（从磁盘加载完之后调用，使下次 save 零写入）。"""
        snap_n: dict[str, dict[str, int]] = {}
        snap_e: dict[tuple, int] = {}
        by_book: dict[str, set] = {}

        def on_node(label, key_field, create_sql, params):
            snap_n.setdefault(label, {})[params[key_field]] = self._row_fp(params)
            self._kz_node_sql[label] = (key_field, create_sql)
            if params.get("fb"):
                by_book.setdefault(str(params["fb"]), set()).add(params[key_field])

        def on_edge(la, lb, params):
            snap_e[(la, lb, params["a"], params["b"], params["rel"],
                    params["fb"])] = self._row_fp(params)

        self._walk_rows(on_node, on_edge)
        self._kz_snap, self._kz_edge_snap = snap_n, snap_e
        self._kz_node_book = by_book

    def _collect_plans(self) -> tuple[list[dict], list[dict], dict, dict,
                                      dict, list[dict]]:
        """边渲染边算差异：**只为发生变化的行保留 params**。

        返回 (节点计划, 边计划, 新节点快照, 新边快照, 书→节点 id, 悬空边)。
        计划里不再带整表 rows——那是旧实现内存翻倍的地方。
        """
        old_n, old_e = self._kz_snap, self._kz_edge_snap
        snap_n: dict[str, dict[str, int]] = {}
        snap_e: dict[tuple, int] = {}
        by_book: dict[str, set] = {}
        plans_n: dict[str, dict] = {}
        plans_e: dict[tuple, dict] = {}

        def plan_node(label: str, key_field: str, create_sql: str) -> dict:
            return plans_n.setdefault(label, {
                "label": label,
                "del_sql": f"MATCH (n:{label} {{{key_field}:$k}}) DETACH DELETE n",
                "create_sql": create_sql,
                "update_sql": (self._update_sql(create_sql, label, key_field)
                               if create_sql else ""),
                "del_params": [], "write_rows": [], "upd_rows": [],
                "changed": [], "stats": [0, 0, 0]})

        def plan_edge(la: str, lb: str) -> dict:
            create_sql, delete_sql = self._edge_sqls(la, lb)
            return plans_e.setdefault((la, lb), {
                "create_sql": create_sql, "delete_sql": delete_sql,
                "del_params": [], "write_rows": [], "stats": [0, 0, 0]})

        def on_node(label, key_field, create_sql, params):
            key = params[key_field]
            fp = self._row_fp(params)
            snap_n.setdefault(label, {})[key] = fp
            self._kz_node_sql[label] = (key_field, create_sql)
            if params.get("fb"):
                by_book.setdefault(str(params["fb"]), set()).add(key)
            prev = old_n.get(label, {}).get(key)
            if prev == fp:
                return                          # 未变动：不写、也不进 DuckDB 投影
            p = plan_node(label, key_field, create_sql)
            if prev is not None:                # 变更：原地 SET 覆盖各属性
                # 只传语句真正引用的参数：Kuzu 会把多余参数当作
                # "Parameter X not found"（SET 里不含主键，故剔除）
                p["upd_rows"].append({**{f: v for f, v in params.items()
                                         if f != key_field}, "k": key})
                p["stats"][1] += 1
            else:
                p["write_rows"].append(params)
                p["stats"][0] += 1
            p["changed"].append(key)

        def on_edge(la, lb, params):
            key = (la, lb, params["a"], params["b"], params["rel"], params["fb"])
            fp = self._row_fp(params)
            snap_e[key] = fp
            prev = old_e.get(key)
            if prev == fp:
                return
            p = plan_edge(la, lb)
            if prev is not None:                # 变更：删后重建（边没有 SET 可用）
                p["del_params"].append({"a": params["a"], "b": params["b"],
                                        "rel": params["rel"], "fb": params["fb"]})
                p["stats"][1] += 1
            else:
                p["stats"][0] += 1
            p["write_rows"].append({k: v for k, v in params.items()
                                    if k not in ("la", "lb")})

        dangling = self._walk_rows(on_node, on_edge)

        # 只在旧快照里、当前内存已没有的行 → 删除
        diff = self._last_diff.setdefault("nodes", {"changed": [], "removed": []})
        for label, old_rows in old_n.items():
            missing = old_rows.keys() - snap_n.get(label, {}).keys()
            if not missing:
                continue
            key_field, create_sql = self._kz_node_sql.get(
                label, ("sid" if label == "Source" else "id", ""))
            p = plan_node(label, key_field, create_sql)
            p["del_params"].extend({"k": k} for k in missing)
            p["stats"][2] += len(missing)
            diff["removed"].extend(missing)
        for key in old_e.keys() - snap_e.keys():
            la, lb, a, b, rel, fb = key
            p = plan_edge(la, lb)
            p["del_params"].append({"a": a, "b": b, "rel": rel, "fb": fb})
            p["stats"][2] += 1
        for p in plans_n.values():
            diff["changed"].extend(p["changed"])

        return (list(plans_n.values()), list(plans_e.values()),
                snap_n, snap_e, by_book, dangling)

    @staticmethod
    def _edge_sqls(la: str, lb: str) -> tuple[str, str]:
        """生成某组（源标签, 目标标签）的 (CREATE, DELETE) 语句。"""
        ka = "sid" if la == "Source" else "id"
        kb = "sid" if lb == "Source" else "id"
        head = f"MATCH (a:{la} {{{ka}:$a}}), (b:{lb} {{{kb}:$b}}) "
        create = (head + "CREATE (a)-[:KGREL {rel:$rel, from_book:$fb,"
                  " confidence:$conf, src_ids:$sids}]->(b)")
        delete = (head + "MATCH (a)-[r:KGREL {rel:$rel, from_book:$fb}]->(b)"
                  " DELETE r")
        return create, delete

    def _apply_plans(self, node_plans: list[dict], edge_plans: list[dict]) -> dict:
        """按 删边 → 删节点 → 写新节点 → 更新存量节点 → 写边 的顺序落盘。"""
        for plan in edge_plans:
            self._run_batch(plan["delete_sql"], plan["del_params"])
        for plan in node_plans:
            self._run_batch(plan["del_sql"], plan["del_params"])
        for plan in node_plans:
            self._write_nodes(plan)
        for plan in node_plans:
            self._run_batch(plan["update_sql"], plan["upd_rows"])
        for plan in edge_plans:
            self._run_batch(plan["create_sql"], plan["write_rows"])
        stats: dict[str, tuple[int, int, int]] = {
            p["label"]: p["stats"] for p in node_plans}
        stats["KGREL"] = tuple(sum(p["stats"][i] for p in edge_plans)
                               for i in range(3))
        return stats

    def _write_nodes(self, plan: dict) -> None:
        """写新节点；撞主键时改走原地 SET，而不是让整次 save 失败。

        Kuzu 0.11 没有显式事务，一次中断（缓冲池满、进程被杀）会在磁盘留下
        半截数据；下一次 save 对同一批行再 CREATE 就会
        `Found duplicated primary key`，于是**每一本后续的书都被这个残行卡死**
        （实测《读通鉴论》写一半失败后，新唐书、旧五代史接连报同一个主键）。
        撞主键说明行已在盘上，语义上等价于覆盖，改写成 SET 即可自愈。
        """
        try:
            self._run_batch(plan["create_sql"], plan["write_rows"])
        except Exception as exc:
            if "duplicated primary key" not in str(exc).lower():
                raise
            key_field = plan.get("key_field") or (
                "sid" if plan["label"] == "Source" else "id")
            print(f"[warn] {plan['label']} 撞主键（上次写了半截），"
                  f"改为原地更新 {len(plan['write_rows'])} 行", flush=True)
            for row in plan["write_rows"]:
                upd = {k: v for k, v in row.items() if k != key_field}
                upd["k"] = row[key_field]
                try:
                    self._x(plan["update_sql"], upd)
                except Exception:
                    continue          # 单行失败不影响其余行，下次 save 会重算

    def _reset_kuzu_connection(self) -> None:
        """丢弃并重开 Kuzu 连接——批量删除之后的必要动作。

        执行过万级批量删除（`MATCH ()-[r:KGREL {from_book:$fb}]->() DELETE r`
        与按 `from_book` 的 DETACH DELETE）的那条连接会被「毒化」：立刻生效不了，
        但紧接着再扫一次 KGREL 表就会把整个进程打死（无异常、无堆栈，原生层直接
        终止，实测必现）。另一个进程打开同一份库则完全正常，说明磁盘数据没坏、
        坏的只是这条连接的内存态。

        所以批量删除之后不能在同一条连接上继续干活，必须关掉重开。实测重开后
        聚合查询 / SET 改属性 / 建删节点 / 顺序扫描全部正常。
        """
        with self._lock:
            self._conn = None
            self._db = None
            self._db = kuzu.Database(
                str(_KUZU_DB),
                buffer_pool_size=int(config.KUZU_BUFFER_POOL_GB * 1024 ** 3))
            self._conn = kuzu.Connection(self._db)

    def _purge_book_disk(self, book_id: str) -> list[str]:
        """磁盘侧整书清除：不依赖内存状态，按 `from_book` 属性一条语句清一批。

        为什么卸载不能走通用 diff：通用 diff 要为这本书的每条节点与边各发一条
        语句（2944+16145 节点 + 44446 条边 ≈ 9 万次 round-trip）。实测要把进程
        顶到 3.6GB、耗时 8 分钟以上；而这本书产生的行必定带 `from_book`，一条
        `MATCH (n:X {from_book:$fb}) DETACH DELETE n` 就能整批清掉，**实测 2.7 秒**。

        要点：只删「快照里行数变化必需」的标签（row 含 fb 键），因此对没有
        from_book 属性的表（Dynasty / Place / Book …）不会误发语句；命中为空
        的表也不会产生任何写入。同时把快照里属于该书的记录一并摘掉，使后续
        save() 对这些行 diff 为零。

        返回被删实体 id，调用方负责把它们推进 DuckDB 副本清理队列。
        """
        self._x("MATCH ()-[r:KGREL {from_book:$fb}]->() DELETE r", {"fb": book_id})
        # 边的 from_book 就在快照 key 里（key 第 6 位），指纹里查不到也无妨
        self._kz_edge_snap = {k: v for k, v in self._kz_edge_snap.items()
                              if k[5] != book_id}
        removed: list[str] = []
        # 节点归属改由 _kz_node_book 索引提供：快照只剩指纹，没有 fb 可查了
        pool = self._kz_node_book.pop(book_id, set())
        for label, rows in self._kz_snap.items():
            hits = [k for k in rows.keys() if k in pool]
            if not hits:
                continue
            self._x(f"MATCH (n:{label} {{from_book:$fb}}) DETACH DELETE n",
                    {"fb": book_id})
            for k in hits:
                rows.pop(k, None)
            removed.extend(hits)
        return removed

    # ================= 遗留 JSON（一次性迁移来源） =================
    def _load_json(self) -> None:
        def read(name):
            return json.loads((_ONT_DIR / name).read_text(encoding="utf-8"))

        self.dynasties = [Dynasty.model_validate(x) for x in read(_F_DYNASTIES)]
        self.events = [Event.model_validate(x) for x in read(_F_EVENTS)]
        self.people = [Person.model_validate(x) for x in read(_F_PEOPLE)]
        self.places = [Place.model_validate(x) for x in read(_F_PLACES)]
        self.books = [Book.model_validate(x) for x in read(_F_BOOKS)]
        self.edges = read(_F_EDGES)
        self.sources = {
            k: [Source.model_validate(s) for s in v]
            for k, v in read(_F_SOURCES).items()
        }
        self._apply_scopes()
        self._index_invalidate()

    def _bootstrap(self) -> None:
        """冷启动：从内置 seed 把本体节点与类型化边写入持久层。"""
        self.dynasties = list(seed.DYNASTIES)
        self.events = list(seed.EVENTS)
        self.people = list(seed.PEOPLE)
        self.places = list(seed.PLACES)
        self.sources = dict(seed.SOURCES)
        self.books = list(seed.BOOKS)
        self.edges = [
            {"a": a, "b": b, "rel": rel, "from_book": None,
             "confidence": 0.8, "src_ids": []}
            for a, b, rel in seed.EDGES
        ]
        self._load_ontology_extra()
        self._apply_scopes()
        self._index_invalidate()

    def _apply_scopes(self) -> None:
        """给书目打分层标记：canonical（主干）/ extended（策展，禁挂载）。

        scope 不落 Kuzu（0.11 不支持 ALTER ADD COLUMN），凡是书籍列表被整体替换
        或新增书的地方都要记得调一次——漏了就会退化成 canonical，进而允许挂载，
        目前四个入口（载入 / 冷启动 seed / 遗留 JSON / 回收站恢复）都已调。

        顺带补齐策展书的内联提案：主干书的提案有 ingest 产物文件兜底（那一列被
        写成 [] 也能回落读文件），**策展书没有产物文件**，提案只存在这一列里。
        历史上它一旦被写成 []，seed 里的手工提案就永久丢了，前端只会显示 0 张
        卡片——而这两层书永远不挂载、不会再有新的抽取结果。所以这里以 seed 为
        基线补齐内存（为空才补，不覆盖库里已有的内容）。
        """
        seed_map = {b.id: b for b in seed.BOOKS}
        for b in self.books:
            b.scope = scope_of(b.id)
            if b.scope == EXTENDED and not b.extracts:
                base = seed_map.get(b.id)
                if base and base.extracts:
                    b.extracts = [x.model_copy(deep=True) for x in base.extracts]

    def _load_ontology_extra(self) -> None:
        """本体深化样例：Institution / Artifact / Concept 及类型化边（幂等）。"""
        p = _DATA_DIR / _F_ONTOLOGY_EXTRA
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        self.institutions = [Institution.model_validate(x)
                             for x in data.get("institutions", [])]
        self.artifacts = [Artifact.model_validate(x)
                          for x in data.get("artifacts", [])]
        self.concepts = [Concept.model_validate(x)
                         for x in data.get("concepts", [])]
        known = set(self._label_index())
        for row in data.get("edges", []):
            a, b, rel = row[0], row[1], row[2]
            conf = row[3] if len(row) > 3 else 0.8
            sids = row[4] if len(row) > 4 else []
            # 端点缺失的种子边跳过（不造悬空数据）
            if a not in known or b not in known:
                continue
            self.edges.append({"a": a, "b": b, "rel": rel, "from_book": None,
                               "confidence": conf, "src_ids": sids})

    # ================= 边写入与目录校验 =================
    def _label_index(self) -> dict[str, str]:
        """id → 标签表的索引（**带缓存**，勿在循环里反复重建）。

        add_edge 每写一条边都要查端点标签，而挂载一本书会写几万条边；若每条边
        都重建一个近 9 万条目的 dict，既慢又吃内存——宋史 49069 条提案挂载时就是
        这样 MemoryError 的（实测爆在 `idx[o.id] = table`，系统 31 GB 内存、
        可用 12 GB 仍不够）。所以这里缓存 + 实体增删时增量维护（_index_put）；
        万一漏登记，add_edge 查不到时会兜底重建一次，不会误判端点不存在。
        """
        idx = getattr(self, "_label_index_cache", None)
        if idx is None:
            idx = self._rebuild_label_index()
            self._label_index_cache = idx
        return idx

    def _rebuild_label_index(self) -> dict[str, str]:
        idx = {}
        for table, objs in (("Dynasty", self.dynasties), ("Event", self.events),
                            ("Person", self.people), ("Place", self.places),
                            ("Book", self.books), ("Institution", self.institutions),
                            ("Artifact", self.artifacts), ("Concept", self.concepts)):
            for o in objs:
                idx[o.id] = table
        for owner, srcs in self.sources.items():
            for i in range(len(srcs)):
                idx[f"{owner}#{i}"] = "Source"
        return idx

    def _index_put(self, oid: str, table: str) -> None:
        """新增实体登记进索引：否则下一条边就会触发整表重建。"""
        self._label_index()[oid] = table

    def _index_invalidate(self) -> None:
        """实体列表被整体替换或批量删除后调用：缓存不可再用。"""
        self._label_index_cache = None

    def add_edge(self, a: str, b: str, rel: str, *,
                 confidence: float = 0.7, src_ids: list[str] | None = None,
                 from_book: str | None = None) -> dict:
        """写边唯一入口：端点必须存在，且端点组合落在 REL_PAIRS 目录内。"""
        labels = self._label_index()
        la, lb = labels.get(a), labels.get(b)
        if not la or not lb:
            # 缓存滞后（实体刚加入但没登记）：重建一次再判定，代价只此一次
            self._index_invalidate()
            labels = self._label_index()
            la, lb = labels.get(a), labels.get(b)
        if not la or not lb:
            raise ValueError(f"端点不存在：{a} / {b}")
        if (la, lb) not in REL_PAIRS:
            raise ValueError(f"非法端点组合（不在关系目录内）：{la} → {lb}")
        edge = {"a": a, "b": b, "rel": rel, "from_book": from_book,
                "confidence": confidence, "src_ids": src_ids or []}
        self.edges.append(edge)
        return edge

    @contextmanager
    def transaction(self):
        """写事务临界区：把「内存变更 + 落盘」包成一个整体。

        save() 是 DROP + 全量重建，若与任意并发写交叉会互相破坏（A 建表途中
        B 再 DROP）；因此所有 Action Type 写路径（挂载/卸载/删除/恢复）都应在
        该临界区内完成「改内存 → save()」。_save_lock 为可重入锁，区内调用
        save() 不会自锁。
        """
        with self._save_lock:
            yield

    # ================= Kuzu 读写 =================
    def save(self) -> None:
        """增量落盘：只把发生变化的部分写入 Kuzu，再同步实体副本到 DuckDB。

        旧实现是 DROP 全表 + 全量重写，写入量随书架累加；现已改为差分写
        （详见 _save_locked），Kuzu 副本与 DuckDB 副本都只动变化的那部分。

        _purge_book_disk 删掉的行不在 diff 明细里（它在 diff 之前执行），因此
        需要单独带进来清 DuckDB 副本——否则卸载后实体副本会残留孤岛记录。
        """
        carry = list(self._purge_buffer)
        self._purge_buffer.clear()
        self._last_diff = {}
        with self._save_lock:
            self._save_locked()
        if carry:
            diff = self._last_diff.setdefault("nodes", {"changed": [], "removed": []})
            diff["removed"] = list(dict.fromkeys(diff.get("removed", []) + carry))
        self._sync_entities_to_warehouse()

    def _entity_rows(self) -> list[dict]:
        """构造实体投影行（真值源仍是 Kuzu，此处只是给查询加速的副本）。"""
        rows: list[dict] = []
        for e in self.events:
            rows.append({"entity_id": e.id, "kind": "event", "name": e.name,
                         "year": e.year, "dynasty_id": e.dynasty_id,
                         "place_id": None, "lon": e.lon, "lat": e.lat,
                         "from_book": e.from_book, "confidence": None})
        for p in self.people:
            rows.append({"entity_id": p.id, "kind": "person", "name": p.name,
                         "year": _as_int(p.years[0] if p.years else None),
                         "dynasty_id": p.dynasty_id, "place_id": None,
                         "lon": p.lon, "lat": p.lat,
                         "from_book": p.from_book, "confidence": None})
        for pl in self.places:
            rows.append({"entity_id": pl.id, "kind": "place", "name": pl.name,
                         "year": None, "dynasty_id": None, "place_id": pl.id,
                         "lon": pl.lon, "lat": pl.lat,
                         "from_book": None, "confidence": None})
        return rows

    def _sync_entities_to_warehouse(self, full: bool = False) -> int:
        """把实体投影同步到 DuckDB（可随时重建的分析副本）。

        默认按本次落盘差异增量同步：挂载/卸载一本书只动它自己的实体。
        旧版是每次 save 都 DELETE + INSERT 全表，实测 **117.6 秒/次**，
        既是 save 的性能瓶颈，也是服务启动慢的一大来源。

        失败不影响主流程——数仓只是查询副本，丢失可 full=True 重建。
        """
        try:
            rows_all = self._entity_rows() if full else []
            if full:
                # 图里有实体却算出空行（说明还没载入完成），就不要往下做：
                # 此时重灌只会把副本擦干净，比不同步更糟。
                if not rows_all and (self.events or self.people):
                    print("[warn] 实体副本全量重灌被跳过：图库有实体但 _entity_rows() 为空",
                          flush=True)
                    return 0
                return warehouse.replace_entities(rows_all)
            diff = (self._last_diff or {}).get("nodes") or {}
            changed, removed = diff.get("changed") or [], diff.get("removed") or []
            if not changed and not removed:
                return 0                       # 本次无实体变化 → 零成本
            wanted = set(changed)
            rows = [r for r in self._entity_rows() if r["entity_id"] in wanted]
            warehouse.upsert_entities(rows)
            if removed:
                warehouse.delete_entities(removed)
            return len(rows)
        except Exception as exc:        # 副本丢失可重建，但不能悄悄丢
            # 这里曾经静默 return 0，导致 replace_entities 先删后插失败时
            # （整个 entities 表被清空）没人知道，只剩一个空表在对外提供查询。
            print("[warn] 实体副本同步失败：%s" % exc, flush=True)
            traceback.print_exc()
            return 0

    def _save_locked(self) -> None:
        """增量落盘：内存与「上次落盘快照」做差，只写发生变化的部分。

        对比旧版「DROP 全表 + 全量重写」：写入量不再随书架累加——第 N 本书
        挂载时只写它自己的提案，而不是前 N 本书的全部节点与边。这正是挂载
        《资治通鉴》44447 条把进程顶到 17.7GB 的根因。

        内存上也不再留整行：渲染与差分一次走完（`_collect_plans`），未变动的
        行连 params 都不留，快照只存指纹。
        """
        self._ensure_schema()
        (node_plans, edge_plans, snap_n, snap_e, by_book,
         dangling) = self._collect_plans()
        stats = self._apply_plans(node_plans, edge_plans)
        # 大批量写入之后重开连接：写过上万条边的那条连接会被「毒化」，紧接着
        # 再扫一次 KGREL 表就会把整个进程打死（无异常、无堆栈，原生层直接终止）。
        # 批量构建时每本书收尾都要 save() 一遍存提案，等于挂载后紧接着全图扫描
        # ——实测必崩，所以这里和卸载一样重开连接。
        if stats.get("KGREL", (0, 0, 0))[0] >= 500:
            self._reset_kuzu_connection()
        # 全部写成功后才推进快照：中途抛异常时快照保持不变，下一次能重算同样的差异
        self._kz_snap, self._kz_edge_snap, self._kz_node_book = (
            snap_n, snap_e, by_book)
        (_KG_DIR / "dangling_edges.json").write_text(_j(dangling), encoding="utf-8")
        self._save_stats = stats        # 排错用：每张表 (新增, 变更, 删除)

    def _emit_statements(self) -> None:
        """把当前内存状态渲染成写库语句——**不真写库**。

        执行时 self._x 已被换成记录器（见 _render），所有字段构造逻辑与原来
        逐条写库的版本完全一致，因此这里不需要re改动就能切换到增量落盘。
        """

        for d in self.dynasties:
            self._x(
                "CREATE (n:Dynasty {id:$id, name:$name, span_s:$ss, span_e:$se,"
                " color:$color, cap_name:$cn, cap_lon:$clon, cap_lat:$clat,"
                " map_tile:$mt, grp:$grp, lane:$lane, emperors:$emp,"
                " external_ids:$ext, provenance:$prov,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": d.id, "name": d.name, "ss": d.span[0], "se": d.span[1],
                 "color": d.color, "cn": d.capital[0], "clon": d.capital[1],
                 "clat": d.capital[2], "mt": d.map_tile,
                 "grp": getattr(d, "group", None),
                 "lane": getattr(d, "lane", 0), "emp": _j(d.emperors),
                 "ext": _j(getattr(d, "external_ids", {}) or {}),
                 "prov": getattr(d, "provenance", None) or "seed",
                 "ca": d.created_at, "ua": d.updated_at, "ver": d.version})

        for e in self.events:
            self._x(
                "CREATE (n:Event {id:$id, name:$name, year:$year, month:$mo,"
                " day:$day, date_conf:$dc, dynasty_id:$did, lon:$lon, lat:$lat,"
                " place:$place, actors:$actors, n_student:$ns, n_scholar:$nc,"
                " n_fan:$nf, significance:$sig, dispute:$dis, route:$route,"
                " route_label:$rl, from_book:$fb, book_sources:$bs,"
                " external_ids:$ext, provenance:$prov,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": e.id, "name": e.name, "year": e.year,
                 "mo": e.month, "day": e.day,
                 "dc": getattr(e, "date_confidence", "exact"),
                 "did": e.dynasty_id, "lon": e.lon, "lat": e.lat,
                 "place": e.place, "actors": _j(e.actors),
                 "ns": e.narrative.student, "nc": e.narrative.scholar,
                 "nf": e.narrative.fan,
                 "sig": e.significance, "dis": e.dispute,
                 "route": _j(e.route) if e.route else None,
                 "rl": e.route_label, "fb": e.from_book,
                 "bs": _j([s.model_dump() for s in e.book_sources]),
                 "ext": _j(getattr(e, "external_ids", {}) or {}),
                 "prov": getattr(e, "provenance", None)
                 or ("book" if getattr(e, "from_book", None) else "seed"),
                 "ca": e.created_at, "ua": e.updated_at, "ver": e.version})

        for p in self.people:
            self._x(
                "CREATE (n:Person {id:$id, name:$name, years:$years,"
                " dynasty_id:$did, home:$home, lon:$lon, lat:$lat,"
                " aliases:$aliases, n_student:$ns, n_scholar:$nc, n_fan:$nf,"
                " from_book:$fb, book_sources:$bs,"
                " external_ids:$ext, provenance:$prov,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": p.id, "name": p.name, "years": _j(list(p.years)),
                 "did": p.dynasty_id, "home": p.home, "lon": p.lon, "lat": p.lat,
                 "aliases": _j(p.aliases),
                 "ns": p.narrative.student, "nc": p.narrative.scholar,
                 "nf": p.narrative.fan, "fb": p.from_book,
                 "bs": _j([s.model_dump() for s in p.book_sources]),
                 "ext": _j(getattr(p, "external_ids", {}) or {}),
                 "prov": getattr(p, "provenance", None)
                 or ("book" if getattr(p, "from_book", None) else "seed"),
                 "ca": p.created_at, "ua": p.updated_at, "ver": p.version})

        for pl in self.places:
            self._x(
                "CREATE (n:Place {id:$id, name:$name, ancient:$anc,"
                " modern:$mod, lon:$lon, lat:$lat, aliases:$aliases,"
                " n_student:$ns, n_scholar:$nc, n_fan:$nf,"
                " external_ids:$ext, provenance:$prov,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": pl.id, "name": pl.name, "anc": pl.ancient,
                 "mod": pl.modern, "lon": pl.lon, "lat": pl.lat,
                 "aliases": _j(pl.aliases),
                 "ns": pl.narrative.student, "nc": pl.narrative.scholar,
                 "nf": pl.narrative.fan,
                 "ext": _j(getattr(pl, "external_ids", {}) or {}),
                 "prov": getattr(pl, "provenance", None) or "seed",
                 "ca": pl.created_at, "ua": pl.updated_at, "ver": pl.version})

        for owner, srcs in self.sources.items():
            for i, s in enumerate(srcs):
                self._x(
                    "CREATE (n:Source {sid:$sid, owner:$owner, level:$lv,"
                    " work:$work, text:$text, translation:$tr,"
                    " confidence:$conf, from_book:$fb})",
                    {"sid": f"{owner}#{i}", "owner": owner, "lv": s.level,
                     "work": s.work, "text": s.text, "tr": s.translation,
                     "conf": s.confidence, "fb": s.from_book})

        for b in self.books:
            self._x(
                "CREATE (n:Book {id:$id, title:$title, author:$author,"
                " year:$year, level:$lv, color:$color, dynasty_id:$did,"
                " span:$span, tags:$tags, descr:$descr,"
                " extracts:$ex, mounted:$mounted, origin:$origin, file:$file,"
                " accepted:$accepted,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": b.id, "title": b.title, "author": b.author,
                 "year": b.year, "lv": b.level, "color": b.color,
                 "did": b.dynasty_id, "span": _j(list(b.span)),
                 "tags": _j(b.tags), "descr": b.description,
                 # 提案**不再整包写进 Book 节点**：单本动辄上万条（读通鉴论
                 # 1.6 万条 = 数 MB 字符串），Kuzu 扫描时必须把这些巨型行整页
                 # pin 住，缓冲池直接爆（"buffer pool is full"，加大缓冲池只能
                 # 推迟），而且写一半被中断还会留下坏 JSON 让服务起不来。
                 # 真值源是 data/ingest/<id>.json 产物文件；小书（策展的几本）
                 # 没有产物文件，仍然照原样写进节点。
                 "ex": _j([x.model_dump() for x in b.extracts])
                       if len(b.extracts) <= _INLINE_EXTRACTS_MAX else "[]",
                 "mounted": b.mounted,
                 "origin": getattr(b, "origin", "builtin"),
                 "file": getattr(b, "file", None),
                 "accepted": _j(getattr(b, "accepted", None)),
                 "ca": b.created_at, "ua": b.updated_at, "ver": b.version})

        for ins in self.institutions:
            self._x(
                "CREATE (n:Institution {id:$id, name:$name, kind:$kind,"
                " function:$func, dynasty_id:$did, span_s:$ss, span_e:$se,"
                " aliases:$aliases, ca:$ca, ua:$ua, version:$ver})",
                {"id": ins.id, "name": ins.name, "kind": ins.kind,
                 "func": ins.function, "did": ins.dynasty_id,
                 "ss": ins.span[0], "se": ins.span[1],
                 "aliases": _j(ins.aliases),
                 "ca": ins.created_at, "ua": ins.updated_at, "ver": ins.version})

        for art in self.artifacts:
            self._x(
                "CREATE (n:Artifact {id:$id, name:$name, kind:$kind,"
                " material:$material, era_text:$era, year:$year,"
                " date_conf:$dc, place_id:$pid, museum:$museum,"
                " aliases:$aliases, ca:$ca, ua:$ua, version:$ver})",
                {"id": art.id, "name": art.name, "kind": art.kind,
                 "material": art.material, "era": art.era_text,
                 "year": art.year,
                 "dc": art.date_confidence, "pid": art.place_id,
                 "museum": art.museum, "aliases": _j(art.aliases),
                 "ca": art.created_at, "ua": art.updated_at, "ver": art.version})

        for con in self.concepts:
            self._x(
                "CREATE (n:Concept {id:$id, name:$name, kind:$kind,"
                " definition:$def, dynasty_id:$did, origin_year:$oy,"
                " aliases:$aliases, ca:$ca, ua:$ua, version:$ver})",
                {"id": con.id, "name": con.name, "kind": con.kind,
                 "def": con.definition, "did": con.dynasty_id,
                 "oy": con.origin_year, "aliases": _j(con.aliases),
                 "ca": con.created_at, "ua": con.updated_at, "ver": con.version})

        # 切块不再写入 Kuzu：真值源为 DuckDB warehouse
        # （迁移见 _sync_chunks_to_warehouse，写入见 set_chunks）

        label_of = self._label_index()
        dangling: list[dict] = []
        for e in self.edges:
            la, lb = label_of.get(e["a"]), label_of.get(e["b"])
            if not la or not lb:
                # 端点尚未纳入本体的悬空边（如陈胜、项羽等未建 Person 节点），
                # 图库遵循引用完整性不入 KGREL，转存旁车文件保持 API 行为一致
                dangling.append(e)
                continue
            ka = "sid" if la == "Source" else "id"
            kb = "sid" if lb == "Source" else "id"
            self._x(
                f"MATCH (a:{la} {{{ka}:$a}}), (b:{lb} {{{kb}:$b}}) "
                f"CREATE (a)-[:KGREL {{rel:$rel, from_book:$fb,"
                f" confidence:$conf, src_ids:$sids}}]->(b)",
                {"a": e["a"], "b": e["b"], "rel": e["rel"],
                 "fb": e.get("from_book"),
                 "conf": e.get("confidence", 0.8),
                 "sids": _j(e.get("src_ids", []))})
        self._pending_dangling = dangling     # 由 _save_locked 落盘，避免这里直写文件

    def _load_kuzu(self) -> None:

        def nodes(table: str) -> list[dict]:
            r = self._x(f"MATCH (n:{table}) RETURN n")
            out = []
            while r.has_next():
                out.append(_strip_node(dict(r.get_next()[0])))
            return out

        self.dynasties = [Dynasty(
            id=d["id"], name=d["name"], span=(d["span_s"], d["span_e"]),
            color=d["color"],
            capital=(d["cap_name"], d["cap_lon"], d["cap_lat"]),
            map_tile=d["map_tile"], emperors=json.loads(d["emperors"] or "[]"),
            group=d["grp"], lane=d["lane"] or 0,
            external_ids=json.loads(d.get("external_ids") or "{}"),
            provenance=d.get("provenance") or "seed",
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Dynasty")]

        self.events = [Event(
            id=d["id"], name=d["name"], year=d["year"],
            month=d["month"], day=d["day"],
            date_confidence=d["date_conf"] or "exact",
            dynasty_id=d["dynasty_id"], lon=d["lon"], lat=d["lat"],
            place=d["place"], actors=json.loads(d["actors"] or "[]"),
            narrative=Narrative(student=d["n_student"],
                                scholar=d["n_scholar"], fan=d["n_fan"]),
            significance=d["significance"], dispute=d["dispute"],
            route=json.loads(d["route"]) if d["route"] else None,
            route_label=d["route_label"], from_book=d["from_book"],
            book_sources=[Source.model_validate(x)
                          for x in json.loads(d["book_sources"] or "[]")],
            external_ids=json.loads(d.get("external_ids") or "{}"),
            provenance=d.get("provenance")
            or ("book" if d.get("from_book") else "seed"),
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Event")]

        self.people = [Person(
            id=d["id"], name=d["name"], years=tuple(json.loads(d["years"])),
            dynasty_id=d["dynasty_id"], home=d["home"],
            lon=d["lon"], lat=d["lat"],
            aliases=json.loads(d.get("aliases") or "[]"),
            narrative=Narrative(student=d["n_student"],
                                scholar=d["n_scholar"], fan=d["n_fan"]),
            from_book=d["from_book"],
            book_sources=[Source.model_validate(x)
                          for x in json.loads(d["book_sources"] or "[]")],
            external_ids=json.loads(d.get("external_ids") or "{}"),
            provenance=d.get("provenance")
            or ("book" if d.get("from_book") else "seed"),
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Person")]

        self.places = [Place(
            id=d["id"], name=d["name"], ancient=d["ancient"],
            modern=d["modern"], lon=d["lon"], lat=d["lat"],
            aliases=json.loads(d.get("aliases") or "[]"),
            narrative=Narrative(student=d["n_student"],
                                scholar=d["n_scholar"], fan=d["n_fan"]),
            external_ids=json.loads(d.get("external_ids") or "{}"),
            provenance=d.get("provenance") or "seed",
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Place")]

        srcs: dict[str, list[Source]] = {}
        r = self._x("MATCH (s:Source) RETURN s ORDER BY s.sid")
        while r.has_next():
            d = _strip_node(dict(r.get_next()[0]))
            srcs.setdefault(d["owner"], []).append(Source(
                level=d["level"], work=d["work"], text=d["text"],
                translation=d["translation"] or "",
                confidence=d["confidence"], from_book=d["from_book"]))
        self.sources = srcs

        self.books = [Book(
            id=d["id"], title=d["title"], author=d["author"], year=d["year"],
            level=d["level"], color=d["color"], dynasty_id=d["dynasty_id"],
            span=tuple(_loads_or(d["span"], [0, 0])),
            tags=_loads_or(d["tags"], []),
            description=d["descr"],
            # 进程被强杀时，正在写的这一列可能留下半截 JSON。以前直接 json.loads
            # 会让**整个服务起不来**（不是少一本的数据问题，是全套接口都挂）。
            # 坏数据按空处理：提案随时能重算，服务可用优先。
            # 大书的提案不内联在节点里（见 _render），这里回落到产物文件。
            extracts=[Extract.model_validate(x)
                      for x in (_loads_or(d["extracts"], [])
                                or _artifact_proposals(d["id"]))],
            mounted=bool(d["mounted"]),
            origin=d.get("origin") or "builtin", file=d.get("file"),
            accepted=_loads_or(d.get("accepted") or "null", None),
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Book")]
        self._apply_scopes()
        self._index_invalidate()      # 实体列表整体换过，索引缓存作废

        def _load_new(label: str, cls, extra: dict):
            if not self._has_table(label):
                return []
            out = []
            for d in nodes(label):
                kw = {"id": d["id"], "name": d["name"],
                      "aliases": json.loads(d.get("aliases") or "[]"),
                      "created_at": d.get("ca") or "",
                      "updated_at": d.get("ua") or "",
                      "version": d.get("version") or 1}
                kw.update(extra(d))
                out.append(cls(**kw))
            return out

        self.institutions = _load_new(
            "Institution", Institution,
            lambda d: {"kind": d["kind"], "function": d.get("function"),
                       "dynasty_id": d.get("dynasty_id"),
                       "span": (d.get("span_s"), d.get("span_e"))})
        self.artifacts = _load_new(
            "Artifact", Artifact,
            lambda d: {"kind": d["kind"], "material": d.get("material"),
                       "era_text": d.get("era_text"), "year": d.get("year"),
                       "date_confidence": d.get("date_conf") or "year",
                       "place_id": d.get("place_id"),
                       "museum": d.get("museum")})
        self.concepts = _load_new(
            "Concept", Concept,
            lambda d: {"kind": d["kind"], "definition": d.get("definition") or "",
                       "dynasty_id": d.get("dynasty_id"),
                       "origin_year": d.get("origin_year")})

        chunks: dict[str, list[dict]] = {}
        if self._has_table("Chunk"):
            for d in nodes("Chunk"):
                chunks.setdefault(d["book_id"], []).append({
                    "idx": d["idx"], "text": d["text"],
                    "embedding": json.loads(d["embedding"]) if d.get("embedding") else None})
        for v in chunks.values():
            v.sort(key=lambda x: x["idx"])
        self.chunks = chunks

        new_schema = self._has_table("Institution")
        if new_schema:
            r = self._x("MATCH (a)-[r:KGREL]->(b) "
                        "RETURN a.id AS a, b.id AS b, r.rel AS rel, "
                        "r.from_book AS fb, r.confidence AS conf, r.src_ids AS sids")
        else:
            r = self._x("MATCH (a)-[r:KGREL]->(b) "
                        "RETURN a.id AS a, b.id AS b, r.rel AS rel, "
                        "r.from_book AS fb")
        self.edges = []
        while r.has_next():
            row = r.get_next()
            a, b, rel, fb = row[0], row[1], row[2], row[3]
            conf = row[4] if new_schema and row[4] is not None else 0.8
            sids = json.loads(row[5]) if new_schema and row[5] else []
            self.edges.append({"a": a, "b": b, "rel": rel, "from_book": fb,
                               "confidence": conf, "src_ids": sids})
        dang = _KG_DIR / "dangling_edges.json"
        if dang.exists():
            self.edges.extend(json.loads(dang.read_text(encoding="utf-8")))

    # ================= 图库浏览（Kuzu 原生 Cypher） =================
    _KG_LABELS = ("Dynasty", "Event", "Person", "Place", "Book",
                   "Institution", "Artifact", "Concept")

    def _all(self, query: str, params: dict | None = None) -> list:
        """加锁执行并在锁内取完全部行（Result 不跨锁使用）。"""
        with self._lock:
            r = self._conn.execute(query, params) if params else self._conn.execute(query)
            rows = []
            while r.has_next():
                rows.append(r.get_next())
            return rows

    @staticmethod
    def _kg_node(d: dict) -> dict:
        label = d["_label"]
        name = d.get("name") or d.get("title") or d["id"]
        node = {"id": d["id"], "label": label, "name": name, "sub": None}
        if label == "Dynasty":
            node["sub"] = f"{d['span_s']}–{d['span_e']}"
        elif label == "Event":
            node["sub"] = str(d.get("year"))
        elif label == "Person":
            node["sub"] = ""
        elif label == "Book":
            node["sub"] = str(d.get("year"))
        elif label == "Institution":
            s, e2 = d.get("span_s"), d.get("span_e")
            node["sub"] = f"{d.get('kind')}　{s or '?'}–{e2 or '?'}"
        elif label == "Artifact":
            node["sub"] = f"{d.get('kind')}　{d.get('year') if d.get('year') is not None else '纪年无考'}"
        elif label == "Concept":
            node["sub"] = d.get("kind")
        return node

    @classmethod
    def _kg_edge(cls, rid: str, src: dict, dst: dict, rel: str) -> dict:
        return {"id": rid, "source": src["id"], "target": dst["id"], "rel": rel}

    def kg_schema(self) -> dict:
        tables = []
        for t in self._KG_LABELS:
            tables.append({"label": t, "count": self._all(
                f"MATCH (n:{t}) RETURN count(n) AS c")[0][0]})
        edge_rows = self._all(
            "MATCH ()-[e:KGREL]->() RETURN e.rel AS rel, count(*) AS c "
            "ORDER BY c DESC")
        dang_path = _KG_DIR / "dangling_edges.json"
        dangling = (len(json.loads(dang_path.read_text(encoding="utf-8")))
                    if dang_path.exists() else 0)
        return {"tables": tables,
                "edge_count": sum(r[1] for r in edge_rows),
                "dangling": dangling,
                "relations": [{"rel": r[0], "count": r[1]} for r in edge_rows]}

    def kg_nodes(self, table: str, q: str, limit: int = 40) -> list[dict]:
        if table not in self._KG_LABELS:
            return []
        col = "title" if table == "Book" else "name"
        if q.strip():
            rows = self._all(
                f"MATCH (n:{table}) WHERE n.{col} CONTAINS $q RETURN n "
                f"LIMIT $lim", {"q": q.strip(), "lim": limit})
        else:
            rows = self._all(
                f"MATCH (n:{table}) RETURN n LIMIT $lim", {"lim": limit})
        return [self._kg_node(dict(row[0])) for row in rows]

    def _find_node(self, node_id: str) -> dict | None:
        for t in self._KG_LABELS:
            rows = self._all(
                f"MATCH (n:{t} {{id:$id}}) RETURN n", {"id": node_id})
            if rows:
                return dict(rows[0][0])
        return None

    def kg_subgraph(self, node_id: str) -> dict | None:
        center = self._find_node(node_id)
        if center is None:
            return None
        clabel = center["_label"]
        nodes: dict[str, dict] = {node_id: self._kg_node(center)}
        edges: dict[str, dict] = {}
        rows = self._all(
            f"MATCH (n:{clabel} {{id:$id}})-[r:KGREL]-(m) RETURN n, r, m",
            {"id": node_id})
        for n_d, r_d, m_d in rows:
            nd, rd, md = dict(n_d), dict(r_d), dict(m_d)
            nodes.setdefault(md["id"], self._kg_node(md))
            forward = rd.get("_src") == nd.get("_id")
            s, t = (nd, md) if forward else (md, nd)
            eid = f"{s['id']}->{t['id']}:{rd['rel']}"
            edges.setdefault(eid, self._kg_edge(eid, s, t, rd["rel"]))
        return {"center": node_id, "nodes": list(nodes.values()),
                "edges": list(edges.values())}

    def kg_whole(self) -> dict:
        """全图：所有 KGREL 端点节点 + 全部已解析边（悬空边无节点不可视化）。"""
        nodes: dict[str, dict] = {}
        edges: dict[str, dict] = {}
        rows = self._all("MATCH (n)-[r:KGREL]->(m) RETURN n, r, m")
        for n_d, r_d, m_d in rows:
            nd, rd, md = dict(n_d), dict(r_d), dict(m_d)
            nodes.setdefault(nd["id"], self._kg_node(nd))
            nodes.setdefault(md["id"], self._kg_node(md))
            eid = f"{nd['id']}->{md['id']}:{rd['rel']}"
            edges.setdefault(eid, self._kg_edge(eid, nd, md, rd["rel"]))
        return {"center": None, "nodes": list(nodes.values()),
                "edges": list(edges.values())}

    # ================= 查询 =================
    def dynasty_at(self, year: int) -> Dynasty:
        # 只取主时间线（lane=0）的朝代；并立政权（lane>0）不参与年份定位
        mains = [d for d in self.dynasties if getattr(d, "lane", 0) == 0]
        for d in mains:
            if d.span[0] == year:
                return d
        for d in mains:
            if d.span[0] < year <= d.span[1]:
                return d
        return mains[-1]

    def events_of(self, dynasty_id: str) -> list[Event]:
        return [e for e in self.events if e.dynasty_id == dynasty_id]

    def get(self, kind: str, oid: str) -> Event | Person | Place | None:
        table = {"event": self.events, "person": self.people, "place": self.places}[kind]
        return next((x for x in table if x.id == oid), None)

    def sources_of(self, kind: str, obj) -> list[Source]:
        out: list[Source] = []
        if kind == "event":
            out.extend(self.sources.get(obj.id, []))
        if kind == "person":
            for e in self.events:
                if obj.id in e.actors:
                    out.extend(self.sources.get(e.id, []))
        out.extend(getattr(obj, "book_sources", []))
        return out

    def edges_of(self, node_id: str) -> list[dict]:
        """知识图谱一阶邻接边（含悬空边；KGREL 中的已解析部分即 Cypher 邻域）。"""
        return [e for e in self.edges if e["a"] == node_id or e["b"] == node_id]

    def search(self, term: str) -> list[tuple[str, object]]:
        term = term.strip()
        hits: list[tuple[str, object]] = []
        if not term:
            return hits
        for p in self.people:
            if term in p.name:
                hits.append(("person", p))
        for e in self.events:
            if term in e.name:
                hits.append(("event", e))
        for pl in self.places:
            if term in pl.name or term in pl.ancient:
                hits.append(("place", pl))
        return hits[:8]

    # ================= 书籍切块 =================
    # 真值源为 DuckDB warehouse；内存 self.chunks 仅作检索缓存。
    def _sync_chunks_to_warehouse(self) -> None:
        """启动自检：DuckDB 为空则以内存（旧库载入）灌入；否则以 DuckDB 为准。"""
        try:
            n_wh = warehouse.chunk_count()
        except Exception:
            return  # 数仓不可用：退化为内存切块，不阻断启动
        if n_wh == 0 and self.chunks:
            for bid, chs in self.chunks.items():
                warehouse.replace_chunks(bid, chs)
            return
        if n_wh > 0:
            loaded: dict[str, list[dict]] = {}
            for bid, ch in warehouse.all_chunks():
                loaded.setdefault(bid, []).append(ch)
            self.chunks = loaded

    def set_chunks(self, book_id: str, chunks: list[dict]) -> None:
        ordered = sorted(chunks, key=lambda c: c["idx"])
        self.chunks[book_id] = ordered
        try:
            warehouse.replace_chunks(book_id, ordered)
        except Exception:
            pass  # 数仓写失败不阻断内存状态，下次启动自检会再次同步

    def chunks_of(self, book_id: str) -> list[dict]:
        return list(self.chunks.get(book_id, []))

    @property
    def all_chunks(self) -> list[tuple[str, dict]]:
        return [(bid, ch) for bid, chs in self.chunks.items() for ch in chs]

    def drop_chunks(self, book_id: str) -> None:
        self.chunks.pop(book_id, None)
        try:
            warehouse.drop_chunks(book_id)
        except Exception:
            pass

    # ================= 上传书删除（trash 回收，支持 undo 恢复） =================
    def trash_book(self, book: Book) -> Path:
        """把上传书的文件、书籍快照、切块移入 trash/{book_id}，再从主库移除。"""
        with self.transaction():
            return self._trash_locked(book)

    def _trash_locked(self, book: Book) -> Path:
        if getattr(book, "origin", "builtin") != "upload":
            raise ValueError("仅上传书籍可删除")
        if book.mounted:
            raise ValueError("请先卸载再删除")
        tdir = _LIB_TRASH / book.id
        tdir.mkdir(parents=True, exist_ok=True)
        stored = book.file
        if stored:
            src = _LIB_FILES / stored
            if src.exists():
                src.replace(tdir / stored)
        (tdir / "book.json").write_text(
            _j(book.model_dump()), encoding="utf-8")
        (tdir / "chunks.json").write_text(
            _j(self.chunks_of(book.id)), encoding="utf-8")
        self.books = [b for b in self.books if b.id != book.id]
        self._index_invalidate()
        self.drop_chunks(book.id)
        self.save()
        return tdir

    def restore_book(self, book_id: str) -> Book:
        """从 trash 恢复书籍（action.undo 调用）。"""
        with self.transaction():
            return self._restore_locked(book_id)

    def _restore_locked(self, book_id: str) -> Book:
        tdir = _LIB_TRASH / book_id
        snap = tdir / "book.json"
        if not snap.exists():
            raise KeyError("回收区内无此书，无法恢复")
        book = Book.model_validate(
            json.loads(snap.read_text(encoding="utf-8")))
        stored = book.file
        if stored and (tdir / stored).exists():
            (tdir / stored).replace(_LIB_FILES / stored)
        chp = tdir / "chunks.json"
        if chp.exists():
            chs = json.loads(chp.read_text(encoding="utf-8"))
            if chs:
                self.set_chunks(book_id, chs)
        self.books.append(book)
        self._apply_scopes()
        self._index_put(book.id, "Book")
        self.save()
        # 恢复成功后清理回收快照目录
        try:
            for f in tdir.iterdir():
                f.unlink()
            tdir.rmdir()
        except OSError:
            pass
        return book

    # ================= 动态本体写操作（Action Types） =================
    def commit_book(self, book: Book, accepted: list[bool], *, persist: bool = True) -> dict:
        """挂载：按 accepted 位图写入本体并落盘（内存变更 + save 同一临界区）。"""
        if getattr(book, "scope", CANONICAL) == EXTENDED:
            raise ValueError(
                f"《{book.title}》是策展书（扩展知识），不入主干本体："
                f"叙述多为观点或文学虚构，挂上去会让小说情节成为可与正史并列的引用。"
                f"确需入主干，先把它从 data/book_scopes.json 的名单里移除。")
        with self.transaction():
            return self._commit_locked(book, accepted, persist=persist)

    def _commit_locked(self, book: Book, accepted: list[bool], *, persist: bool = True) -> dict:
        added = {"person": 0, "event": 0, "link": 0, "relation": 0}
        rel_failed = 0
        if len(accepted) != len(book.extracts):
            raise ValueError("accepted 位图长度须与提案数一致")
        # 同 id 必须复用，不能无脑 append：人物 id 常常就是名字（「王饶」），
        # 事件 id 形如 <book>_e<n>；两本书提到同一个人就会各建一条同 id 的 Person。
        # 内存里出现两条同 id → 投影到 DuckDB 副本时主键 (kind, entity_id) 冲突 →
        # **整批副本写不进去**（《旧五代史》《新唐书》的副本就是这样整本丢的），
        # 而副本是时间窗/空间窗查询的唯一来源。这里按 id 建索引，命中就合并证据。
        p_index = {p.id: p for p in self.people}
        e_index = {e.id: e for e in self.events}
        for ext, ok in zip(book.extracts, accepted):
            if not ok:
                continue
            node_id = None
            src = Source(level=book.level, work=f"《{book.title}》", text=ext.summary,
                         translation=ext.summary, confidence=ext.confidence, from_book=book.id)
            if ext.kind == "person":
                node_id = ext.new_id
                # 上传书 LLM 提案可能无生卒：给空串占位，避免 None.split
                yr_parts = (ext.years or "").split("—")
                years = (yr_parts + ["", ""])[:2]
                exist = p_index.get(node_id)
                if exist is not None:
                    # 已存在（跨书同名）：并入这条证据，不新建节点
                    exist.book_sources.append(src)
                    added["person_merged"] = added.get("person_merged", 0) + 1
                else:
                    p = Person(
                        id=node_id, name=ext.name, years=tuple(years),
                        dynasty_id=book.dynasty_id, home="（挂载书源实体）",
                        narrative=Narrative(
                            student=ext.summary,
                            scholar=ext.summary + f"　【本体注记】本实体由《{book.title}》挂载，学术使用请核对原书及更上游史料。",
                            fan=ext.summary),
                        from_book=book.id, book_sources=[src])
                    self.people.append(p)
                    p_index[node_id] = p
                    self._index_put(node_id, "Person")
                    added["person"] += 1
            elif ext.kind == "event":
                node_id = ext.new_id
                ev_year = ext.year or 0
                # 朝代按事件年份推导，而非全书统一用 book.dynasty_id：
                # 《资治通鉴》这类通史跨十余朝，整本书标一个朝代会把纪年归属搞错。
                ev_dyn = book.dynasty_id
                if ev_year:
                    try:
                        ev_dyn = self.dynasty_at(int(ev_year)).id
                    except Exception:
                        pass
                exist = e_index.get(node_id)
                if exist is not None:
                    exist.book_sources.append(src)
                    added["event_merged"] = added.get("event_merged", 0) + 1
                else:
                    ev = Event(
                        id=node_id, name=ext.name, year=ev_year,
                        dynasty_id=ev_dyn,
                        # 坐标缺失一律为 None（禁止 0.0：会堆到几内亚湾）
                        lon=ext.lon, lat=ext.lat,
                        place=ext.place or "", actors=[],
                        narrative=Narrative(student=ext.summary, scholar=ext.summary, fan=ext.summary),
                        significance=None,
                        dispute=f"挂载观点：代表《{book.title}》作者的解释框架，非学界定论。",
                        from_book=book.id, book_sources=[src])
                    self.events.append(ev)
                    e_index[node_id] = ev
                    self._index_put(node_id, "Event")
                    added["event"] += 1
            elif ext.kind == "link":
                # 对齐到主干实体：目标可能是人或事，只按事件找会把「对齐到人物」
                # 的提案全部静默丢弃（实测 62120 条 link 里绝大多数指向人物）。
                node_id = ext.target_id
                target = (self.get("event", node_id)
                          or self.get("person", node_id))
                if target:
                    target.book_sources.append(src)
                    added["link"] += 1
                else:
                    node_id = None  # 目标缺失：不造悬空边
            elif ext.kind == "relation":
                # 实体间语义边：这是「谁参与了哪件事」的答案来源。端点在本次
                # 挂载里刚建出来（也可挂到主干），缺任一端就跳过，不造悬空边。
                node_id = None
                try:
                    self.add_edge(ext.new_id, ext.target_id, ext.rel or "相关",
                                  confidence=ext.confidence, src_ids=[book.id],
                                  from_book=book.id)
                    added["relation"] += 1
                except Exception:      # 端点不存在 / 组合不在 REL_PAIRS 目录内
                    rel_failed += 1
            # 知识图谱增量边：书 → 节点（走 add_edge：端点存在 + REL_PAIRS 校验）
            if node_id:
                self.add_edge(book.id, node_id, "书载观点",
                              confidence=ext.confidence, src_ids=[book.id],
                              from_book=book.id)
        # mounted 标记必须等本体真正落盘之后再写：先置位再 save 的话，save 抛错
        # （例：Kuzu bad allocation）会留下「内存已挂载、图库没数据」的脏状态，
        # 而且 book.mounted 为真会让后续 apply 被 409 挡住、只能先卸载才能重来。
        # 第二次 save 只写 Book 行，其余行 diff 为零，成本可忽略。
        if persist:
            self.save()
        book.mounted = True
        book.accepted = list(accepted)
        if persist:
            self.save()
        if rel_failed:
            added["relation_skipped"] = rel_failed
        return added

    def uncommit_book(self, book: Book) -> dict:
        """卸载：移除该书引入的节点/边并落盘（内存变更 + save 同一临界区）。"""
        with self.transaction():
            return self._uncommit_locked(book)

    def _uncommit_locked(self, book: Book) -> None:
        """卸载的顺序是硬约束：**先清磁盘，再改内存**。

        旧实现反过来（先改内存、再 save 走通用 diff），中途失败就会留下
        「内存里已卸载、图库里都还在」的分叉：接口显示未挂载，但数据仍在，
        重启后又从磁盘原样加载回来，用户以为删掉的东西复活了。现在磁盘侧
        整书清除只依赖 `from_book`（见 _purge_book_disk），失败时内存完全没被
        动过，可以直接原样重试，不存在半成品状态。
        """
        removed = self._purge_book_disk(book.id)
        self._reset_kuzu_connection()      # 批量删除把连接毒化了，见方法注释
        self._purge_buffer.extend(removed)
        self.people = [p for p in self.people if p.from_book != book.id]
        self.events = [e for e in self.events if e.from_book != book.id]
        for table in (self.events, self.people, self.places):
            for obj in table:
                bs = getattr(obj, "book_sources", None)
                if bs:
                    obj.book_sources = [s for s in bs if s.from_book != book.id]
        self.edges = [e for e in self.edges if e.get("from_book") != book.id]
        self._index_invalidate()      # 实体已移除，索引缓存必须作废
        book.mounted = False
        # 此刻磁盘已不含该书任何行，快照也已对齐 → 本次 save 只会写 Book 节点
        # 的 mounted 翻转与残留 book_sources 变更，并清掉 DuckDB 实体副本。
        self.save()
        return {"person": 0, "event": 0, "removed": len(removed)}


# 单例（FastAPI lifespan 中创建并挂到 app.state）
store = Store()
