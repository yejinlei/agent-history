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
from contextlib import contextmanager
from pathlib import Path

import kuzu

from app.data import seed
from app.warehouse import warehouse
from app.schema.ontology import (
    Book, Dynasty, Event, Extract, Institution, Artifact, Concept,
    Narrative, Person, Place, Source, REL_PAIRS,
)

_DATA_DIR = Path(__file__).parent / "data"
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
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Person(
        id STRING, name STRING, years STRING, dynasty_id STRING, home STRING,
        lon DOUBLE, lat DOUBLE, aliases STRING,
        n_student STRING, n_scholar STRING, n_fan STRING,
        from_book STRING, book_sources STRING,
        ca STRING, ua STRING, version INT64,
        PRIMARY KEY(id))""",
    """CREATE NODE TABLE Place(
        id STRING, name STRING, ancient STRING, modern STRING,
        lon DOUBLE, lat DOUBLE, aliases STRING,
        n_student STRING, n_scholar STRING, n_fan STRING,
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


def _strip_node(d: dict) -> dict:
    d.pop("_id", None)
    d.pop("_label", None)
    return d


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
        # save 全量重建（DROP+CREATE）整体临界区，杜绝并发 save 交叉破坏
        self._save_lock = threading.RLock()
        self._db = kuzu.Database(str(_KUZU_DB))
        self._conn = kuzu.Connection(self._db)

        if self._kuzu_ready():
            self._load_kuzu()
            # 切块真值源迁移：旧库 Chunk 表数据一次性搬入 DuckDB
            self._sync_chunks_to_warehouse()
            # 实体副本同步到 DuckDB，供 /api/graph/query 做时间/空间切片
            self._sync_entities_to_warehouse()
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

    def _create_schema(self) -> None:
        self._refuse_wipe()          # 落库前先确认不会拿空内存覆盖非空图库
        for t in _ALL_TABLES:
            try:
                self._x(f"DROP TABLE {t}")
            except Exception:
                pass
        for stmt in _NODE_DDL:
            self._x(stmt)
        self._x(_REL_DDL)

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

    def add_edge(self, a: str, b: str, rel: str, *,
                 confidence: float = 0.7, src_ids: list[str] | None = None,
                 from_book: str | None = None) -> dict:
        """写边唯一入口：端点必须存在，且端点组合落在 REL_PAIRS 目录内。"""
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
        """全量重写 Kuzu（原型数据量小；生产改为增量事务写库）。

        Kuzu 落盘后同步一份实体副本到 DuckDB，供时间窗 / 空间窗切片查询。
        """
        with self._save_lock:
            self._save_locked()
        self._sync_entities_to_warehouse()

    def _sync_entities_to_warehouse(self) -> None:
        """实体投影到 DuckDB（分析副本；真值源仍是 Kuzu，可随时重建）。

        失败不影响主流程——数仓只是加速查询的副本，丢失可重建。
        """
        try:
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
            warehouse.replace_entities(rows)
        except Exception:
            pass

    def _save_locked(self) -> None:
        self._create_schema()

        for d in self.dynasties:
            self._x(
                "CREATE (n:Dynasty {id:$id, name:$name, span_s:$ss, span_e:$se,"
                " color:$color, cap_name:$cn, cap_lon:$clon, cap_lat:$clat,"
                " map_tile:$mt, grp:$grp, lane:$lane, emperors:$emp,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": d.id, "name": d.name, "ss": d.span[0], "se": d.span[1],
                 "color": d.color, "cn": d.capital[0], "clon": d.capital[1],
                 "clat": d.capital[2], "mt": d.map_tile,
                 "grp": getattr(d, "group", None),
                 "lane": getattr(d, "lane", 0), "emp": _j(d.emperors),
                 "ca": d.created_at, "ua": d.updated_at, "ver": d.version})

        for e in self.events:
            self._x(
                "CREATE (n:Event {id:$id, name:$name, year:$year, month:$mo,"
                " day:$day, date_conf:$dc, dynasty_id:$did, lon:$lon, lat:$lat,"
                " place:$place, actors:$actors, n_student:$ns, n_scholar:$nc,"
                " n_fan:$nf, significance:$sig, dispute:$dis, route:$route,"
                " route_label:$rl, from_book:$fb, book_sources:$bs,"
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
                 "ca": e.created_at, "ua": e.updated_at, "ver": e.version})

        for p in self.people:
            self._x(
                "CREATE (n:Person {id:$id, name:$name, years:$years,"
                " dynasty_id:$did, home:$home, lon:$lon, lat:$lat,"
                " aliases:$aliases, n_student:$ns, n_scholar:$nc, n_fan:$nf,"
                " from_book:$fb, book_sources:$bs,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": p.id, "name": p.name, "years": _j(list(p.years)),
                 "did": p.dynasty_id, "home": p.home, "lon": p.lon, "lat": p.lat,
                 "aliases": _j(p.aliases),
                 "ns": p.narrative.student, "nc": p.narrative.scholar,
                 "nf": p.narrative.fan, "fb": p.from_book,
                 "bs": _j([s.model_dump() for s in p.book_sources]),
                 "ca": p.created_at, "ua": p.updated_at, "ver": p.version})

        for pl in self.places:
            self._x(
                "CREATE (n:Place {id:$id, name:$name, ancient:$anc,"
                " modern:$mod, lon:$lon, lat:$lat, aliases:$aliases,"
                " n_student:$ns, n_scholar:$nc, n_fan:$nf,"
                " ca:$ca, ua:$ua, version:$ver})",
                {"id": pl.id, "name": pl.name, "anc": pl.ancient,
                 "mod": pl.modern, "lon": pl.lon, "lat": pl.lat,
                 "aliases": _j(pl.aliases),
                 "ns": pl.narrative.student, "nc": pl.narrative.scholar,
                 "nf": pl.narrative.fan,
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
                 "ex": _j([x.model_dump() for x in b.extracts]),
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
        (_KG_DIR / "dangling_edges.json").write_text(
            _j(dangling), encoding="utf-8")

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
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Person")]

        self.places = [Place(
            id=d["id"], name=d["name"], ancient=d["ancient"],
            modern=d["modern"], lon=d["lon"], lat=d["lat"],
            aliases=json.loads(d.get("aliases") or "[]"),
            narrative=Narrative(student=d["n_student"],
                                scholar=d["n_scholar"], fan=d["n_fan"]),
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
            span=tuple(json.loads(d["span"])), tags=json.loads(d["tags"]),
            description=d["descr"],
            extracts=[Extract.model_validate(x) for x in json.loads(d["extracts"])],
            mounted=bool(d["mounted"]),
            origin=d.get("origin") or "builtin", file=d.get("file"),
            accepted=json.loads(d.get("accepted") or "null"),
            created_at=d.get("ca") or "", updated_at=d.get("ua") or "",
            version=d.get("version") or 1)
            for d in nodes("Book")]

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
        with self.transaction():
            return self._commit_locked(book, accepted, persist=persist)

    def _commit_locked(self, book: Book, accepted: list[bool], *, persist: bool = True) -> dict:
        added = {"person": 0, "event": 0, "link": 0}
        if len(accepted) != len(book.extracts):
            raise ValueError("accepted 位图长度须与提案数一致")
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
                self.people.append(Person(
                    id=node_id, name=ext.name, years=tuple(years),
                    dynasty_id=book.dynasty_id, home="（挂载书源实体）",
                    narrative=Narrative(
                        student=ext.summary,
                        scholar=ext.summary + f"　【本体注记】本实体由《{book.title}》挂载，学术使用请核对原书及更上游史料。",
                        fan=ext.summary),
                    from_book=book.id, book_sources=[src]))
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
                self.events.append(Event(
                    id=node_id, name=ext.name, year=ev_year,
                    dynasty_id=ev_dyn,
                    # 坐标缺失一律为 None（禁止 0.0：会堆到几内亚湾）
                    lon=ext.lon, lat=ext.lat,
                    place=ext.place or "", actors=[],
                    narrative=Narrative(student=ext.summary, scholar=ext.summary, fan=ext.summary),
                    significance=None,
                    dispute=f"挂载观点：代表《{book.title}》作者的解释框架，非学界定论。",
                    from_book=book.id, book_sources=[src]))
                added["event"] += 1
            elif ext.kind == "link":
                node_id = ext.target_id
                target = self.get("event", node_id)
                if target:
                    target.book_sources.append(src)
                    added["link"] += 1
                else:
                    node_id = None  # 目标缺失：不造悬空边
            # 知识图谱增量边：书 → 节点（走 add_edge：端点存在 + REL_PAIRS 校验）
            if node_id:
                self.add_edge(book.id, node_id, "书载观点",
                              confidence=ext.confidence, src_ids=[book.id],
                              from_book=book.id)
        book.mounted = True
        book.accepted = list(accepted)
        if persist:
            self.save()
        return added

    def uncommit_book(self, book: Book) -> None:
        """卸载：移除该书引入的节点/边并落盘（内存变更 + save 同一临界区）。"""
        with self.transaction():
            self._uncommit_locked(book)

    def _uncommit_locked(self, book: Book) -> None:
        self.people = [p for p in self.people if p.from_book != book.id]
        self.events = [e for e in self.events if e.from_book != book.id]
        for table in (self.events, self.people, self.places):
            for obj in table:
                bs = getattr(obj, "book_sources", None)
                if bs:
                    obj.book_sources = [s for s in bs if s.from_book != book.id]
        self.edges = [e for e in self.edges if e.get("from_book") != book.id]
        book.mounted = False
        self.save()


# 单例（FastAPI lifespan 中创建并挂到 app.state）
store = Store()
