# 数据血缘与构建手册（本体 · 图谱 · RAG · 界面 · Agent）

> 定位：一篇讲清「数据从哪来、变成什么、存在哪、进不进版本库、后续怎么继续构建、界面和 Agent 怎么用」。
> 配套：`docs/PLAN.md`（总体方案）、`docs/BUILD.md`（逐步构建操作）、`docs/ONTOLOGY_ALIGN.md`（外部本体对齐与闸门）。

---

## 0. 一句话概览

**Kuzu 图库存「是什么」（实体与语义关系，真值源），DuckDB 数仓存「怎么来的」（原文切块、LLM 抽取结果、进度台账）与检索副本；权威源（CBDB/CHGIS/Wikidata/CIDOC/OWL-Time）是主干，书籍（《史记》《二十四史》《资治通鉴》…）是挂在主干上的扩展知识。**

---

## 1. 数据血缘总图

```
┌──────────────────────── 源头层（source） ────────────────────────┐
│ 内置史书原文      app/data/books/*.txt        27 本  71.9 MB  ✅入库 │
│ 地图瓦片          app/data/tiles/**.png   181,133 张 1022.8 MB ✅入库 │
│ 主干权威源        app/data/core/raw/**          857.8 MB  ❌不入（超限，脚本重建）│
│   ├ CBDB sqlite 837 MB（535,181 人物 / 82 朝代 / 33,957 官职）        │
│   ├ CHGIS MySQL dump 16 MB（122,075 历史地名）                        │
│   ├ Wikidata SPARQL 3.6 MB（12,403 人物锚点）                         │
│   └ CIDOC CRM OWL 0.8 MB / OWL-Time RDF 0.1 MB（本体词汇，仅对齐用） │
│ 本地嵌入模型      models/…/model_optimized.onnx   94.8 MB      ✅入库 │
└────────────────────────────────────────────────────────────────┘
            │ fetch_core.py            │ extract_core.py        │ parse/chunk
            ▼                          ▼                        ▼
┌──────────────────────── 派生层（derived） ────────────────────────┐
│ 主干精简表   app/data/core/normalized/*.jsonl   87 MB      ✅入库   │
│   ├ cbdb_persons_001..004.jsonl   535,180 人                       │
│   ├ chgis_places.jsonl            122,075 地名                     │
│   └ wikidata_anchors.jsonl         12,403 锚点（含 CBDB 交叉 403）  │
│ 切块原文     warehouse.chunks（含 embedding）        ❌不入（可重跑）│
│ LLM 抽取结果 warehouse.extract_raw（最贵，重跑要花钱）❌不入         │
└────────────────────────────────────────────────────────────────┘
            │ import_core.py（计划）   │ apply → mount（人工确认）
            ▼                          ▼
┌──────────────────────── 真值层（truth） ─────────────────────────┐
│ Kuzu 图库  app/data/kg/         658.7 MB  ❌不入（构建产物）        │
│   ├ 节点表 Dynasty 84 / Event 2934 / Person 2057 / Place 9 / …     │
│   └ 关系表 KGREL 4917 条（当前 rel 仅「书载观点」，语义边待补）      │
│ 书级产物   Book.extracts、merges.json、kg/dangling_edges.json       │
└────────────────────────────────────────────────────────────────┘
            │ _sync_entities_to_warehouse（每次 save 后）
            ▼
┌──────────────────────── 检索层（serving） ───────────────────────┐
│ DuckDB  app/data/warehouse/      518.8 MB  ❌不入                   │
│   ├ entities（Kuzu 实体的投影副本，供时间窗/空间窗快查）             │
│   ├ ingest_progress / book_runs（块级进度与书状态）                 │
│   └ ingest_failures / book_run_params（失败台账、参数指纹）          │
│ RAG：retrieve_chunks() = 向量检索（LocalEmbedder ONNX，512 维）      │
│      → 失败/无向量时回退 BM25                                       │
└────────────────────────────────────────────────────────────────┘
            ▼
┌──────────────────────── 消费层（consume） ───────────────────────┐
│ API：/api/timeline /events /search /entity /kg/* /graph/query      │
│      /maps /ontology /chat（含 grounding 接地校验）/actions /agents │
│ 前端：时间轴 · 地图 · 图谱 · 本体导航 · 右栏 · 书籍挂载 · Agent 管理 │
│ Agent：只读工具（图谱浏览 / RAG / 史料）＋ 写操作走确认卡，可 undo   │
└────────────────────────────────────────────────────────────────┘
```

