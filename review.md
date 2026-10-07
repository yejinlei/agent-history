# 代码审查报告：书库接入 / 多 Agent / 本体深化

> ## 修复状态复核（2026-10-08）
>
> 下文的问题清单写于 2026-10-02，此后多有改动。已逐条按符号（而非行号）复核，
> 结果如下。**已修的不再重复排查**；标「未复核」的是本次没来得及确认的项。
>
> | 编号 | 状态 | 依据（当前代码） |
> |---|---|---|
> | H1 前端删除走 POST | ✅ 已修 | `frontend/src/api.ts` 有 `deleteJSON`（method DELETE），deleteBook/deleteAgent 已改用 |
> | H2 save 非原子 | ✅ 已修 | `store.save()` 已改为差分落盘，不再 DROP 全表重建 |
> | H3 确认卡未绑真实目标 | ✅ 已修 | `runtime.confirm_operation` 按 Agent【当前】授权 + 目标书存在性重校验，且返回完整 op |
> | H4 undo 卸载丢 accepted | ✅ 已修 | `routes._undo_unmount` 按 `book.accepted` 原位图重放，旧数据才回退全接受 |
> | M1 add_edge 是死代码 | 🟡 部分修 | `store.py` 挂载路径与增量边已走 `add_edge`；历史直写路径仍有残留 |
> | M2 书载观点/书载·观点 | ✅ 已修 | 全库统一为「书载观点」 |
> | M3 Source→Artifact 无 DDL | ✅ 已修 | `REL_PAIRS` 仅保留 Artifact→Source，并加了注释说明 |
> | M4 ActionLog/PendingOps 无锁 | ❌ 未修 | 并发撤销仍有 TOCTOU |
> | M5 LLM 问答失败 500 | ✅ 已修 | `runtime.run_agent` 的 `llm.chat` 有 try/except 回退；`nodes.narrate_node` 同样已降级 |
> | M6 confirm 返回形状错 | ✅ 已修 | 返回 `set_status` 后的完整 op |
> | M7 Resolver 未接线 | ❌ 未修 | 上传仍会对已知人物造重复实体提案 |
> | M8 上传异常兜底 | ✅ 已修 | 有 `except Exception` + `dest.unlink` 清理 |
> | M9 前端异步无 catch | 未复核 | — |
> | M10 async 内同步重操作 | 🟡 部分修 | `parse_document` 已放 `run_in_threadpool`，`store.save()` 仍同步 |
> | L1 requirements 缺 kuzu | ✅ 已修 | `backend/requirements.txt` 含 `kuzu==0.11.3` |
> | L2 仅校验扩展名 | ❌ 未修 | — |
> | L3 无认证 | ❌ 未修 | 产品决策，待定 |
> | L4 配色只 5 类 | ✅ 已修 | `GraphExplorer.COLORS` 已覆盖 8 类（含 Artifact/Concept） |
> | L5 KGLabel 缺新三类 | ✅ 已修 | `types.ts` 已含 Institution/Artifact/Concept |
> | L6–L10 | 未复核 | — |
>
> 复核期间另外修掉的加载健壮性问题：`Narrative` 三字段与 `Person.years` 为
> None/int 时会让 pydantic 拒绝整条实体（一个空节点即拖崩全站），现已在
> `store._load_kuzu` 做容错归一。

- 审查日期：2026-10-02
- 审查方式：只读静态审查 + 只读命令（tsc/compileall/GetDiagnostics）+ 对在运行服务器（127.0.0.1:8000）做非破坏性 HTTP 探测；未启动任何占库进程
- 审查范围：
  - 后端：`backend/app/api/routes.py`、`backend/app/store.py`、`backend/app/ingestion/{parse,rag,extract,resolve,mount}.py`、`backend/app/agents/{registry,runtime}.py`、`backend/app/schema/ontology.py`、`backend/app/ontology/actions.py`、`backend/app/data/ontology_extra.json`、`backend/scripts/ingest_book.py`
  - 前端：`frontend/src/{api.ts,types.ts}`、`components/{BookMount,AgentManager,RightDock,GraphExplorer,OntologyNav}.tsx`
