"""把「已有产物、但未挂载」的书挂进图库。

与 _build_all.py 的区别：这本**不重跑 LLM**，直接吃 data/ingest/<id>.json 的现成提案，
走线上三步：

    /api/ingest/{id}/apply  产物 → Book.extracts（不挂载）
    /api/mounts/start       prepare + 完整性告警 + SHACL 校验，图在 await_approval 中断
    /api/mounts/commit      送 accepted 位图（这里默认全收，人工确认在界面上做）

用法：
    python scripts/_mount_ready.py                 # 自动挑「未挂载且有产物」的书
    python scripts/_mount_ready.py houhanshu 史通   # 指定书 id
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402

BASE = "http://127.0.0.1:8000"


def _post(path: str, payload: dict | None = None, timeout: int = 3600) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: int = 120) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def mounted_ids() -> set:
    try:
        return {b["id"] for b in _get("/api/books")["books"] if b.get("mounted")}
    except Exception as e:                                  # noqa: BLE001
        print("取书目失败：%s" % e, flush=True)
        return set()


def auto_targets() -> list:
    """未挂载 + 有产物（且产物不是被中断的空壳）。"""
    done = mounted_ids()
    out = []
    for p in sorted((Path(config.DATA_DIR) / "ingest").glob("*.json")):
        if p.stem in done:
            continue
        if p.stat().st_size < 20 * 1024:      # <20 KB 视为中断残留，留给构建重跑
            print("跳过（产物过小，疑似中断）：%s %d KB"
                  % (p.stem, p.stat().st_size // 1024), flush=True)
            continue
        out.append(p.stem)
    return out


def mount_one(bid: str) -> bool:
    t0 = time.time()
    print("=== %s ===" % bid, flush=True)
    try:
        ap = _post("/api/ingest/%s/apply" % urllib.parse.quote(bid), {}, timeout=600)
    except urllib.error.HTTPError as e:
        print("  apply 失败：%s %s" % (e.code, e.read().decode("utf-8")[:200]), flush=True)
        return False
    print("  apply：提案 %s 条，warnings=%s"
          % (ap.get("proposals"), ap.get("warnings")), flush=True)

    try:
        st = _post("/api/mounts/start", {"book_id": bid}, timeout=900)
    except urllib.error.HTTPError as e:
        print("  start 失败：%s %s" % (e.code, e.read().decode("utf-8")[:200]), flush=True)
        return False
    n = len(st.get("proposals") or [])
    print("  start：提案 %d 条，warnings=%s" % (n, st.get("warnings")), flush=True)
    if not n:
        print("  提案为 0，跳过", flush=True)
        return False

    try:
        res = _post("/api/mounts/commit",
                    {"thread_id": st["thread_id"], "accepted": [True] * n})
    except urllib.error.HTTPError as e:
        print("  commit 失败：%s %s" % (e.code, e.read().decode("utf-8")[:300]), flush=True)
        return False
    print("  commit：%s（%.0f 秒）" % (res.get("result"), time.time() - t0), flush=True)
    return True


def main() -> int:
    ids = sys.argv[1:] or auto_targets()
    if not ids:
        print("没有待挂载的书", flush=True)
        return 0
    print("待挂载 %d 本：%s" % (len(ids), "、".join(ids)), flush=True)
    ok = 0
    for bid in ids:
        if mount_one(bid):
            ok += 1
    print("完成 %d / %d 本" % (ok, len(ids)), flush=True)
    return 0 if ok == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())