### 入库 / 不入库总账

| 类别 | 目录 | 体积 | 入库 | 重建代价 |
|---|---|---|---|---|
| 原始·瓦片 | `app/data/tiles/` | 1022.8 MB | ✅ | 极高（抓取困难，必须入库） |
| 原始·书 | `app/data/books/` | 71.9 MB | ✅ | 高 |
| 原始·模型 | `models/` | 94.8 MB | ✅ | 中 |
| 原始·主干大件 | `app/data/core/raw/` | 857.8 MB | ❌（CBDB 单文件 837 MB > GitHub 100 MB 硬限） | 中：`fetch_core.py` 按 `manifest.json` 重下 |
| 派生·主干精简 | `app/data/core/normalized/` | 87 MB | ✅ | 低：`extract_core.py` |
| 构建·图库 | `app/data/kg/` | 658.7 MB | ❌ | 中高 |
| 构建·数仓 | `app/data/warehouse/` | 518.8 MB | ❌ | **高**（`extract_raw` 是 LLM 调用结果） |
| 过程产物 | `app/data/ingest/`、`library/` | 2 MB | ❌ | 低 |

---

## 2. 构建路径 A：本体主干（先立骨架）

主干 = 权威源，不来自任何一本书；书籍只是挂在它上面的证据与观点。

```bash
# 1) 抓取原始权威数据（CIDOC / OWL-Time / Wikidata / CHGIS / CBDB）
python scripts/fetch_core.py                    # 并发抓取，断点续传，写 manifest.json
python scripts/fetch_core.py --discover         # 只探测可达性

# 2) 派生精简表（统一字段，体积小、可 diff、入库）
python scripts/extract_core.py                  # → core/normalized/*.jsonl
python scripts/extract_core.py --only cbdb --limit 2000   # 试跑

# 3) 导入（计划中：scripts/import_core.py）
#    · 主干大表 → DuckDB（core_persons / core_places / core_anchors）
#    · 图谱只连「已对齐」的实体：Wikidata↔CBDB 锚点（403）+ 与已挂载书实体同名同期者
#    · 实体新增 external_ids{wikidata,cbdb,chgis} 与 origin=core|book
```

对齐规范见 `docs/ONTOLOGY_ALIGN.md`：Person→CIDOC `E21`、Event→`E5`、Place→`E53`、参与→`P11`；时间区间映射 OWL-Time `time:Interval` / `time:Instant`（已核实：OWL-Time 里没有 `TimeSpan`，那是 CIDOC `E52` 的叫法）；外部锚点统一放 `external_ids`。

> 现状提醒：KGREL 里 4917 条边当前全是「书载观点」，本体语义边（发动/败于/建立/出使…共 82 条）尚未进图库，其中 29 条因种子用拼音 id（`xiangyu`）而库里节点是中文名 id（项羽）判为悬空。修复脚本 `scripts/rebuild_seed_edges.py`（干跑 → `--apply`）。

---

## 3. 构建路径 B：知识图谱（每本书叠加）

```
书 txt → parse → chunk（800 字/重叠 100，句末断句）
       → extract（LLM；失败静默回退启发式，记入 ingest_failures）
       → normalize / align / score → proposals（提案）
       → 人工 apply → mounts/start → mounts/commit（挂载进 Kuzu）
```

