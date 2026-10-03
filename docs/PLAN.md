# ChronoSino 实施方案（本体 · 数仓 · Agent）

本文是后续开发的指导文档。目标：让系统能基于**多部史料**做**多视角取证与综合分析**，
而不是单方面史料叙事。

---

## 0. 现状基线与核心问题

### 0.1 架构现状

| 层 | 现状 |
|---|---|
| 模式层 | `app/schema/ontology.py` 手写 pydantic：9 个 Object Types、22 条 Link Types、`REL_PAIRS` 端点白名单 |
| 存储层 | Kuzu 单文件图库（`data/kg/kuzu_db`）+ Python 内存列表；`save()` 为**全量 DROP+CREATE 重写** |
| 检索层 | BM25（零依赖），有 embedding 时走余弦；切块与 embedding 存于 Kuzu `Chunk` 表 |
| Agent 层 | 内置「领航员」「太史」；7 个工具；写操作经确认卡人工批准 |
| LLM 层 | `StubLLM`（默认，无密钥）/ `OpenAILLM`（需 `OPENAI_API_KEY`；支持 `OPENAI_BASE_URL` 兼容协议） |

### 0.2 实测暴露的核心问题

| # | 问题 | 证据 |
|---|---|---|
| P1 | 图谱几乎不连通：3094 条边中「书载观点」占 3041（98.3%）；`参与`/`触发`/`包含` 为 0 | `/api/kg/schema` |
| P2 | 挂了 27 部书，实体上仍只有主干种子史料：秦始皇 `sources=5`，4 条出自《史记》，**0 条来自挂载书** | `/api/entity/person/qsh` |
| P3 | 书的视角挂不到人物：`commit_book` 的 link 分支只查 event（`store.py:912-919`）；内置书 `align_mode="skip"` 跳过已知人物（`pipeline.py:325-326`） | 代码 |
| P4 | 坐标缺失填 `0.0`（`store.py:904-906`），无坐标事件全部堆在几内亚湾 | 实测 `lon=0 lat=0` |
| P5 | 全书统一 `dynasty_id`（`store.py:903`），《通鉴》这类通史的纪年归属全错 | 实测 `dyn=donghan` |
| P6 | 卸载不校验外部引用，已产生 **29 条悬空边** | `/api/kg/schema` dangling |
| P7 | 抽取质量差：「迁司空」「七月壬子」被当作事件 | 实测 |
| P8 | Agent 无 function calling，仅「预检索 + 单次调用」（`runtime.py:206`） | 代码 |

---

## 1. 议题一：扩展 Agent（近现代史家编组）

### 1.1 设计

```
用户问题
   ↓
主编 Agent（分解 → 分派 → 对比 → 归因）
   ├─ 司马迁视角 Agent    限定读《史记》
   ├─ 司马光视角 Agent    限定读《资治通鉴》
   ├─ 王夫之视角 Agent    限定读《读通鉴论》
   ├─ 王国维 / 陈寅恪 / 陈垣 / 钱穆 视角 Agent（限定读其著作）
   └─ 现代史学视角 Agent  限定读 L2 现代研究
   ↓
各自取证（强制带引用）→ 主编列出分歧并归因（时代 / 立场 / 所据史料）
```

**关键机制：每个视角 Agent 被限制只能检索指定书籍。** 这样它输出的必然是该书的立场，
而不是模型自身的综合知识。

### 1.2 近现代史家候选

| 史家 | 专长 / 方法 | 代表作 |
|---|---|---|
| 王国维 | 二重证据法、殷周制度、甲骨金石 | 《殷周制度论》《观堂集林》 |
| 陈寅恪 | 中古史、关陇集团、种族与文化 | 《隋唐制度渊源略论稿》《唐代政治史述论稿》 |
| 陈垣 | 宗教史、元史、校勘学 | 《元也里可温教考》《校勘学释例》 |
| 钱穆 | 通史、制度史、文化保守立场 | 《国史大纲》《秦汉史》《中国历代政治得失》 |
| 顾颉刚（备选） | 古史辨、层累造成说 | 《古史辨》 |
| 吕思勉（备选） | 通史、制度与社会 | 《中国通史》《先秦史》 |
| 傅斯年（备选） | 史料学派、史学即史料学 | 《史料论略》 |

### 1.3 ⚠ 必须先解决的前提：文本入库，否则是角色扮演

