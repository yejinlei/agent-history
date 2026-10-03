# 外部本体对齐与「反幻觉」闸门

> 目标：引入本体技术栈**不是为了让图谱更好看**，而是为了让 Agent 的推理
> 有可判定的约束，从而把幻觉从"靠模型自觉"变成"靠机制拦住"。
> 参考清单：ozekik/awesome-ontology（本体编辑器 / 工具 / 存储 / 语言 / 上层与中层本体）。

## 0. 幻觉的四种来源 → 对应的闸门

| 幻觉类型 | 闸门 | 机制 | 落点 |
|---|---|---|---|
| 凭空造实体、造关系 | **G1 模式闸门** | 只允许本体声明的 18 类 Object Type；关系端点组合必须在 `REL_PAIRS` 白名单内，越界直接 `ValueError` | `store.add_edge`（已实现） |
| 编造年份、地点 | **G2 归一化闸门** | 纪年由 `emperors.json` 年号表换算、地名由 `places` 表解析，**不交给 LLM 生成数值**；坐标缺失一律 `None`（禁止 0.0） | `ingestion/extract.py` `resolve.py` |
| 无据陈述（说得顺溜但无出处） | **G3 证据 + 接地闸门** | 每条 Claim 必带 `from_book` + 原文片段；回答生成后跑 `grounding_report`，未接地的年份/专名标红 | `ontology/verify.py`（已实现），接入 `/api/chat` |
| 用一家之言冒充定论 | **G4 分歧闸门** | 同一 `about` 上多书说法不一致 → `detect_disputes` 并列呈现，不做二选一 | `ontology/verify.py` |
| 与公认事实冲突 | **G5 外部锚点闸门** | `external_ids` 对齐 Wikidata/CBDB/CHGIS 等；外部事实与抽取冲突 → 标记 `needs_review` | 待落 `external_ids` 字段 |

一句话：**能进图的必须合模式，能进答案的必须有出处，有分歧的必须并列。**

## 1. 对齐目标（按 awesome-ontology 的分类取用）

### Ontologies and Vocabularies（直接对齐）

| 项目 | 用途 | 映射到本项目 | external_ids 键 |
|---|---|---|---|
| **CIDOC CRM**（ISO 21127，文化遗产事实模型） | 关系语义的"标准说法"；成熟推理规则可复用 | Person→E21、Event→E5、Place→E53、TimeSpan→E52、Book/Source→E31、参与→P11、发生于→P7、时间→P4 | — |
| **OWL-Time** | 时间区间与 before/after/overlaps | TimeSpan、时间轴"同期"推理 | — |
| **Wikidata / Wikibase** | 通用锚点，跨源消歧与事实核对 | 所有类型都可挂 QID | `wikidata` |
| **CBDB**（中国历代人物传记资料库） | 人物生卒、官职、亲属、入仕 | Person | `cbdb` |
| **CHGIS**（中国历史地理信息系统） | 历史地名与沿革、坐标 | Place | `chgis` |
| **Getty TGN / GeoNames** | 地名权威标识与现代坐标 | Place | `tgn` / `geonames` |
| **PeriodO** | 历史时期（朝代/年号区间）的权威定义 | Dynasty / TimeSpan | `periodo` |
| **BIBFRAME / schema.org** | 书目与著作元数据 | Book / Source | `bibframe` |

### Upper-level Ontologies（只做"薄映射"，不照搬）

BFO / DOLCE / gist / SUMO 这类上层本体有数百个类，直接引入会让本体膨胀、
推理成本上升。做法：仅在需要跨领域互换时，把我们的 18 类映射到上层类
（Event→CRM E5 / BFO occurrent，Person→E21 / BFO object），
**映射表写在文档里，不进主 schema**。

### Utilities / Languages / Datastore（工具链）

| 类别 | 取用 | 说明 |
|---|---|---|
| Languages | **SHACL / ShEx** | 把 G1（关系白名单）、G2（必带证据）写成 shape，可对外导出与自动校验 |
| Languages | OWL / RDF / SPARQL | 需要 OWL 推理机时，把 Kuzu 图导出 RDF 再装载 |
| Editors | Protégé / EDDY | 离线维护 L1 schema、查看类层级 |
| Utilities | ROBOT / OOPS! | 本体构建与质量检查 |
| Datastore | Oxigraph / GraphDB / Virtuoso | 若要跑 SPARQL 与 OWL 推理，可选其一做只读副本 |

## 2. external_ids 规范

```json
"external_ids": {
  "wikidata": "Q7185",
  "cbdb": "0012345",
  "chgis": "...",
  "tgn": "...",
  "periodo": "..."
}
```

- 键必须在白名单内：`wikidata / cbdb / chgis / tgn / geonames / periodo / viaf / bibframe / wikisource`
- 值为非空字符串；未知键一律丢弃（`normalize_external_ids`）
- 同一实体的多个外部标识可以并存，冲突时以「外部事实 vs 抽取结果 → `needs_review`」的方式暴露，不静默覆盖

## 3. 落地顺序

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | G3 接地校验 + 接入 `/api/chat` | ✅ 已实现（`app/ontology/verify.py`） |
| P1 | 抽取产物强制带 `chunk_idx` + 原文引用（G2 证据闸门完整化） | 进行中（逐本编排器 `bookrun.py`） |
| P2 | `external_ids` 字段落到 Event/Person/Place + 导入脚本 | 待做 |
| P3 | CIDOC CRM 映射表 + SHACL shape 导出 | 待做 |
| P4 | G5：用 Wikidata/CBDB 做冲突核对与 `needs_review` 队列 | 待做 |