- 离线（推荐，停服）：`python scripts/ingest_one.py <book> --llm --repair --max-rounds 3 --strict --retry-rounds N --reset-failed --no-store`
- 在线：`POST /api/ingest/{book_id}`（后台单任务，409 表示已有任务在跑）→ `/api/ingest/{id}/apply` → `/api/mounts/start`（看 `warnings`）→ `/api/mounts/commit`
- 队列与进度：`/api/books/queue`、`/api/books`（带 `ingest` 与 `integrity`）

**可靠性与完整性保障**（已实现，详见 `docs/BUILD.md` §3.6）
单组隔离重试 · 失败台账（error/empty/fallback 三类）· 缺口计划（missing/no_raw/empty/fallback）· `--repair` 多轮补齐 · 收口合并全量 raw 不按 limit 裁剪 · **先落 raw 再记进度** · 产物保护（`keep_best`，新提案少于旧产物不覆盖）· 参数指纹（改参数自动作废旧进度）· Ctrl+C 先收口再退出 · `--strict` 不完整退出码 2 · 状态保护（小样本不冲掉已完成状态）。

**五道闸门**
- G1 模式白名单：写边必须过 `REL_PAIRS`（`store.add_edge`）
- G2 证据绑定：实体/事件必须挂 `Source` 引文
- G3 接地校验：`/api/chat` 返回 `grounding`，年份/专名必须在原文出现
- G4 分歧并列：同一事件多书记载并列 + `dispute` 注记，不覆盖
- G5 外部锚点：`external_ids`（wikidata/cbdb/chgis/tgn/geonames）与权威源比对；冲突进 `needs_review`

---

## 4. 构建路径 C：RAG（让回答能溯源）

| 环节 | 实现 | 位置 |
|---|---|---|
| 切块 | `chunk_text()` 800 字 / 重叠 100，句末断句 | `app/ingestion/rag.py:16` |
| 分词 | `tokenize()`（BM25 用） | `app/ingestion/rag.py:49` |
| 嵌入 | `LocalEmbedder`（ONNX `bge-small-zh-v1.5`，512 维，本地离线）→ 云端兜底 | `app/llm/local_embed.py:26`、`app/llm/base.py:128` |
| 向量存放 | **DuckDB `chunks.embedding`**（Kuzu 不再存 Chunk） | `app/warehouse.py:49` |
| 检索 | `retrieve_chunks()`：向量 `vector_search()`（余弦）→ 无向量/异常回退 BM25 | `app/ingestion/rag.py:136/121/63` |
| 接入问答 | `retrieve_node()` 把命中块转成 `Source` 证据；`verify/narrate/validate/review` 节点强制引用编号 `[n]` | `app/graph/nodes.py:43-162` |
| 对外 | `/api/chat` 返回 `answer` + `evidence` + `grounding` + `warnings` | `app/api/routes.py:532-576` |

自检：`python scripts/selftest_ingest.py`（解析与 RAG 冒烟）。

---

## 5. 每加一本书的标准流程（叠加，不重建）

```bash
# ① 停服，离线抽取（可断点续跑、可多轮补齐）
python scripts/ingest_one.py 资治通鉴 --llm --repair --max-rounds 3 --strict

# ② 启服，接回线上并挂载（人工确认）
POST /api/ingest/资治通鉴/apply
POST /api/mounts/start      # 看 warnings：coverage < 100% 会提示
POST /api/mounts/commit

# ③ 可选：新实体与主干对齐（产出 external_ids 候选 → 人工确认 merges）
python scripts/import_core.py --link

# ④ 验收
python scripts/e2e_flow.py --book 资治通鉴            # 只读冒烟
python scripts/e2e_flow.py --book 资治通鉴 --chat     # 连对话与接地一起验
```

事件在 `commit_book` 时按年份用 `dynasty_at(year)` 自动归朝，因此《资治通鉴》这类跨十余朝的通史也能正确落位。

---

## 6. 界面怎么用

