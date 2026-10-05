"""只读探针：link 提案为什么恒为 0？

离线复算《资治通鉴》提案并统计各 kind 数量，**不写库、不写产物**。
对照两组：
  A) known={}   —— 无主干快照（等价 --no-store，此前跑出 link=0）
  B) known=主干 —— 有 store 快照（真实构建链路走的就是这条）

若 B 的 link 仍为 0，说明对齐环节真的产不出 link，需要改代码；
若 B 有 link，说明此前 link=0 只是 --no-store 造成的假象。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion.bookrun import _build_opts, RunOptions  # noqa: E402
from app.ingestion.pipeline import (  # noqa: E402
    accumulate, finalize, known_from_store, new_accumulator)
from app.ingestion.resolve import Resolver  # noqa: E402
from app.store import store  # noqa: E402
from app.warehouse import warehouse  # noqa: E402

BID = "资治通鉴"
print("store 载入完成：人物 %d 事件 %d" % (len(store.people), len(store.events)),
      flush=True)
known = known_from_store(store)
print("主干快照：person_names %d  event_names %d  person_ids %d"
      % (len(known["person_names"]), len(known["event_names"]),
         len(known["person_ids"])), flush=True)

rows = warehouse.load_extract_raw(BID, "llm:sensenova-6.8-flash-lite")
print("已落盘抽取块 %d" % len(rows), flush=True)

opts = _build_opts(RunOptions(max_persons=20000, max_events=30000), BID)
print("align_mode =", opts.align_mode, flush=True)
resolver = Resolver() if True else None  # noqa: SIM108


def recount(known_arg, tag):
    acc = new_accumulator()
    t0 = time.time()
    for _idx, out in rows:
        accumulate(acc, out, known=known_arg)
    props, info = finalize(acc, known=known_arg, resolver=resolver, opts=opts)
    print(f"[{tag}] 候选 人{info['candidate_persons']} 事{info['candidate_events']} "
          f"关系{info['candidate_links']} → 提案 {len(props)} "
          f"{ {k: sum(1 for x in props if x.kind == k) for k in ('person', 'event', 'link', 'relation')} }"
          f"  耗时 {time.time() - t0:.0f}s", flush=True)
    return props


pa = recount({}, "A 无主干")
pb = recount(known, "B 有主干")

# 抽样看看 B 多出来的 link 长什么样
links_b = [x for x in pb if x.kind == "link"]
if links_b:
    print("link 样例：", flush=True)
    for x in links_b[:5]:
        print(f"   {x.name} → {x.target_id}  conf={x.confidence}", flush=True)
else:
    # 统计一下：抽取出来的 dst 里到底有多少能命中主干名字
    names = set()
    for _idx, out in rows:
        for lk in out.get("links", []):
            dst = lk.get("dst") or lk.get("target_id")
            if dst:
                names.add(dst)
    hit_p = sum(1 for n in names if n in known["person_names"])
    hit_e = sum(1 for n in names if n in known["event_names"])
    print(f"抽取到的 dst 去重 {len(names)} 个：命中主干人物名 {hit_p}、事件名 {hit_e}",
          flush=True)
    print("样例 dst：", list(names)[:10], flush=True)