**若库中没有该史家的著作文本，Agent 只能依赖模型自身知识作答 —— 那是角色扮演，不是基于史料，
会显著放大幻觉。** 因此：

- 史家 Agent 上线**必须以其著作入库为前置条件**
- 若暂时无文本，该 Agent 应明确标注为「模拟视角（无一手文本支撑）」，并在输出中声明
- 当前库中近现代著作仅《万历十五年》《大秦帝国》《丝绸之路》三部，且 `mounted=false`

**版权风险（需自行核实）**：中国著作权保护期为作者终生 +50 年。王国维（1927 卒）、
陈寅恪（1969）、陈垣（1971）的多数作品应已进入公有领域；钱穆（1990 卒）仍在保护期内，
需取得授权或改用已进入公有领域的早期文本 / 二手研究。入库前请逐部确认。

### 1.4 要补的能力

- **L1 结构化输出**：`OpenAILLM.extract(schema, text) -> dict`（用 `response_format` 或 Pydantic schema），
  替代当前「让模型输出 JSON 文本再正则解析」
- **L2 工具调用循环**：ReAct（决策 → 调用 → 观察 → 再决策）。新增工具：
  `time_query` / `geo_query` / `book_query` / `stance_compare` / `conflict_detect`
- **L3 多 Agent 编组**：主编 + 限书视角 Agent
- **L4 人在环路**：确认卡机制已具备，扩展到「分析结果标注证据 vs 推断」

---

## 2. 议题二：引入 DuckDB 作为 OLAP 底座

### 2.1 结论：不是替换 Kuzu，而是**图 + 表混合**

| 引擎 | 职责 | 理由 |
|---|---|---|
| **Kuzu** | 实体与关系的**图遍历**（邻域、子图、路径） | 图查询能力 DuckDB 不具备 |
| **DuckDB** | **分析型查询**：时间切片、空间过滤、全文检索、向量检索、聚合统计 | 列式 OLAP，扫描亿级行仍快；单文件、嵌入式 |

共享主键：`entity_id` / `book_id` / `chunk_id`。

### 2.2 单一真值源（避免双写不一致）

```
实体与关系  →  Kuzu 为真值源；DuckDB 是其分析副本（由 Kuzu 同步生成）
切块与原文  →  DuckDB 为真值源；Kuzu 不再存 Chunk（减负）
```

### 2.3 表结构草案

```sql
-- ODS 原始层：书籍与切块
CREATE TABLE books_raw (
    book_id VARCHAR PRIMARY KEY, title VARCHAR, author VARCHAR,
    level INT, stance VARCHAR, author_context VARCHAR,
    dynasty_hint VARCHAR, span_start INT, span_end INT,
    calendar VARCHAR, char_count BIGINT, source_path VARCHAR,
    mounted BOOLEAN, ingested_at TIMESTAMP
);

CREATE TABLE chunks (
    book_id VARCHAR, idx BIGINT, text TEXT,
    char_start BIGINT, char_end BIGINT, token_count INT,
    PRIMARY KEY (book_id, idx)
);

-- 证据四维（见 §3）
CREATE TABLE evidence (
    chunk_id VARCHAR, book_id VARCHAR, idx BIGINT,
    about_entity VARCHAR, about_event VARCHAR,
    year INT, year_raw VARCHAR, calendar VARCHAR, date_confidence VARCHAR,
    place_id VARCHAR, lon DOUBLE, lat DOUBLE,
    stance VARCHAR, confidence DOUBLE
);

-- DWD 明细层：实体（由 Kuzu 同步）
CREATE TABLE entities (
    entity_id VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR,
    year INT, dynasty_id VARCHAR, place_id VARCHAR,
    lon DOUBLE, lat DOUBLE,          -- 缺失必须为 NULL，禁止填 0
    from_book VARCHAR, confidence DOUBLE
);

-- DWS 汇总层：时间轴 / 空间 / 视角聚合视图
CREATE VIEW v_timeline AS
    SELECT year, dynasty_id, count(*) AS n FROM entities WHERE year IS NOT NULL GROUP BY 1,2;
CREATE VIEW v_viewpoints AS
    SELECT about_entity, book_id, stance, count(*) AS n FROM evidence GROUP BY 1,2,3;
```

- 全文检索：DuckDB FTS 扩展
- 向量检索：DuckDB VSS 扩展（HNSW）；无扩展时保留 BM25 兜底

### 2.4 收益