主壳 `frontend/src/App.tsx` 负责编排，UIAction 执行器把后端动作回放成界面变化。

| 面板 | 文件 | 用法 |
|---|---|---|
| 时间轴 | `components/Timeline.tsx` | 按年份浏览事件；车道展示并立政权；点事件开右栏详情 |
| 地图 | `MapView.tsx`、`MapGallery.tsx` | 按朝代切底图（18.1 万张本地瓦片），叠加事件地点 |
| 图谱 | `GraphExplorer.tsx` | 浏览实体与语义边，做子图展开 |
| 本体导航 | `OntologyNav.tsx` | 按本体类型（朝代/事件/人物/地点…）浏览主干 |
| 右栏 | `RightDock.tsx`（`onto/detail/source/books/agents/arch/ask`）+ `SidePanel.tsx` | 详情、引文原文、书籍、Agent、架构图、问答 |
| 书籍挂载 | `BookMount.tsx` | 上传/入库/抽取/应用/挂载全流程 |
| Agent 管理 | `AgentManager.tsx` | 注册 Agent、授权工具、查看待审批确认卡 |

服务启动：`powershell -File scripts/serve.ps1`（后端 8000；加 `-Frontend` 拉前端 5173）。

---

## 7. Agent 怎么用

- 注册与授权：`/api/agents*`（`app/api/routes.py:1088-1141`）；运行时 `app/agents/runtime.py:run_agent():197`
- 只读工具：图谱浏览、RAG 检索、史料读取（`_read_context():128`）
- **写操作不直接落地**：LLM 在【OPS】段提出写意图（`_split_ops():258`）→ 生成确认卡 `pending_ops`（`app/data/pending_ops.json`）
- 审批执行：`POST /api/operations`（confirm/cancel，`routes.py:1145`）→ `confirm_operation():298` 复核「Agent 仍存在 / 当前授权 / 目标存在」后才执行（`_EXECUTORS`：mount/unmount/delete，`:332-375`），并写 `action_log`
- 撤销：`/api/actions` 与 `POST /api/actions/{seq}/undo`（`routes.py:1023-1030`），动作类型与逆动作定义见 `app/ontology/actions.py:41/67`

三者区别别混淆：
- `proposals` = 入库抽取出来的**史料提案**（需逐项确认后挂载）
- `pending_ops` = **Agent** 写操作的待审批确认卡
- `actions` = UI 动作序列（可单步 undo）

---

## 8. 备份与恢复（保护「不入库但很贵」的数据）

```bash
python scripts/backup_data.py           # 备份 kg/ + warehouse/ + core 元数据 → _backup/<时间戳>/
python scripts/backup_data.py --list    # 列出现有备份（默认保留最近 3 份）
```
恢复：停服 → 用备份覆盖 `app/data/kg/`、`app/data/warehouse/` → 启服 → `e2e_flow.py` 验收。
建议**停服后**备份（Kuzu/DuckDB 单写者，运行中复制可能拿到中间状态）。

---

## 9. 关键文件索引

| 主题 | 位置 |
|---|---|
| 图库真值源 | `backend/app/store.py` |
| 数仓与进度/台账 | `backend/app/warehouse.py` |
| 本体模型与关系目录 | `backend/app/schema/ontology.py`（`REL_TYPES` / `REL_PAIRS`） |
| 单本编排与完整性 | `backend/app/ingestion/bookrun.py` |
| 抽取管线 | `backend/app/ingestion/pipeline.py` / `extract.py` / `rag.py` |
| 问答流水线（LangGraph） | `backend/app/graph/nodes.py` |
| 接地校验 / 动作与撤销 | `backend/app/ontology/verify.py` / `actions.py` |
| 主干抓取 / 派生 / 备份 | `backend/scripts/fetch_core.py` / `extract_core.py` / `backup_data.py` |
| 端到端冒烟 / 语义边修复 | `backend/scripts/e2e_flow.py` / `rebuild_seed_edges.py` |
