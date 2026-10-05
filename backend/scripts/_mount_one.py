"""挂载单本书：按界面默认口径（置信度 ≥40%）接受提案并提交。

    python scripts/_mount_one.py sanguozhi
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def _req(path: str, payload: dict, timeout: int = 7200):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


bid = sys.argv[1]
t0 = time.time()
st = _req("/api/mounts/start", {"book_id": bid}, timeout=3600)
props = st["proposals"]
kinds: dict[str, int] = {}
for p in props:
    kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
print(f"提案 {len(props)} {kinds}（准备耗时 {time.time() - t0:.0f}s）", flush=True)
acc = [bool(p.get("default_accepted", True)) for p in props]
print(f"接受 {sum(1 for x in acc if x)} / {len(acc)}", flush=True)
t1 = time.time()
res = _req("/api/mounts/commit", {"thread_id": st["thread_id"],
                                  "accepted": acc})
print(f"挂载结果 {json.dumps(res, ensure_ascii=False)[:500]}", flush=True)
print(f"挂载耗时 {(time.time() - t1) / 60:.1f}min", flush=True)