1. **解掉 `save()` 全量重写的负担**：chunks 移出 Kuzu 后，Kuzu 只存实体与关系，重写量大幅下降
2. **27 部书原文不再常驻内存**
3. 时间 / 空间 / 聚合查询走 SQL，无需在 Python 列表里线性过滤
4. 可增量写入，不必每次全量重建

> 注意：DuckDB 同样是**单进程写**，与现有 Kuzu 约束一致，不改变「离线落库需停服」的前提。

---

## 3. 议题三：原始史书入库

### 3.1 入库内容

| 内容 | 存储 | 说明 |
|---|---|---|
| 原始全文 | `books_raw` + 外部文件 / Parquet | 保留原文，可回溯 |
| 切块 | `chunks` | 800 字 / 重叠 100 |
| 向量 | `chunk_vectors` 或 VSS 索引 | 有 embedding 才生成 |
| 书级年表 | `books_raw.calendar` + `book_eras` | 纪年体系 |
| 书级地名词表 | `book_gazetteer` | 解决同名词在不同书指代不同 |

### 3.2 证据四维（贯穿本方案的核心改造）

每条证据（Source / chunk）必须可定位在四个维度上：

| 维度 | 字段 | 有了之后能做什么 |
|---|---|---|
| 实体 | `about_entity` / `about_event` | 问「焚书坑儒」精确取到各书论此事的段落 |
| 时间 | `year` / `year_raw` / `calendar` | 沿时间轴对齐多书记载 |
| 空间 | `place_id` / `lon` / `lat` | 沿地图对齐；异名对照 |
| 立场 | `stance` | 区分实录 / 评论 / 考辩 / 文学演绎 |

**可靠 = 四维定位 + 可溯源 + 层级/置信度/冲突标注**
**综合 = 沿任一维度聚合 + 跨维度切片 + 冲突识别**

---

## 4. 数据分层（数仓视角）

| 层 | 内容 | 载体 |
|---|---|---|
| ODS 原始层 | 书籍全文、切块、原始纪年与地名 | DuckDB `books_raw` / `chunks` |
| DWD 明细层 | 抽取出的实体、事件、关系、Claim（带 `from_book`/`confidence`） | Kuzu（真值）+ DuckDB（副本） |
| DWS 汇总层 | 时间切片、空间聚合、视角聚合 | DuckDB 视图 |
| ADS 应用层 | 时间轴 / 地图 / 图谱三视图 | API 投影 |

---

## 5. 实施步骤

### Phase 0 · 准备
- 确定 LLM 供应商与 API key（OpenAI / DeepSeek / 通义 / 本地 Ollama；`OPENAI_BASE_URL` 已支持切换）
- 本文作为基线文档；建立回归基线（当前 `/api/ontology` 统计）
- **验收**：`/api/health` 返回 `llm=openai`

### Phase 1 · DuckDB 引入（减负）
- 加 `duckdb` 依赖；建 `data/warehouse.duckdb`
- 建 `books_raw` / `chunks` 表；迁移现有 chunks（从 Kuzu `Chunk` 表导出）
- Kuzu 移除 `Chunk` 表；RAG 检索改走 DuckDB FTS / VSS，保留 BM25 兜底
- **验收**：内存占用下降；检索召回率不低于 BM25；27 部书数据无损

### Phase 2 · 时间与空间索引（修 P4 / P5）
- DuckDB 建 `entities` 表 + year / place 索引
- 新增按时间窗、bbox 的查询接口
- **修 `lon/lat` 填 0 的 bug**：缺失一律 `NULL`，地图层跳过
- **修 `dynasty_id` 全书统一**：改为按事件年份推导
- **验收**：地图无几内亚湾聚集；《通鉴》事件朝代归属正确

### Phase 3 · 证据四维
- `evidence` 表落 `about` / `time` / `place` / `stance`
- 抽取器产出四维字段
- **验收**：问「焚书坑儒」能按 `about` 精确取到多书段落

### Phase 4 · LLM 结构化抽取（修 P7）
- `OpenAILLM.extract(schema, text)` 用 structured output
- 抽 2–3 部书做样本，与启发式对比（字段完整率、幻觉率人工抽检）
- **验收**：样本抽取字段完整率显著高于启发式；事件名不再是句子碎片

