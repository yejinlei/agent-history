"""静态体检：各标签的 update_sql（SET）引用参数 vs 渲染时传入的 params 键。

背景：卸载《资治通鉴》时 save() 在 `_run_batch(update_sql, upd_rows)` 抛
`RuntimeError: Parameter did not found.`——Kuzu 对「语句没引用却又传了的参数」
直接报错。这里不加载 Store（构造一次要十分钟），只解析源码里的模板做比对。
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app" / "store.py"
src = SRC.read_text(encoding="utf-8")

# 形如  "CREATE (n:Event {id:$id, ...})"  的模板（跨行拼接的隐去逗号不在此列）
tmpl = re.findall(r'"(CREATE \(n:(\w+) \{[^}]*\}\))"', src)
print("模板数:", len(tmpl))


def update_sql(create_sql: str, label: str, key_field: str) -> str:
    body = create_sql[create_sql.index("{") + 1: create_sql.rindex("}")]
    clause = []
    for seg in body.split(","):
        prop, _, param = seg.partition(":")
        prop, param = prop.strip(), param.strip()
        if prop and prop != key_field:
            clause.append("n.%s=%s" % (prop, param))
    return "MATCH (n:%s {%s:$k}) SET " % (label, key_field) + ", ".join(clause)


# 紧随模板的 params 字典字面量（可能跨多行，取到配对的收尾花括号为止）
bad = 0
for sql, label in tmpl:
    key_field = "sid" if label == "Source" else "id"
    i = src.index(sql)
    j = src.index("{", src.index("})", i) + 2)          # params 字典起点
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    plit = src[j:k + 1]
    pkeys = set(re.findall(r'"(\w+)":', plit))
    usql = update_sql(sql, label, key_field)
    uref = set(re.findall(r"\$(\w+)", usql))
    # 渲染时 upd_rows = params 去掉主键 + 追加 k
    provide = (pkeys - {key_field}) | {"k"}
    miss, extra = uref - provide, provide - uref
    flag = "OK " if not (miss or extra) else "BAD"
    if flag == "BAD":
        bad += 1
    print(f"{flag} {label:<12} SET 引用 {len(uref):>2} 参数 | 缺: {sorted(miss)} | 多: {sorted(extra)}")

print("不一致标签数:", bad)
sys.exit(1 if bad else 0)
