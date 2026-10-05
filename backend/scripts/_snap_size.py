"""验证指纹快照：对齐后 diff 必须为零；改动/删除要能精确识别；内存要降下来。"""
import gc
import sys

sys.path.insert(0, ".")


def deep(obj, seen=None) -> int:
    seen = seen if seen is not None else set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    n = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            n += deep(k, seen) + deep(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            n += deep(v, seen)
    return n


MB = 1024 ** 2
print("脚本已启动，正在载入 store…", flush=True)
from app.store import store  # noqa: E402
print("store 载入完成", flush=True)

n_nodes = sum(len(v) for v in store._kz_snap.values())
print("节点行 %d  边行 %d" % (n_nodes, len(store._kz_edge_snap)), flush=True)
print("新快照：节点 %.1f MB  边 %.1f MB（改前 173.8 / 75.8 MB）"
      % (deep(store._kz_snap) / MB, deep(store._kz_edge_snap) / MB), flush=True)
print("书→节点 id 索引 %.1f MB" % (deep(store._kz_node_book) / MB), flush=True)


def diff_stat():
    plans_n, plans_e, *_ = store._collect_plans()
    add = sum(p["stats"][0] for p in plans_n) + sum(p["stats"][0] for p in plans_e)
    upd = sum(p["stats"][1] for p in plans_n) + sum(p["stats"][1] for p in plans_e)
    dele = sum(p["stats"][2] for p in plans_n) + sum(p["stats"][2] for p in plans_e)
    return add, upd, dele


print("① 对齐后 diff（应为 0/0/0）:", diff_stat(), flush=True)

# ② 改一个属性：应识别出 1 处变更，且是原地 SET（不是删后重建）
ev = store.events[0]
old_name = ev.name
ev.name = old_name + "（改）"
print("② 改 1 个属性:", diff_stat(), "（应为 0/1/0）", flush=True)
ev.name = old_name
print("   改回后:", diff_stat(), "（应为 0/0/0）", flush=True)

# ③ 删一个实体：应识别出 1 处删除
removed = store.events.pop(0)
print("③ 删 1 个实体:", diff_stat(), "（应为 0/0/N，N≥1：实体连带它的边）", flush=True)
store.events.insert(0, removed)
print("   放回后:", diff_stat(), "（应为 0/0/0）", flush=True)

del store
gc.collect()