### Phase 5 · 派生边与挂载链路（修 P1 / P2 / P3 / P6）
- 由 `dynasty_id` / `actors` / `place` 派生 `属于时期` / `参与` / `发生于` 边
- `commit_book` 的 link 分支支持 person / place
- 内置书 `align_mode` 由 `skip` 改 `link`
- 卸载前校验外部引用（有引用则拒绝或降级为解除引用）
- **验收**：秦始皇实体出现来自多书的来源；dangling 归零

### Phase 6 · Agent 编组
- 补 5 个查询工具；建史家 Agent（**以文本入库为前提**，见 §1.3）
- **验收**：单个史家 Agent 能限定书目作答且带引用

### Phase 7 · 多视角综合分析
- `Claim` 类型 + `conflicts` 冲突建模
- 主编 Agent：分组 → 分述 → 列分歧 → 归因
- **验收**：问「秦始皇」能给出多视角对比与分歧归因

### Phase 8 · 前端呈现
- 并排对比视图（同一实体各家说法）
- 时间轴 / 地图 / 图谱按统一查询接口联动

---

## 6. 风险与取舍

| 风险 | 应对 |
|---|---|
| 近现代著作版权 | 入库前逐部确认；优先公有领域；钱穆等仍在保护期者需授权 |
| LLM 成本（27 部书全量抽取） | 先样本验证（Phase 4）；小模型预筛 + 大模型精抽；按需抽取而非全量预抽 |
| 幻觉 | 强制引用、无证据明确标注、结构化输出校验、人工确认卡 |
| 双库一致性 | 明确单一真值源（实体→Kuzu，切块→DuckDB），DuckDB 侧为可重建副本 |
| 本体过度建模 | 类型克制；细节靠属性与 `kind` 限定语承载；核心层填满再扩扩展层 |
| 抽取质量不足支撑细本体 | 本体详细度与 LLM 能力同步推进；无真 LLM 时不横向扩类型 |

---

## 7. 决策记录（已定）

| # | 事项 | 决策 |
|---|---|---|
| 1 | LLM 供应商 | `backend/.env` 已配置：Sensenova 兼容协议（`https://token.sensenova.cn/v1`），实测可用 |
| 2 | 史家 Agent 名单 | 见 §7.1 |
| 3 | 抽取范围 | **先样本**：3 部体裁不同的书，验证后再决定是否全量 |
| 4 | 本体详细度 | **激进版**：18 个 Object Types，见附录 A |
| 5 | 外部标准对齐 | **对齐**：`external_ids`（Wikidata / CBDB / CHGIS / VIAF） |

### 7.1 史家 Agent 名单（含版权取舍）

中国著作权保护期为作者终生 + 50 年。据此：

| 史家 | 生卒 | 公有领域 | 首批 |
|---|---|---|---|
| 王国维 | 1877–1927 | ✅ 1978 起 | ✅ 首批 |
| 陈寅恪 | 1890–1969 | ✅ 2020 起 | ✅ 首批 |
| 陈垣 | 1880–1971 | ✅ 2022 起 | ✅ 首批 |
| 吕思勉 | 1884–1957 | ✅ 2008 起 | ✅ 首批（补位） |
| 傅斯年 | 1896–1950 | ✅ 2001 起 | 备选 |
| 顾颉刚 | 1893–1980 | ❌ 2031 起 | 暂缓 |
| 钱穆 | 1895–1990 | ❌ 2041 起 | **暂缓**（需授权） |

**决定：首批四大 = 王国维、陈寅恪、陈垣、吕思勉。** 钱穆因仍在保护期暂不入库，
其观点可经由主干种子中已有的钱穆《秦汉史》引文（L2 史料）间接体现，并标注来源。

> ⚠ 版权判断依据「作者终生+50年」，入库前仍需逐部核实具体版本与出版社权利。

### 7.2 LLM 选型（实测）

**当前可用模型**（`client.models.list()` 实测，共 9 个）：

```
deepseek-v4-flash, glm-5.2, sensenova-u1-fast, sensenova-6.8-flash-lite,
sensenova-u1.5-lite, deepseek-v4-pro, kimi-k3, deepseek-flash, deepseek-v4.1-flash
```

| 用途 | 配置 | 说明 |
|---|---|---|
| 轻量任务（当前） | `sensenova-6.8-flash-lite` | 已配置 |
| 结构化抽取（量大） | `deepseek-flash` / `deepseek-v4-flash` | 便宜 |
| 综合分析（要求高） | `deepseek-v4-pro` / `kimi-k3` | 强推理，用于多视角对比与归因 |

