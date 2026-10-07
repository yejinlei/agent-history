# 逐步构建指南（离线 / 在线）

原则是**一本一本地叠加**：内置 27 本书只是"待构建队列"，跑通一本、人工确认、再上下一本。

## 0. 三条硬约束

1. **单写者**：DuckDB（切块/进度）与 Kuzu（图谱）都只能被一个进程写 —— 离线脚本必须**先停服**；在线入口由后端进程统一写。
2. **幂等**：切块按 `book_id` 整本替换；抽取进度按 `(book_id, piece_idx, model)` 记录，重跑只补未完成的块；换模型自动重跑。
3. **挂载必须人工确认**：构建只生成提案（`Book.extracts`），写入图谱由 `/api/mounts/start → commit` 完成，UI 上逐项勾选。

## 1. 环境准备（一次性）

```powershell
cd d:/src/agent-history
python -m venv .venv
.venv\Scripts\pip install -r backend/requirements.txt
```

`backend/.env` 关键项（真实 LLM 才需要；不配也能离线用启发式）：

```
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_MODEL=...

# 抽取专用网关（量大，要便宜快）；不配则回退到上面的 OPENAI_*
EXTRACT_API_KEY=...
EXTRACT_BASE_URL=https://api.deepseek.com/v1
EXTRACT_MODEL=deepseek-chat
# 对话/综述专用网关（要质量）；不配同样回退
CHAT_API_KEY=...
CHAT_BASE_URL=...
CHAT_MODEL=...
```

> 网关实测（token.sensenova.cn，2026-10）：单次 25–30s；**TPM/RPM 配额极低**，
> 连续调用数十次即 429 `inference exceeds tpm/rpm limit`，配额打满后静默返回空串；
> 并发 ≥4 触发 `rps exhausted`。因此抽取默认限速（`LLM_MIN_INTERVAL`，默认 1.5s）
> 并对空响应/429 退避重试（`LLM_MAX_RETRY`，默认 2）。**换服务商只需改这三组环境变量。**
>
> 提示词坑：**长 system 提示词会让请求挂死**（>180s）。JSON schema 必须放 user 侧，
> system 只留一句约束。
>
> **最关键的坑：该网关的模型默认是「推理型」**，token 全被 `reasoning` 吃掉，
> `content` 恒为空（表现为空响应/回退启发式）。必须显式关闭思考：
> `extra_body={"thinking": {"type": "disabled"}}`（已在 `app/llm/base.py` 默认开启，
> 可用 `LLM_DISABLE_THINKING=0` 关掉；网关不认该参数时会自动降级重试）。
> 关掉思考后 token 全给正文，`max_tokens` 要给到 4000，否则 JSON 被截断
> （`finish_reason=length`）。

Windows 控制台中文/符号乱码时先设：`$env:PYTHONIOENCODING="utf-8"`。

## 2. 回到干净基线（强烈建议先做）

旧版本启动时会把 27 本书一次性切块并自动挂载。逐本叠加要从干净基线开始：

```powershell
# 启服后查看
python scripts/reset_library.py --list
python scripts/reset_library.py --unmount-all --keep shiji --dry-run   # 只看计划
python scripts/reset_library.py --unmount-all --keep shiji             # 真删（会逐本确认）
```

卸载只是移除该书写入的子图与来源，不动其他书的数据。

## 3. 离线构建（停服，推荐用于大批量）

```powershell
# 1) 停掉后端（Ctrl+C 或结束占用 8000 端口的进程）

# 2) 先看队列
cd backend
python scripts/ingest_one.py --list

# 3) 先跑小样本，看抽取质量（不写库）
python scripts/ingest_one.py shiji --limit 100 --dry-run

# 4) 满意后真跑（续跑：只补未完成的块）
python scripts/ingest_one.py shiji --limit 100
python scripts/ingest_one.py shiji                 # 放开全书
python scripts/ingest_one.py shiji --llm --limit 100   # 用 .env 的大模型抽取
python scripts/ingest_one.py shiji --llm               # 大模型跑全书

# 补齐：一轮跑完自动算缺口再补（只补缺口块，成功块不重跑）
python scripts/ingest_one.py shiji --llm --repair --max-rounds 3
# 跑完必须完整（还有块没跑成功就以退出码 2 结束，适合写进脚本）
python scripts/ingest_one.py shiji --llm --repair --strict

# 调参不重烧钱：原始抽取结果已落盘（extract_raw 表），改 TopN/阈值后秒级重算
python scripts/ingest_one.py shiji --finalize-only --max-persons 3000 --max-events 4000

# 5) 启服，到界面确认挂载
cd backend; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

输出示例：

```
[ok] 《汉书》(hanshu)  mode=heuristic model=heuristic
  切块 1333 · 抽取块 8/427（本次处理 5）
  提案 13 {'person': 0, 'event': 0, 'link': 13}
  完整性 覆盖 8/427（1.87%）· 原始结果 8 · 缺块 419 · 缺结果 0 · 空 0 · 回退 0 · 报错 0
  产物 D:\...\app\data\ingest\hanshu.json
