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
