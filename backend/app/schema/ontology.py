"""本体论层（Ontology）

定义历史世界的对象类型（Object Types）与链接类型（Link Types）。
对标 Palantir Foundry：Object Types / Link Types / Action Types。

Object Types:
    Dynasty  政权（起止、都城、代表历史地图）
    Event    事件（时间、地点、参与者、意义、分歧）
    Person   人物（生卒、籍贯、所属政权）
    Place    地点（古今名、坐标）
    Source   史料（层级、原文、置信度）
    Book     可挂载书源（新书接入的数据源）

Link Types（由 Store/edges 表达）:
    Person -[:参与]-> Event
    Person -[:任职于]-> Dynasty
    Person -[:师承]-> Person
    Event  -[:发生于]-> Place
    Event  -[:触发]-> Event
    Book   -[:书载观点]-> Event | Person
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EntityType(str, Enum):
    dynasty = "dynasty"
    event = "event"
    person = "person"
    place = "place"


class Audience(str, Enum):
    student = "student"   # 学生：通俗脉络
    scholar = "scholar"   # 学者：学术叙述
    fan = "fan"           # 爱好者：故事化


class Narrative(BaseModel):
    student: str
    scholar: str
    fan: str


class _Governed(BaseModel):
    """全部主对象的统一治理字段（对齐 Foundry 版本管理；id 即 RID，不另设 rid）。"""
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    version: int = 1
    # 外部权威锚点（G5）：把本地符号锚定到公共参照系，等价于 owl:sameAs。
    #   键取 EXTERNAL_NAMESPACES 白名单：wikidata / cbdb / chgis / tgn / geonames / periodo
    #   例：{"cbdb": "12345", "wikidata": "Q7122"}
    external_ids: dict[str, str] = Field(default_factory=dict)
    # 知识来源层级（可废止推理 / PROV-O wasDerivedFrom）：冲突裁决的优先级依据。
    #   core=权威主干（CBDB/CHGIS/Wikidata）| seed=内置种子
    #   book=挂载书（单一史观，不可覆盖 core）| llm=模型抽取（最弱）
    #   注：Book 另有 origin 字段表示「内置 / 上传」，两者语义不同，勿混用。
    provenance: str = "seed"


class Dynasty(_Governed):
    id: str
    name: str
    span: tuple[int, int]
    color: str
    capital: tuple[str, float, float]         # 都城名、经度、纬度
    map_tile: Optional[str] = None           # 首选 osgeo 瓦片 ID
    emperors: list[dict] = Field(default_factory=list)
    # 时间轴细分：并立政权支持
    group: Optional[str] = None              # 归属时期组，如 sanguo/nanbei/wudai/songliao
    lane: int = 0                            # 0=主时间线；1/2=并立政权车道（三国、南北朝、五代十国、辽宋金）


class Event(_Governed):
    id: str
    name: str
    year: int
    # 高精度日期（夏历/农历月日；明清部分为公历，叙述中注明）。None = 仅精确到年
    month: Optional[int] = None
    day: Optional[int] = None
    # 纪年置信度：exact=史料明确 | month=仅知年月 | year=仅知年份 | disputed=纪年存疑（如前841年前）
    date_confidence: str = "exact"
    dynasty_id: str
    # 坐标由 (Event)-[:发生于]->(Place) 派生；未解析到地点时为 None。
    # 严禁填 0.0：那会把事件标记堆到几内亚湾 (0,0)。
    lon: Optional[float] = None
    lat: Optional[float] = None
    place: str = ""
    actors: list[str] = Field(default_factory=list)
    narrative: Narrative
    significance: Optional[str] = None
    dispute: Optional[str] = None
    # 行军/迁徙/出使等移动事件的地理路径：[[lon, lat], ...]，按时间顺序
    route: Optional[list[list[float]]] = None
    route_label: Optional[str] = None       # 路线说明，如"安禄山进军路线"
    # 动态本体挂载元数据
    from_book: Optional[str] = None
    book_sources: list["Source"] = Field(default_factory=list)


class Person(_Governed):
    id: str
    name: str
    years: tuple[str, str]
    dynasty_id: str
    home: str
    # 籍贯/主要活动地坐标（用于地图标记，可选）
    lon: Optional[float] = None
    lat: Optional[float] = None
    aliases: list[str] = Field(default_factory=list)   # 字、号、别称（实体融合/消歧用）
    narrative: Narrative
    from_book: Optional[str] = None
    book_sources: list["Source"] = Field(default_factory=list)


class Place(_Governed):
    id: str
    name: str
    ancient: str
    modern: str
    lon: float
    lat: float
    aliases: list[str] = Field(default_factory=list)
    narrative: Narrative


class Source(BaseModel):
    level: int                    # 1 原始史料 | 2 现代史学 | 3 大众/文学
    work: str
    text: str
    translation: str = ""
    confidence: float
    from_book: Optional[str] = None


class Extract(BaseModel):
    """新书提炼产物（挂载提案的一项）"""
    kind: Literal["person", "event", "link"]
    name: str
    summary: str
    confidence: float
    new_id: Optional[str] = None  # person/event：新建实体 id
    target_id: Optional[str] = None  # link：对齐到主干事件
    years: Optional[str] = None
    year: Optional[int] = None
    lon: Optional[float] = None
    lat: Optional[float] = None
    place: Optional[str] = None


class Book(_Governed):
    id: str
    title: str
    author: str
    year: int
    level: int                    # 挂载后史料层级
    color: str
    dynasty_id: str
    span: tuple[int, int]
    tags: list[str]
    description: str
    extracts: list[Extract]
    mounted: bool = False
    # 书籍来源：builtin=内置 seed；upload=用户界面/CLI 上传
    origin: str = "builtin"
    # 上传书籍的服务端存储文件名（仅 origin=upload）
    file: Optional[str] = None
    # 最近一次挂载时用户确认的提案位图（卸载撤销时按原位图重放）
    accepted: Optional[list[bool]] = None


Event.model_rebuild()
Person.model_rebuild()


class Institution(_Governed):
    """机构实体：行政/军事机构（org）与官职/差遣（post）同表，以 kind 区分。"""
    id: str
    name: str
    kind: str                              # org=机构 | post=官职
    function: Optional[str] = None         # 职能说明
    dynasty_id: Optional[str] = None       # 主要行用朝代
    span: tuple[Optional[int], Optional[int]] = (None, None)
    aliases: list[str] = Field(default_factory=list)


class Artifact(_Governed):
    """文物实体：考古器物、碑刻、简牍、文书等一手实物史料。"""
    id: str
    name: str
    kind: str                              # 器物 | 碑刻 | 简牍 | 文书 | 造像
    material: Optional[str] = None
    era_text: Optional[str] = None         # 铭文/纪年原文
    year: Optional[int] = None
    date_confidence: str = "year"          # exact | year | disputed | unknown
    place_id: Optional[str] = None         # 出土地（关联 Place）
    museum: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)


class Concept(_Governed):
    """观念实体：可跨朝代反复实例化的制度、思想、术语。
    纪律：治世/事变（如「文景之治」「安史之乱」）归 Event，不入 Concept。"""
    id: str
    name: str
    kind: str                              # 制度 | 思想 | 学说 | 术语 | 文化符号
    definition: str = ""
    dynasty_id: Optional[str] = None       # 起源朝代
    origin_year: Optional[int] = None
    aliases: list[str] = Field(default_factory=list)


class RelType(BaseModel):
    """规范边类型（文档化目录；写边的硬约束见 REL_PAIRS 端点组合）。"""
    rel: str
    src: str
    dst: str
    inverse: Optional[str] = None          # 逆关系名（仅文档；方向以存储为准）
    transitive: bool = False               # 是否传递（如 继承自/包含）
    desc: str = ""


def _R(rel: str, src: str, dst: str, inverse: Optional[str] = None,
       transitive: bool = False, desc: str = "") -> RelType:
    return RelType(rel=rel, src=src, dst=dst, inverse=inverse,
                   transitive=transitive, desc=desc)


# ---- 规范 Link Types（历史场景专属）----
REL_TYPES: list[RelType] = [
    # 主干既有边
    _R("参与", "Person", "Event", desc="人物与事件的通用参与"),
    _R("任职于", "Person", "Institution", desc="担任某官职/隶属某机构"),
    _R("师承", "Person", "Person", "师从/弟子"),
    _R("发生于", "Event", "Place"),
    _R("触发", "Event", "Event", desc="因果"),
    _R("奠定", "Event", "Dynasty"),
    _R("书载观点", "Book", "Event", inverse="见于"),
    _R("书载观点", "Book", "Person", inverse="见于"),
    # 新增边
    _R("继承自", "Dynasty", "Dynasty", transitive=True),
    _R("包含", "Event", "Event", transitive=True, desc="大事件拆分出子事件"),
    _R("隶属", "Institution", "Dynasty", desc="机构设置于某朝"),
    _R("设于", "Institution", "Place", desc="机构治所所在地"),
    _R("下辖", "Institution", "Institution"),
    _R("出土于", "Artifact", "Place"),
    _R("年代属", "Artifact", "Dynasty"),
    _R("铭文录为", "Artifact", "Source", desc="文物铭文/简文进入史料层"),
    _R("提及", "Book", "Institution"),
    _R("提及", "Book", "Artifact"),
    _R("提及", "Book", "Concept"),
    _R("创立", "Person", "Concept"),
    _R("推行", "Event", "Concept", desc="事件中推行某制度/观念"),
    _R("盛行于", "Concept", "Dynasty"),
    _R("从属", "Concept", "Concept", transitive=True, desc="观念谱系上下位"),
]


# ---- 允许的端点组合目录（写边硬约束；既有自由关系名沿用，仅约束端点类型）----
REL_PAIRS: set[tuple[str, str]] = {
    ("Person", "Person"), ("Person", "Event"), ("Person", "Place"),
    ("Person", "Dynasty"), ("Person", "Book"),
    ("Person", "Institution"), ("Person", "Concept"), ("Person", "Artifact"),
    ("Event", "Event"), ("Event", "Place"), ("Event", "Dynasty"),
    ("Event", "Person"), ("Event", "Book"), ("Event", "Concept"),
    ("Dynasty", "Dynasty"), ("Dynasty", "Place"), ("Dynasty", "Event"),
    ("Dynasty", "Person"), ("Dynasty", "Concept"),
    ("Place", "Place"), ("Place", "Event"),
    ("Book", "Person"), ("Book", "Event"), ("Book", "Place"),
    ("Book", "Dynasty"), ("Book", "Book"),
    ("Book", "Institution"), ("Book", "Artifact"), ("Book", "Concept"),
    ("Institution", "Institution"), ("Institution", "Dynasty"),
    ("Institution", "Place"), ("Institution", "Person"),
    ("Artifact", "Place"), ("Artifact", "Dynasty"),
    ("Artifact", "Source"), ("Artifact", "Institution"),
    ("Concept", "Dynasty"), ("Concept", "Concept"),
    ("Concept", "Person"), ("Concept", "Event"),
    # 注：仅允许 Artifact→Source（铭文录为）；反向 Source→Artifact 无 DDL 支持
}