```

`完整性` 一行是自检结果：**缺块 / 缺结果 / 报错 三项都为 0 才算跑完整**
（空块与回退块只是质量提示，不算未完成，但 `--repair` 会试着再补）。

> **别把两种「块」混着数**：`切块 N` 是入库的最小切片（检索/上下文用），
> `抽取块 M/P` 才是 LLM 的抽取单元（若干切片拼成一块）。判断"这本书跑完没有"
> 只看 `book_runs.pieces / done`（或 `scripts/ingest_one.py --status`）。
> 拿 `chunks` 表的行数去比 `ingest_progress` 的条数，会凭空算出几万块的缺口
> ——《宋史》7,215 个切片对应 2,374 个抽取块，两者差三倍。

常用开关：`--reset`（清进度重跑）、`--force`（连切块一起重跑）、`--embed`（算向量，可选）、`--no-store`（只写 DuckDB 与产物 JSON，不碰 Kuzu）、`--out`（指定产物路径）。

### 3.5 换网关后的验收（先跑 5 块）

```powershell
python scripts/ingest_one.py shiji --limit 5 --llm --concurrency 2 --dry-run
```

看输出里的 `大模型块 N · 回退启发式块 M`：N 接近 5 才说明网关可用；
M 高说明仍是空响应/限流，先降 `--concurrency 1` 并加大 `$env:LLM_MIN_INTERVAL`（如 3~6）。

### 3.6 补齐机制与可靠性保障

一本大书要几千块、跑几个小时，中途断网、限流、Ctrl+C 都会留下缺口。
现在「有没有缺口」不再靠猜，而是有台账、有自检、有补跑：

| 机制 | 作用 |
| --- | --- |
| 单块重试 `--retries 2` | 抛异常 / 网关空响应都重试，重试间隔 1.5s→3s→6s |
| 异常隔离 | 一块挂了只丢那一块，不再拖垮整批（以前一块抛异常，后面全丢） |
| 失败台账 `ingest_failures` | 区分「没跑过 / 报错 / 抽到空 / 大模型回退」，补跑只补该补的 |
| 缺口计划 | 每轮开跑前先算缺口：没跑过 + 报错 + 缺原始结果 + 空块（最多补 2 次）+ 回退块 |
| `--repair --max-rounds N` | 跑完一轮自动重算缺口再补，直到无缺口或达轮数 |
| 收口全量重算 | 提案始终由**全部已落盘块**重算，补跑只会让提案变多，不会变少 |
| 写入顺序 | 先落原始结果、再记进度；不会出现「进度说跑过、结果没落盘」的静默缺口 |
| 产物保护 `keep_best` | 新提案比旧产物少时**不覆盖**旧产物（真要覆盖加 `--force`） |
| 状态保护 | 小样本 / 换模型试跑不会把「全书已跑完」的 `book_runs` 状态与 `Book.extracts` 冲掉 |
| 参数指纹 | 切块/抽取参数或书源文件一变，旧进度与旧结果自动作废重跑（块边界已变） |
| 中断安全 | Ctrl+C / 异常也会先收口落盘，重跑同一条命令即可续跑 |
| `--strict` | 覆盖不完整就以退出码 2 结束，可直接写进脚本做门禁 |
| 挂载提示 | `POST /api/mounts/start` 会返回 `warnings`：只跑了一部分的书会明确提示 |

看缺口（不用跑）：

```powershell
python scripts/ingest_one.py --list      # 队列里直接带「缺口(缺/空/退/错)」列
python scripts/ingest_one.py shiji --status
```

在线入口同样支持：`POST /api/ingest/{book_id}` 传
`{"repair": true, "max_rounds": 3, "retries": 2, "strict": true}`，
任务结果里带 `integrity` 自检报告。

### 3.7 长任务怎么在后台跑（Windows）

一键启动（推荐，两个窗口都另开、关掉脚本窗口不会带走服务）：

```powershell
cd d:\src\agent-history
.\scripts\serve.ps1 -Frontend     # 后端 :8000 + 前端 :5173，自动等就绪
.\scripts\serve.ps1 -Minimized    # 窗口最小化后台跑
.\scripts\serve.ps1 -Status       # 看状态（谁在跑、health）
.\scripts\serve.ps1 -Stop         # 按端口停服务
.\scripts\serve.ps1 -Restart -Frontend
```

若被执行策略拦住：`powershell -ExecutionPolicy Bypass -File .\scripts\serve.ps1 -Frontend`

一本大书要跑几小时，命令窗口一关进程就没了。两个配套脚本：

```powershell
# 1) 常驻服务（在线构建用；日志 backend/logs/serve.log）
cd d:\src\agent-history\backend
d:\src\agent-history\.venv\Scripts\python.exe -u scripts\serve.py