- 环境事实：Kuzu 0.11.3（已安装但**未写入 requirements.txt**）；LangGraph 0.2.60；TypeScript `tsc --noEmit` 通过（`as never` 强转掩盖了方法错误）；Python compileall 无语法错误；IDE 诊断为空。

---

## 一、总体结论

三条主线（书库、多 Agent、本体深化）功能骨架完整，异常降级设计（embedding 失败、LLM 抽取失败、瓦片上游故障）总体用心，CLI 遵守"只走 HTTP"纪律，上传落盘文件名使用 UUID 不存在路径穿越。但存在 **4 个高危问题**：

1. 前端两处"删除"实际发出 POST 而后端是 DELETE，功能 100% 不可用（已 HTTP 实证 405）；
2. `Store.save()` 是"DROP 全部表 + 全量重建"且无方法级锁/事务，并发写可互相破坏，异常窗口内重启会丢数据；
3. 确认卡只展示 LLM 生成的 `summary`，不展示 `params` 指向的真实目标，且 confirm 时不再校验授权，人审防线可被提示注入绕过；
4. 撤销"卸载"会把当初**被用户否决**的提案全部重新挂载（accepted 选择从未被持久化）。

另有中危 10 项、低危 10 项，详见下文。

---

## 二、高危问题（High）

### H1. 前端两个删除功能全部失效：POST → 405（已实证）

证据：