**⚠ 关键限制：该服务不提供 embedding 模型**（`text-embedding-3-small` 实测 404）。
因此：

- 向量检索不可用 → RAG 走 **BM25 / DuckDB 全文检索**
- 代码中 `embed_fn` 相关路径保持「有则向量、无则 BM25」的回退逻辑，不写死依赖
- 若将来接入带 embedding 的服务，改 `.env` 即可启用，无需改代码

---

## 附录 A：激进版 Object Types（18 类）

### A.1 核心层（8 类，必须填满）

| 类型 | 说明 | 状态 |
|---|---|---|
| `Event` 事件 | 时间、地点、参与者、意义、分歧 | 已有，需扩字段 |
| `Person` 人物 | 生卒、籍贯、字号谥号、学派 | 已有，需扩字段 |
| `Place` 地点 | 古今名、坐标、别名、层级、时效 | 已有，**仅 9 个需扩充** |
| `TimeSpan` 时期 | 朝代 / 年号 / 在位期 / 习惯分期 | **新增** |
| `Dynasty` 政权 | 起止、都城、疆域图 | 已有（可视为 `TimeSpan` 的 kind=朝代） |
| `Book` 书籍 | 可挂载书源，含 stance / time / geo 三件套 | 已有，需扩展 |
| `Source` 史料 | 层级、原文、置信度、四维坐标 | 已有，需加 about/time/place/stance |
| `Claim` 论断 | 史家评论，含 about / holder / conflicts | **新增** |

### A.2 扩展层（10 类，允许稀疏）

| 类型 | 说明 | 状态 |
|---|---|---|
| `Institution` 机构 | 行政/军事机构（org） | 已有 |
| `Office` 官职 | 官职/差遣（从 Institution 拆出以便精细建模） | 已有（以 `kind=post` 表达） |
| `Artifact` 文物 | 器物、简牍、碑刻 | 已有 |
| `Concept` 观念 | 制度/思想/术语 | 已有 |
| `Document` 文献 | 奏疏 / 诏令 / 书信 / 碑文 | **新增** |
| `Work` 著作 | 某人所撰的著作（区别于物理书籍 `Book`） | **新增** |
| `Lineage` 宗族世系 | 家族、谱系 | **新增** |
| `EthnicGroup` 族群 | 民族、部族 | **新增** |
| `School` 学派 | 学术流派、思想谱系 | **新增** |
| `AdministrativeUnit` 行政区划 | 州郡道府县（对齐 CHGIS） | **新增** |

> 战役 / 盟约 / 改制 / 灾异等**不另立类型**，由 `Event.kind` 承载，避免类型爆炸。

### A.3 Link Types 扩展原则

- 高频、需被遍历推理的核心关系保留独立类型：`参与`、`发生于`、`属于时期`、`触发`、
  `奠定`、`书载观点`、`继承自`、`并存于`、`矛盾于`
- 长尾关系用**通用关系 + 限定语**：
  `(Person)-[:关联 {kind:"父子"|"师生"|"政敌"|"举荐"|"姻亲"}]->(Person)`
  1 个类型覆盖数十种关系，查询按 `kind` 过滤
- 细分维度一律用 `kind` 字段，不另起类型

### A.4 外部标准对齐

```python
external_ids: dict = {
    "wikidata": "Q7191",    # Wikidata 实体
    "cbdb":     "12345",    # 中国历代人物传记资料库
    "chgis":    "PLACE_ID", # 中国历史地理信息系统
    "viaf":     "...",      # 虚拟国际权威档
}
```

所有核心类型均带此字段（可选、可空），为将来与外部知识库融合留口子。

---

## 附录 B：样本书选择（Phase 4）

选 3 部**体裁不同**的书，验证抽取器对不同体裁的适应性，同时正好对应三个视角 Agent：

| 书 | 体裁 | 对应视角 | 验证点 |
|---|---|---|---|
| 《史记》 | 纪传体 | 司马迁视角 | 人物抽取、纪年换算 |
| 《资治通鉴》 | 编年体 | 司马光视角 | 事件序列、跨朝代归属 |
| 《读通鉴论》 | 史评 | 王夫之视角 | `Claim` 抽取、评论指向（about） |

验证指标：字段完整率、事件名合理性（是否仍出现句子碎片）、幻觉率（人工抽检）、
单书耗时与 token 成本。样本通过后再决定是否全量重抽其余 24 部。