# 2) 离线长任务（停服后跑；日志 backend/logs/<目标脚本>.log）
d:\src\agent-history\.venv\Scripts\python.exe -u scripts\run_bg.py `
    scripts\ingest_one.py 资治通鉴 --llm --concurrency 3 --repair --max-rounds 3 --no-store
```

`run_bg.py` 也支持配置文件（命令行传中文容易出编码问题）：

```json
// backend/_bg_task.json
{"target": "scripts/ingest_one.py",
 "argv": ["资治通鉴", "--llm", "--concurrency", "3", "--no-store"],
 "env": {"LLM_MIN_INTERVAL": "2"}}
```
```powershell
python -u scripts\run_bg.py --config _bg_task.json
```

想让进程**完全脱离终端**（连窗口都不用留），用 WMI 或任务计划拉起（任务计划需要管理员权限）：

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = 'd:\src\agent-history\.venv\Scripts\python.exe -u ' +
                  'd:\src\agent-history\backend\scripts\run_bg.py --config ' +
                  'd:\src\agent-history\backend\_bg_task.json'
    CurrentDirectory = 'd:\src\agent-history\backend' }
```

进度随时可看：日志是行缓冲的，`Get-Content logs\ingest_one.log -Tail 5 -Wait`。

## 4. 在线构建（不停服，适合边用边加）

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/hanshu \
     -H "Content-Type: application/json" -d '{"limit":100,"use_llm":false}'
# → {"job_id":"...","state":"running",...}

curl http://127.0.0.1:8000/api/ingest/hanshu   # 进度：done/pieces、proposals、stage
curl http://127.0.0.1:8000/api/ingest          # 全部任务
curl http://127.0.0.1:8000/api/books/queue     # 27 本书的构建队列
curl http://127.0.0.1:8000/api/books           # 每本书带 ingest 进度
```

挂载（人工确认）：

```bash
curl -X POST http://127.0.0.1:8000/api/mounts/start -d '{"book_id":"hanshu"}'
# → {"thread_id":"...","proposals":[...]}
curl -X POST http://127.0.0.1:8000/api/mounts/commit \
     -d '{"thread_id":"...","accepted":[true,false,...]}'   # 位图与提案一一对应
```

一次只跑一个构建任务（接口会返回 409）。上传自己的书：`python scripts/ingest_book.py <file> [--mount]`。

## 5. 叠加出多史观（顺序建议）

```
《史记》(纪传) → 《资治通鉴》(编年) → 《读通鉴论》(史评) → 汉书 → 后汉书 → 三国志 …
```

第二本起 `align_mode="link"`（默认）：已在主干的人物/事件转成"对齐"提案，不重复造节点，只挂该书的说法与出处。叠加后同一实体卡会出现多个来源，分歧由 G4 并列呈现（见 `docs/ONTOLOGY_ALIGN.md`）。

按书查看三视图切片：`GET /api/graph/query?book_id=shiji&year_from=-221&year_to=-206&bbox=...`

## 6. 质量与反幻觉闸门

- 构建侧：G1 模式白名单、G2 证据绑定（提案带原文摘要与来源书）
- 回答侧：`POST /api/chat` 返回 `grounding`（接地率、未接地的年份/专名）与 `warnings`
- 想核对某说法：`POST /api/ontology/import` 导入离线产物，或在实体卡看 `sources` 的 `from_book`

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `检测到服务正在运行（单写者锁）` | 离线脚本要求停服；或改用 `POST /api/ingest/{book_id}` |
| `Catalog Error: Table ingest_progress does not exist` | 未重启后端加载新 DDL；重启服务 |
| `已挂载，请先卸载再重跑抽取` | 先 `POST /api/mounts/unmount`，或 `scripts/reset_library.py` |
| 提案全是 `link`、person/event 为 0 | 主干已有这些实体（正常）；从干净基线跑第一本时会是 person/event |
| 中文乱码 / `UnicodeEncodeError` | 设 `$env:PYTHONIOENCODING="utf-8"` |
| 想重来一遍某本书 | `python scripts/ingest_one.py <book> --reset`（重跑抽取）或加 `--force`（连切块重跑） |
| 抽取报 `429 / rps exhausted / tpm limit` | 网关配额打满：降并发、加大 `LLM_MIN_INTERVAL`，或换 `EXTRACT_*` 服务商 |
| `回退启发式块` 数很高 | 网关在静默返回空串：换服务商；启发式可先顶上（召回低但不会脏） |
| 人名出现「傅险姓之」这类碎片 | 已在名称闸门拦掉（`pipeline._ok_person_name`）；若还有漏网，把脏字加进 `_NAME_STOP_ANY` |
