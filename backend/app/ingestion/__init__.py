# -*- coding: utf-8 -*-
"""摄入层：解析 / 抽取 / 切块检索 / 实体对齐 / 本体构建管线。

注意：本包 __init__ **刻意保持为空**。子模块（parse / rag / extract /
resolve / pipeline）都不依赖 Store，因此离线脚本可以在服务运行期间
import 它们而不与 Kuzu 单写者锁冲突。
需要挂载图（mount → app.store）的调用方请显式 `from app.ingestion.mount import ...`。
"""
