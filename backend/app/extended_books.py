"""策展书（扩展知识）名单：书库的第二层，不入主干。

为什么 scope 不落成图库的列
--------------------------
Kuzu 0.11 的 ALTER 只支持 RENAME / DROP，**不支持 ADD COLUMN**（实测报
``Invalid input <ADD COLUMN>: expected rule kU_AlterOptions``）；而存量图库已
7 GB、26 史全部挂载，为加一列去重建库的代价远高于把名单放这里。
因此判定放在代码 + 数据目录的 ``book_scopes.json``（形如 ``{"extended": [...]}``），
以后新增策展书改这个文件即可，不必动代码、也不必动库。

两层语义
--------
- ``canonical``（默认）：主干史料。可挂载，提案（人物/事件/语义边/对齐）写入
  主干本体，进入时间轴 / 地图 / 图谱。
- ``extended``：策展书——学术专著、历史小说、科普读物。**禁止挂载**（store.commit_book
  与 mount_start 双重闸门），只作为书卡 + 观点卡片在「扩展阅读」栏展示，
  不产生任何主干节点与边。理由：这类书的叙述是观点而非史事，《大秦帝国》
  的人物对话多为虚构，若并入主干会让小说情节变成《史记》同级的可引用史实。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

CANONICAL = "canonical"
EXTENDED = "extended"

# 内置策展书：来自 seed.py 的手工提案（id 一眼可辨，提案形如 bk_xxx 前缀）
_BUILTIN_EXTENDED = frozenset({"wanli15", "daqin", "silkroad"})

_SCOPES_FILE = "book_scopes.json"


def extended_ids() -> set[str]:
    """返回应作「扩展知识」处理的书 id 集合（内置名单 + 数据目录配置）。"""
    out = set(_BUILTIN_EXTENDED)
    p: Path = DATA_DIR / _SCOPES_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out |= {str(x) for x in (data.get("extended") or [])}
        except Exception as e:      # 配置文件坏掉不该让服务起不来，退回内置名单
            logger.warning("读取 %s 失败（%s），只用内置策展书名单", p, e)
    return out


def is_extended(book_id: str) -> bool:
    return book_id in extended_ids()


def scope_of(book_id: str) -> str:
    return EXTENDED if book_id in extended_ids() else CANONICAL
