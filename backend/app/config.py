import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# LLM 提供方：stub（离线确定性）| openai
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "stub")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# ---- 分用途网关：抽取（量大、要便宜快）与对话/综述（要质量）可分不同服务商 ----
# 未配置时回退到统一的 OPENAI_*，换服务商只需改 .env，不动代码。
# 例（DeepSeek 官方）：EXTRACT_BASE_URL=https://api.deepseek.com/v1
#                     EXTRACT_MODEL=deepseek-chat  EXTRACT_API_KEY=sk-...
EXTRACT_API_KEY = os.getenv("EXTRACT_API_KEY", "") or OPENAI_API_KEY
EXTRACT_BASE_URL = os.getenv("EXTRACT_BASE_URL", "") or OPENAI_BASE_URL
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "") or OPENAI_MODEL
CHAT_API_KEY = os.getenv("CHAT_API_KEY", "") or OPENAI_API_KEY
CHAT_BASE_URL = os.getenv("CHAT_BASE_URL", "") or OPENAI_BASE_URL
CHAT_MODEL = os.getenv("CHAT_MODEL", "") or OPENAI_MODEL

# ---- LLM 调用行为（不同模型/网关差异很大，全部可在 .env 里调，改完重启即生效）----
# 单次调用硬超时（秒）。网关会偶发挂死（实测 >6min 无响应），而 SDK 默认不超时，
# 批处理会被单个请求拖死；超时按失败计入重试。
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
# 全局调用最小间隔（秒）：RPS 配额很低，并发高会 429 rps exhausted
LLM_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "1.5"))
# 单次调用的最大重试次数（空响应 / 429 / 超时都计入）
LLM_MAX_RETRY = int(os.getenv("LLM_MAX_RETRY", "2"))
# 推理型模型（token 全被 reasoning 吃掉、content 恒空）必须显式关思考
LLM_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "1") != "0"
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
# 抽取：单块送入的原文上限（字）。给太多 → 输出 JSON 变长 → 被 max_tokens 截断
LLM_EXTRACT_INPUT_CHARS = int(os.getenv("LLM_EXTRACT_INPUT_CHARS", "6000"))
# 抽取：输出预算。须 ≥ 输入能催生的 JSON token 量，否则 finish_reason=length 截断
LLM_EXTRACT_MAX_TOKENS = int(os.getenv("LLM_EXTRACT_MAX_TOKENS", "8000"))
# 对话/综述输出预算（0 = 不限制）
LLM_CHAT_MAX_TOKENS = int(os.getenv("LLM_CHAT_MAX_TOKENS", "4096"))

# ---- 图库（Kuzu 嵌入式）内存上限 ----
# save() 目前是全量重建（DROP + CREATE 全表重写），挂载一本书要写入数万节点与边。
# Kuzu 默认的 buffer pool 会按机器内存比例吃，实测挂《资治通鉴》44447 条提案时
# 进程峰值冲到 17.7 GB 并抛 RuntimeError: bad allocation。
# 显式给出上限，让 Kuzu 落盘换页而不是无限吃内存（牺牲速度换稳定性）。
KUZU_BUFFER_POOL_GB = float(os.getenv("KUZU_BUFFER_POOL_GB", "6"))

# ---- 本地嵌入模型（ONNX，离线可用）----
# 当前 LLM 服务不提供 embedding 模型，RAG 的语义检索改由本地模型承担。
# 模型存放于项目根 models/（与 backend/ 同级），由 scripts/embed_chunks.py 下载。
LOCAL_EMBED_ENABLED = os.getenv("LOCAL_EMBED_ENABLED", "1") != "0"
LOCAL_EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
LOCAL_EMBED_DIR = os.getenv("LOCAL_EMBED_DIR", "") or str(
    Path(__file__).resolve().parents[2] / "models")
LOCAL_EMBED_DIM = int(os.getenv("LOCAL_EMBED_DIM", "512"))
# 国内网络直连 HuggingFace 常超时，首次下载模型时需设置镜像
HF_ENDPOINT = os.getenv("HF_ENDPOINT", "")

# 时间轴范围
YEAR_MIN = -2200
YEAR_MAX = 1912

# osgeo 瓦片模板（ID 规则：页面 slug m058d -> 瓦片 ID mp058d）
OSGEO_TILE_TPL = "https://tile.osgeo.cn/wmts/{tile}/webmercator/{{z}}/{{x}}/{{y}}.png"
OSGEO_ATTR = "© <a href='https://osgeo.cn/' target='_blank'>OSGeo中国中心</a>"