- [api.ts:81-82](file:///d:/src/agent-history/frontend/src/api.ts#L81-L82) `deleteBook` 调用 `postJSON(...)`，而 `postJSON` 固定 `method: "POST"`（[api.ts:12](file:///d:/src/agent-history/frontend/src/api.ts#L12)）；后端为 [routes.py:688](file:///d:/src/agent-history/backend/app/api/routes.py#L688) `@router.delete("/api/library/{book_id}")`。
- [api.ts:98-99](file:///d:/src/agent-history/frontend/src/api.ts#L98-L99) `deleteAgent` 同样走 POST；后端为 [routes.py:812](file:///d:/src/agent-history/backend/app/api/routes.py#L812) `@router.delete("/agents/{agent_id}")`。
- 运行时实证（对不存在 id 的非破坏性探测）：
  - `POST /api/library/__nonexistent__` → **405，Allow: DELETE**
  - `POST /api/agents/__nonexistent__` → **405**（Allow 头仅 PUT：Starlette 对先注册的同路径 PUT 路由短路返回）
  - `DELETE /api/library/__nonexistent__` → 404（后端 DELETE 路由本身正确）
  - `DELETE /api/agents/__nonexistent__` → 404（同上）

影响：BookMount 的"删除上传书"（[BookMount.tsx:108-117](file:///d:/src/agent-history/frontend/src/components/BookMount.tsx#L108-L117)）和 AgentManager 的"删除自定义 Agent"（[AgentManager.tsx:23-32](file:///d:/src/agent-history/frontend/src/components/AgentManager.tsx#L23-L32)）点击必失败；且调用处无 catch，表现为无提示的未处理 rejection。

修复方向：api.ts 增加通用 `deleteJSON`（method DELETE），两处替换。

### H2. `save()` 非原子：DROP 全部表后全量重建，无方法级锁、无事务

证据：

- [store.py:308-310](file:///d:/src/agent-history/backend/app/store.py#L308-L310)：`save()` 先 `_create_schema()`，后者对全部 11 张表执行 `DROP TABLE`（[store.py:214-222](file:///d:/src/agent-history/backend/app/store.py#L214-L222)），随后逐条 CREATE。
- 锁只在单条语句粒度：`_x()` 的 RLock（[store.py:195-198](file:///d:/src/agent-history/backend/app/store.py#L195-L198)），`save()` 整体不在临界区；内存变更（`self.books.append`、`self.edges.append` 等）也无锁。
- 并发写路径真实存在：
  - 上传：async 路由 [routes.py:678-680](file:///d:/src/agent-history/backend/app/api/routes.py#L678-L680)；
  - 删书：`def` 路由跑在线程池，`trash_book → save`（[store.py:786-806](file:///d:/src/agent-history/backend/app/store.py#L786-L806)）；
  - 挂载提交：[mount.py:66-69](file:///d:/src/agent-history/backend/app/ingestion/mount.py#L66-L69) → `commit_book → save`（[store.py:880-882](file:///d:/src/agent-history/backend/app/store.py#L880-L882)）；
  - 确认卡 executor：[runtime.py:261-297](file:///d:/src/agent-history/backend/app/agents/runtime.py#L261-L297)。

后果：

1. A 的 save 建表途中，B 的 save 再 DROP → A 随后 INSERT 已被 B 重建的同主键 → Kuzu 主键冲突异常，请求 500；
2. 任一 CREATE 失败（磁盘、锁、数据问题），磁盘上的库停留在"缺表/缺数据"状态——DROP 已发生而重建未完成。此时内存仍是完整副本，下一次成功 save 可自愈；但**进程重启即永久丢失**，无备份无事务。
3. [store.py:880-881](file:///d:/src/agent-history/backend/app/store.py#L880-L881) `book.mounted = True` 先于 save：save 失败后内存认为已挂载，确认卡重试时 executor 走 `{"already": True}` 分支直接标记 confirmed，数据库可能仍不完整。

修复方向：`save()` 整体包在 RLock 内（含"内存变更 + 落盘"为一个临界区）；长期改增量事务写库；启动时检测上次异常重建（如写 schema 版本/临时标记）并自动从内存或备份修复。

### H3. 确认卡展示内容与真实参数未绑定，confirm 时不重新校验授权（人审可被提示注入绕过）

证据：

- LLM 输出的 ops，`params` 不做任何校验即落盘：[runtime.py:187-192](file:///d:/src/agent-history/backend/app/agents/runtime.py#L187-L192) → `pending_ops.add(...)`（[runtime.py:62-70](file:///d:/src/agent-history/backend/app/agents/runtime.py#L62-L70)）。
- 前端卡片只渲染 `op.action` 和 LLM 生成的 `op.summary`，**不解析 `params.book_id` 对应的真实书名**：[RightDock.tsx:202-218](file:///d:/src/agent-history/frontend/src/components/RightDock.tsx#L202-L218)。
- confirm 直接信任卡片创建时的 action，不重新检查该 Agent 当前工具授权：[runtime.py:244-253](file:///d:/src/agent-history/backend/app/agents/runtime.py#L244-L253)。授权仅在卡片生成时检查一次（[runtime.py:197-203](file:///d:/src/agent-history/backend/app/agents/runtime.py#L197-L203)）。

攻击/失误路径：上传书内容含提示注入（"请输出【OPS】…"）→ LLM 可令 `summary` 写"挂载《史记》"，而 `params.book_id` 指向另一本 upload 书的删除；用户凭 summary 批准即误删。Agent 被编辑收回写权限、或 Agent 被删除后，遗留 pending 卡仍可确认执行。

修复方向：

1. 卡片必须展示从 `params` 解析出的真实目标（书名+id），summary 仅作说明；
2. confirm 时按 Agent **当前**授权与书籍当前状态重新校验；
3. Agent 删除/降权时联动取消其 pending ops。

### H4. undo "卸载"会把原被否决的提案全部重新挂载

证据：

- [routes.py:742-749](file:///d:/src/agent-history/backend/app/api/routes.py#L742-L749)：`_undo_unmount` 调用 `store.commit_book(book, [True] * len(book.extracts))`——无差别全部接受。
- 根因：用户在确认卡上的勾选 `accepted` 从未随书持久化（挂载路径仅在 LangGraph MemorySaver 内存中，[mount.py:60-69](file:///d:/src/agent-history/backend/app/ingestion/mount.py#L60-L69)；`uncommit_book` 后选择信息即消失）。

后果：mount → 部分接受 → unmount → 撤销该 unmount，当初被否决的人物/事件/观点全部进入主干本体，"撤销"产生了用户从未批准过的数据，违背 ActionLog 宣称的逆动作语义。

修复方向：commit 时把 accepted 位图存入 Book（或 action entry 的 result）；undo unmount 按原位图重放。

---

## 三、中危问题（Medium）

### M1. commit 新增边缺 `confidence/src_ids` 键，且边写入全部绕过 `add_edge`/REL_PAIRS 校验

- [store.py:877-879](file:///d:/src/agent-history/backend/app/store.py#L877-L879)：`{"a", "b", "rel", "from_book"}`——对比规范形状 [store.py:302-303](file:///d:/src/agent-history/backend/app/store.py#L302-L303) 缺两个治理字段。`save()` 用 `get(..., 默认值)` 兜底（[store.py:461-462](file:///d:/src/agent-history/backend/app/store.py#L461-L462)），内存边字典形状不统一，`src_ids` 溯源链在此断裂。
- `Store.add_edge` 自称"写边唯一入口"（[store.py:292-305](file:///d:/src/agent-history/backend/app/store.py#L292-L305)），但全库 grep **无任何调用方**：所有边均为 `self.edges.append` 直写（commit、`_load_ontology_extra` [store.py:268-276](file:///d:/src/agent-history/backend/app/store.py#L268-L276)），REL_PAIRS 端点硬校验是死代码。
- link 提案目标不存在时，`added["link"]` 不增加但**仍在循环末尾 append 边**（[store.py:870-879](file:///d:/src/agent-history/backend/app/store.py#L870-L879)）→ 制造悬空边写入 dangling_edges.json。
- 附带：`Extract.kind` 是自由字符串（[ontology.py:133-135](file:///d:/src/agent-history/backend/app/schema/ontology.py#L133-L135)），LLM 返回未知 kind 时，[store.py:878](file:///d:/src/agent-history/backend/app/store.py#L878) 引用未绑定的 `node_id` → UnboundLocalError 500。应改为 `Literal["person","event","link"]` 并对未知 kind 跳过。

### M2. 关系目录名与实际写入名不一致："书载观点" vs "书载/观点"

- 目录：[ontology.py:233](file:///d:/src/agent-history/backend/app/schema/ontology.py#L233) `_R("书载观点", ...)`；实际写入：[store.py:878](file:///d:/src/agent-history/backend/app/store.py#L878) `"书载/观点"`。模块文档（[store.py:7](file:///d:/src/agent-history/backend/app/store.py#L7)）还写作"书载·观点"。三种写法并存，按 REL_TYPES 过滤/统计边时这些边永远对不上。

### M3. REL_PAIRS 含 DDL 不支持的方向：("Source","Artifact")

- [ontology.py:272](file:///d:/src/agent-history/backend/app/schema/ontology.py#L272) 允许 Source→Artifact；但 KGREL DDL（[store.py:109-128](file:///d:/src/agent-history/backend/app/store.py#L109-L128)）只有 `FROM Artifact TO Source`（[store.py:125](file:///d:/src/agent-history/backend/app/store.py#L125)，对应"铭文录为"），无 FROM Source 方向。
- 已逐条比对其余 42 个 REL_PAIRS 组合与 DDL 全部吻合；仅此一对：走 add_edge 写入会在 save 的 CREATE 阶段触发 Kuzu Binder 异常。建议删除该对（逆方向已由 Artifact→Source 表达）。

### M4. ActionLog / PendingOps / Resolver 均无锁，undo 存在 TOCTOU

- [actions.py:99-116](file:///d:/src/agent-history/backend/app/ontology/actions.py#L99-L116)：`undone` 检查、"最新一步"检查与 handler 执行非原子。FastAPI 同步端点跑线程池，双击/并发撤销可双执行逆动作处理器，并各记一条 action.undo（[actions.py:113-115](file:///d:/src/agent-history/backend/app/ontology/actions.py#L113-L115)）。
- `log()` 的 seq 生成与落盘无锁（[actions.py:91-97](file:///d:/src/agent-history/backend/app/ontology/actions.py#L91-L97)）：并发可产生重复 seq、JSON 文件交叉写损坏。
- 同类无锁文件：pending_ops.json（[runtime.py:57-86](file:///d:/src/agent-history/backend/app/agents/runtime.py#L57-L86)）、merges.json（[resolve.py:47-96](file:///d:/src/agent-history/backend/app/ingestion/resolve.py#L47-L96)）、agents.json（[registry.py:79-123](file:///d:/src/agent-history/backend/app/agents/registry.py#L79-L123)）。
- 缓解事实：confirm 的执行→置状态→记日志顺序（[runtime.py:248-252](file:///d:/src/agent-history/backend/app/agents/runtime.py#L248-L252)）保证了不会重复记 ActionLog，双确认的第二次通常被 executor 的状态检查挡住。

### M5. Agent 在线问答 LLM 失败直接 500，无 stub/规则回退

- [runtime.py:185](file:///d:/src/agent-history/backend/app/agents/runtime.py#L185) `llm.chat(...)` 无 try；超时（OpenAILLM 已设 30s，[llm/base.py:37-39](file:///d:/src/agent-history/backend/app/llm/base.py#L37-L39)）或任何 SDK 异常从 [routes.py:393](file:///d:/src/agent-history/backend/app/api/routes.py#L393) 冒泡为 500。与抽取路径"LLM 失败→启发式回退"（[extract.py:250-258](file:///d:/src/agent-history/backend/app/ingestion/extract.py#L250-L258)）策略不一致。
- 默认问答图同样：`narrate_node` 调 `llm.narrate` 无捕获（[nodes.py:108-116](file:///d:/src/agent-history/backend/app/graph/nodes.py#L108-L116)），在线模式生成失败 → 500。

### M6. confirm 端点返回 executor 结果而非更新后的 op，前端按 PendingOp 使用 → 卡片渲染损坏

- 后端：[runtime.py:249-253](file:///d:/src/agent-history/backend/app/agents/runtime.py#L249-L253) 返回 `result`（如 `{"person":1,"event":0,"link":0}`，无 id/status 字段）；cancel 则返回完整 op（[runtime.py:256-257](file:///d:/src/agent-history/backend/app/agents/runtime.py#L256-L257)）。
- 前端：[RightDock.tsx:173-177](file:///d:/src/agent-history/frontend/src/components/RightDock.tsx#L173-L177) 用返回值整体替换 pending 卡 → `status` 变 undefined，卡片丢失状态样式与正文、无法消失。
- 修复：confirm 返回 `set_status` 的 op（可把 result 挂在 op.result），或前端单独按 id 置 "confirmed"。

### M7. 上传管线把"已知人物"当新建提案 → 挂载即造重复实体；抽取的 links 被整体丢弃

- 启发式会把书中出现的既有主干人物录入 persons（[extract.py:180-182](file:///d:/src/agent-history/backend/app/ingestion/extract.py#L180-L182)，注释自称"供挂载时对齐"）；上传路由对 res["persons"] 无差别分配新 `new_id` 生成"新建人物"提案（[routes.py:656-659](file:///d:/src/agent-history/backend/app/api/routes.py#L656-L659)）→ 同一人物出现两个节点。
- `Resolver.resolve` / `record_merge` 全库无调用方，"实体对齐"流水线阶段（[mount.py:32-38](file:///d:/src/agent-history/backend/app/ingestion/mount.py#L32-L38)）无实际动作；LLM 抽出的 links（[extract.py:232-238](file:///d:/src/agent-history/backend/app/ingestion/extract.py#L232-L238)）在上传路由被完全忽略。
- LLM 返回的 `dynasty_id` 不校验存在性即用于 Book 与挂载默认朝代（[routes.py:649-653](file:///d:/src/agent-history/backend/app/api/routes.py#L649-L653), [routes.py:669-676](file:///d:/src/agent-history/backend/app/api/routes.py#L669-L676)），模型编造 id 会产生悬空朝代引用。

### M8. 上传异常路径：非 ParseError 异常 → 500 且文件孤儿；写库与记日志之间失败产生无日志书

- 上传路由只捕获 ParseError（[routes.py:616-620](file:///d:/src/agent-history/backend/app/api/routes.py#L616-L620)）。解析器的其他异常（MemoryError、编码极端情况、第三方库未预期错误）会让已落盘文件永久留在 files/ 且无 Book 记录。
- [routes.py:678-682](file:///d:/src/agent-history/backend/app/api/routes.py#L678-L682)：`set_chunks → append → save → action_log.log`，任一前几步失败/成功后日志失败，都会产生"无 ActionLog 记录的书"（undo 不可追溯）或回滚不完整。

### M9. 前端大量异步调用无 catch，失败对用户不可见

- AskPanel `ask`/`decide` 无错误处理（[RightDock.tsx:160-177](file:///d:/src/agent-history/frontend/src/components/RightDock.tsx#L160-L177)）：confirm 因业务前置失败（如删除已挂载书返回 400"请先卸载再删除"，[store.py:790-791](file:///d:/src/agent-history/backend/app/store.py#L790-L791)）时无任何提示，卡也不更新。
- BookMount 的 `commit`/`unmount`/`remove`/`undo` 均无 catch（[BookMount.tsx:90-123](file:///d:/src/agent-history/frontend/src/components/BookMount.tsx#L90-L123)）。

### M10. async 上传路由内同步执行 CPU/IO 重操作，阻塞事件循环

- [routes.py:617](file:///d:/src/agent-history/backend/app/api/routes.py#L617) `parse_document(dest)`、[routes.py:623](file:///d:/src/agent-history/backend/app/api/routes.py#L623) `chunk_text`、[routes.py:680](file:///d:/src/agent-history/backend/app/api/routes.py#L680) `store.save()` 均为同步调用。大 PDF 解析或全库重建期间，整个 API（含 /health）被卡住。embedding 调用已正确放入线程池（[routes.py:627-631](file:///d:/src/agent-history/backend/app/api/routes.py#L627-L631)），parse/save 也应照此处理。

---

## 四、低危问题（Low）

| # | 证据 | 说明 |
|---|------|------|
| L1 | [requirements.txt:1-11](file:///d:/src/agent-history/backend/requirements.txt#L1-L11) | **缺 `kuzu==0.11.3`**（当前靠手工装在 .venv）。全新环境 `import kuzu` 即崩，属于部署阻断项，发布前必须补。 |
| L2 | [routes.py:594-597](file:///d:/src/agent-history/backend/app/api/routes.py#L594-L597) | 上传仅校验扩展名，不校验 MIME/魔数。本地单机风险低；垃圾文件会在解析阶段被 422 拦截并清理。 |
| L3 | [main.py:10-16](file:///d:/src/agent-history/backend/app/main.py#L10-L16) | API 无任何认证。当前以 127.0.0.1 启动尚可；uvicorn 默认绑 0.0.0.0 时同网段任何人可上传/删除/确认操作。建议默认绑回环或加 token。 |
| L4 | [GraphExplorer.tsx:7-17](file:///d:/src/agent-history/frontend/src/components/GraphExplorer.tsx#L7-L17) | 节点配色/中文名/搜索下拉只有 5 类。`kg_whole` 会返回含 Institution/Artifact/Concept 的端点（[store.py:707-719](file:///d:/src/agent-history/backend/app/store.py#L707-L719)），这些节点 `COLORS[label]` 为 undefined（错色）、标签显示"undefined"。不崩溃但视觉错误。 |
| L5 | [types.ts:256](file:///d:/src/agent-history/frontend/src/types.ts#L256), [OntologyNav.tsx:5-14](file:///d:/src/agent-history/frontend/src/components/OntologyNav.tsx#L5-L14) | `KGLabel`/`KGSchema` 类型未含新三表；OntologyNav 无机构/文物/概念分类入口，新实体只能从 KG 浏览触达。 |
| L6 | [mount.py:44](file:///d:/src/agent-history/backend/app/ingestion/mount.py#L44), [mount.py:67](file:///d:/src/agent-history/backend/app/ingestion/mount.py#L67) | mount_start 后、commit 前书被删除（上传书未挂载允许删）→ commit 节点 `next(...)` StopIteration → 500。建议改查找 + 400。 |
| L7 | [runtime.py:214-223](file:///d:/src/agent-history/backend/app/agents/runtime.py#L214-L223) | `_split_ops` 非贪婪匹配到第一个 `】`；若 LLM 在 summary 文本里含 `】`，JSON 截断解析失败 → ops 被静默丢弃。 |
| L8 | [BookMount.tsx:254-261](file:///d:/src/agent-history/frontend/src/components/BookMount.tsx#L254-L261), [BookMount.tsx:68](file:///d:/src/agent-history/frontend/src/components/BookMount.tsx#L68) | 挂载后统计按全部 extracts 计数而非实际 accepted（部分挂载时数字虚高）；演示用 setInterval 在组件卸载时未清理。 |
| L9 | [runtime.py:261-297](file:///d:/src/agent-history/backend/app/agents/runtime.py#L261-L297) | LLM 给出的 params 缺 `book_id` 时 executor 抛 KeyError → 500（路由只把 KeyError 映射为 404 的情形在 confirm 外层，executor 内 KeyError 不被转 400）。 |
| L10 | [check_kuzu.py:7-9](file:///d:/src/agent-history/backend/scripts/check_kuzu.py#L7-L9) | `read_only=True` 设计上可与写进程共存（Kuzu 支持多只读 + 单写），本次未实测；建议脚本头部注明所需 Kuzu 版本行为。 |

---

## 五、专项分析

### 5.1 commit / uncommit 对称性

- 对称部分（[store.py:836-894](file:///d:/src/agent-history/backend/app/store.py#L836-L894)）：
  - 新建 Person/Event 按 `from_book` 精确移除；
  - edges 按 `from_book` 移除，且 `self.edges` 包含从 dangling_edges.json 载入的悬空边，故挂载时造的悬空边也能被清掉（save 会重写 dangling 文件）；
  - link 追加到既有事件的 `book_sources`，卸载时遍历全部对象按 Source.from_book 清理，不漏。
- 不对称/缺口：
  1. 撤销卸载的重挂不按原位图（H4）；
  2. link 目标缺失时仍造悬空边（M1），虽可对称删除但属于写入污染；
  3. 当前管线不产生跨书边（上传提案一律新建 id），"他书引用本书创建节点"暂无实际路径；若未来开放跨书 link，uncommit 必须先检查依赖，否则被引节点删除后他书边转悬空。
- `zip(book.extracts, accepted)`（[store.py:838](file:///d:/src/agent-history/backend/app/store.py#L838)）：accepted 短于提案时尾部被静默当作否决；前端与 agent 路径当前都按提案数传值，风险低，建议显式校验长度。

### 5.2 Undo 单步语义

- "仅最新一条未撤销记录可撤销"规则本身正确：[actions.py:106-109](file:///d:/src/agent-history/backend/app/ontology/actions.py#L106-L109)，并正确排除 action.undo 条目，使得连续线性 undo（…→undo→undo 前一步）可以工作；重复撤销被 `undone` 拦截（[actions.py:104-105](file:///d:/src/agent-history/backend/app/ontology/actions.py#L104-L105)）。
- 缺口：
  1. `book.upload` 在 ACTION_DEFS 里声明 inverse=book.delete（[actions.py:41-44](file:///d:/src/agent-history/backend/app/ontology/actions.py#L41-L44)），但**未注册 handler**（仅注册 mount/unmount/delete，[routes.py:752-765](file:///d:/src/agent-history/backend/app/api/routes.py#L752-L765)），对 upload 记 undo 会得到 400"暂不支持撤销"；而 BookMount 的撤销按钮判定（[BookMount.tsx:134-135](file:///d:/src/agent-history/frontend/src/components/BookMount.tsx#L134-L135)）会把 upload 也显示为可撤销，点击必失败且无提示。
  2. `book.restore` 无 handler（不支持"撤销恢复"/redo）；ACTION_DEFS 与实际能力不一致，应在 defs 中标注 reversible=false 或补齐处理器。
  3. 并发性缺口见 M4；重放语义缺口见 H4。

### 5.3 锁违规风险

- 进程内：RLock 保证单连接使用串行（Kuzu 连接非线程安全，这一点处理正确），但多步操作不原子（H2）。
- 跨进程：
  - CLI [ingest_book.py](file:///d:/src/agent-history/backend/scripts/ingest_book.py) 全文只用 `urllib`，**无 kuzu import、无开库代码**，遵守"只走 HTTP"纪律 ✓；
  - check_kuzu.py 只读模式 ✓（L10）；
  - 部署注意：`uvicorn --workers 2` 的第二个 worker 在 `Store()` 构造时即因 Kuzu 单写者锁崩溃；应在启动文档中明确"单 worker"。
- 现存运行实例：uvicorn --reload（父 PID 41104 / worker PID 32452，绑 127.0.0.1:8000）。审查期间未启动第二实例。

### 5.4 异常路径评估

| 路径 | 处理 | 评价 |
|---|---|---|
| 解析失败 | ParseError → 删临时文件 → 422（[routes.py:616-620](file:///d:/src/agent-history/backend/app/api/routes.py#L616-L620)）；PDF 页级异常容忍（[parse.py:51-56](file:///d:/src/agent-history/backend/app/ingestion/parse.py#L51-L56)）；扫描件明确提示 OCR | 良好；非 ParseError 兜底不足（M8） |
| Embedding 超时/失败 | try/except → None，切块照常落盘（[routes.py:627-635](file:///d:/src/agent-history/backend/app/api/routes.py#L627-L635)）；SDK 30s 超时 | 良好；检索侧向量无命中自动回退 BM25（[rag.py:145-155](file:///d:/src/agent-history/backend/app/ingestion/rag.py#L145-L155)） |
| LLM 抽取失败 | 自动回退启发式（[extract.py:250-258](file:///d:/src/agent-history/backend/app/ingestion/extract.py#L250-L258)） | 良好；但启发式产物有重复实体问题（M7） |
| LLM 问答失败 | 无捕获 → 500 | 不足（M5） |
| 确认卡执行失败 | executor 异常 → 卡保持 pending，可重试 | 合理；但部分失败后 mounted 状态漂移（H2）、前端无错误提示（M9） |
| 瓦片上游故障 | 1×1 透明 PNG 降级（[routes.py:152-158](file:///d:/src/agent-history/backend/app/api/routes.py#L152-L158)） | 良好 |

### 5.5 安全：上传校验 / 路径穿越

- 上传落盘名 = 全新 UUID + 白名单后缀（[routes.py:598-600](file:///d:/src/agent-history/backend/app/api/routes.py#L598-L600)），原始文件名从不参与存储路径 → 文件名路径穿越不可行；50MB 限制在流式读取中强制执行，超限即删临时文件（[routes.py:602-614](file:///d:/src/agent-history/backend/app/api/routes.py#L602-L614)）。
- trash/restore 中 `book.file` 即该 UUID 名（[store.py:786-833](file:///d:/src/agent-history/backend/app/store.py#L786-L833)），同样无穿越。
- EPUB 仅在 zip 内按成员名 read、不解压文件系统，无 Zip Slip；成员名含 `../` 只作为字典键，不触盘。
- 瓦片 tile id 有正则白名单、zoom 有边界（[routes.py:138-146](file:///d:/src/agent-history/backend/app/api/routes.py#L138-L146)）；kg_nodes 表白名单、列名固定（[store.py:666-677](file:///d:/src/agent-history/backend/app/store.py#L666-L677)），无注入面。
- 残留风险：无认证（L3）、仅扩展名校验（L2）、确认卡社会工程面（H3）。

---

## 六、修复优先级建议

1. 第一批（阻断/安全）：H1（删除方法）→ H3（确认卡真实目标 + confirm 重新授权）→ L1（requirements 补 kuzu）。
2. 第二批（数据完整性）：H2（save 原子化/事务）→ H4（accepted 持久化）→ M1/M2/M3（边形状与目录统一）。
3. 第三批（健壮性/体验）：M4-M10 → 前端错误提示全覆盖 → L4/L5 新三类实体的浏览与类型补齐。

---

*本报告仅基于静态阅读与非破坏性探测，未执行写库操作；行号以审查时工作区版本为准。*
