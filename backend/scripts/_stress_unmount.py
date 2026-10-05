"""大批量卸载压力测试：唯一会造成数据不一致的隐患，必须实测。

流程：基线快照 → 卸载全书 → 校验（边/节点/副本/悬空）→ 用**原 accepted 位图**
原样重挂 → 再校验是否回到基线。

判定标准：
1. 卸载后该书在 Kuzu 与 DuckDB 副本里的行都清零（不留下半卸载脏数据）；
2. 悬空边数不增加（卸载不能把别人的边搞成悬空）；
3. 重挂后各项计数回到基线 ±1%（允许实体对齐的微小差异）。
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
BID = "资治通鉴"
Q = urllib.parse.quote(BID)


def _req(path: str, payload: dict | None = None, timeout: int = 3600):
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot(tag: str) -> dict:
    s = _req("/api/kg/schema", timeout=600)
    nodes = {t["label"]: t["count"] for t in s["tables"]}
    rels = {r["rel"]: r["count"] for r in s["relations"]}
    copy_n = len(_req(f"/api/graph/query?book_id={Q}&limit=200000",
                      timeout=600)["items"])
    print(f"[{tag}] 边={s['edge_count']} 悬空={s['dangling']} "
          f"节点={sum(nodes.values())} 副本内该书实体={copy_n}", flush=True)
    print(f"      书载观点={rels.get('书载观点', 0)} "
          f"参与={rels.get('参与', 0)} 触发={rels.get('触发', 0)}", flush=True)
    return {"edge": s["edge_count"], "dangling": s["dangling"],
            "nodes": sum(nodes.values()), "copy": copy_n,
            "rel": rels.get("书载观点", 0)}


base = snapshot("基线")
acc = None
for b in _req("/api/books", timeout=600)["books"]:
    if b["id"] == BID:
        acc = b.get("accepted")
print(f"原 accepted 位图长度 = {len(acc) if acc else '无'}", flush=True)

t0 = time.time()
print("=== 卸载 ===", flush=True)
print(_req("/api/mounts/unmount", {"book_id": BID}), flush=True)
print(f"卸载耗时 {time.time() - t0:.1f}s", flush=True)
after = snapshot("卸载后")

ok = True
if after["copy"] != 0:
    print(f"[FAIL] 副本仍残留该书 {after['copy']} 行", flush=True)
    ok = False
if after["dangling"] > base["dangling"]:
    print(f"[FAIL] 悬空边从 {base['dangling']} 涨到 {after['dangling']}", flush=True)
    ok = False
if after["edge"] >= base["edge"]:
    print(f"[FAIL] 边数没减少（{base['edge']} → {after['edge']}）", flush=True)
    ok = False
if ok:
    print("[OK] 卸载后一致性检查通过", flush=True)

print("=== 重挂 ===", flush=True)
t1 = time.time()
st = _req("/api/mounts/start", {"book_id": BID})
props = st["proposals"]
print(f"提案 {len(props)} 项，耗时 {time.time() - t1:.1f}s，"
      f"警告={st.get('warnings')}", flush=True)
if acc and len(acc) == len(props):
    bitmap = acc
else:
    bitmap = [True] * len(props)
    print("（无原位图，全部接受）", flush=True)
t2 = time.time()
res = _req("/api/mounts/commit",
           {"thread_id": st["thread_id"], "accepted": bitmap})
print(f"挂载结果 {json.dumps(res, ensure_ascii=False)[:400]}", flush=True)
print(f"重挂耗时 {time.time() - t2:.1f}s", flush=True)

fin = snapshot("重挂后")
for k in ("edge", "nodes", "copy"):
    lo, hi = base[k] * 0.99, base[k] * 1.01
    if not (lo <= fin[k] <= hi):
        print(f"[WARN] {k} 偏离基线：{base[k]} → {fin[k]}", flush=True)
print("完成", flush=True)
