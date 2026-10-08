"""事件子类（kind）派生规则。

背景：编年体史书（尤其《资治通鉴》、诸史本纪/五行志）里，日食、地震、水旱、
祥瑞、大赦这类记载占了相当比例，且**每条都是独立的一次记录**（如「日食」
249 条跨 234 个年份）。它们不是脏数据，但挤在时间轴上会淹没真正的大事。

按 docs/PLAN.md 附录 A.3 的做法：不另立类型，用 `Event.kind` 承载，
前端默认折叠、可展开。

实现选择：**不落库，在加载时派生**。
Kuzu 这一版不支持 `ALTER TABLE ... ADD COLUMN`（实测 Parser exception），
而加列只有 DROP+重建一条路 —— 那是会清库的危险操作。派生还有一个好处：
规则改了重启即生效，不必动数据，也无需回滚。

规则是**保守**的：宁可漏标，不可误标（误标会让一条真事件被折叠）。
特别注意回避「崩」——「高祖崩」是人物死亡，不是山崩。
"""

from __future__ import annotations

import re

# 顺序敏感：先匹配更具体的类别。返回第一个命中的类别名。
_RULES: list[tuple[str, re.Pattern]] = [
    ("日食星变", re.compile(
        r"日食|日蚀|日有食|日有蚀|月食|月蚀|彗星|孛星|太白|岁星|荧惑|辰星|"
        r"镇星|五星|客星|星变|星陨|星孛|星见|星昼见|星晝見|陨星|流星")),
    ("地震山崩", re.compile(
        r"地震|山崩|地陷|地裂|地坼|山鸣|山裂|岸崩|沙麓崩|梁山崩|川竭")),
    ("疫疠", re.compile(r"疫|疠")),
    ("祥瑞", re.compile(
        r"甘露|嘉禾|麒麟|凤凰|凤皇|黄龙|青龙|白龙|龙见|龙现|两龙|芝草|"
        r"连理|瑞麦|白雉|白鹿|白乌|神雀|景云|庆云|醴泉|木连理")),
    ("赦", re.compile(r"赦")),
    ("水旱蝗饥", re.compile(
        r"螟|蝗|螽|大水|水灾|霖雨|不雨|亢旱|久旱|旱|大饥|饥馑|饥|河决|河溢")),
    ("雨雪雹霜", re.compile(
        # 「大风」要排除《大风歌》：那是刘邦的诗，不是天气记录
        r"大雨|大雪|雨雹|雨血|雨土|雨木冰|雹|恒雨|恒燠|恒寒|无冰|木冰|"
        r"冬雷|雷震|大风(?!歌)|霾|霜")),
]

# 命中这些的不算灾异（人物死亡、即位等常见误伤）
_EXCLUDE = re.compile(r"(崩|薨|卒|即位|立为|葬)$")


def kind_of(name: str | None) -> str | None:
    """按事件名派生子类；不属于灾异/套话则返回 None。"""
    if not name:
        return None
    s = name.strip()
    if not s:
        return None
    if _EXCLUDE.search(s):
        return None
    for kind, rx in _RULES:
        if rx.search(s):
            return kind
    return None


#: 所有可能的子类（供 API/前端枚举）
KINDS: tuple[str, ...] = tuple(k for k, _ in _RULES)
