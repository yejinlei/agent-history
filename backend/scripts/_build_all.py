"""剩余书籍批量构建：逐本「构建 → 挂载 → 记录」，一次只跑一本（单写者锁）。

为什么必须串行：DuckDB 与 Kuzu 都是单进程写，服务端已占着库；在线构建接口
同一时刻也只允许一个任务（409）。

挂载口径：只接受 default_accepted（置信度 ≥40%）的提案，与界面默认勾选一致，
低置信的句读碎片不进图。

用法：
    python scripts/_build_all.py                 # 构建全部未完成的书
    python scripts/_build_all.py hanshu jinshu   # 只构建指定书
    python scripts/_build_all.py --no-mount      # 只构建不挂载（先攒提案）
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

# 日志里有中文书名，重定向到文件时默认编码可能是 ascii，一打印就崩
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:      # 允许从任意目录直接跑
    sys.path.insert(0, str(_BACKEND_DIR))

BASE = "http://127.0.0.1:8000"
CONCURRENCY = 16          # 实测并发 6 时 6.4 块/分钟，提速主要靠这里
MIN_FREE_MB = 4096        # 数据盘低于此值就停：构建一本瞬时吃数 GB
POLL = 30


def _req(path: str, payload: dict | None = None, method: str | None = None,
         timeout: int = 1800):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def free_mb_of(path: str) -> float:
    import shutil
    return shutil.disk_usage(path).free / 1024 / 1024


RESTART_RSS_MB = 9000      # 服务端 RSS 超过此值就重启：Kuzu 缓冲池 + 内存实体
                           # 逐本累加，实测会被原生层静默杀死（无堆栈、无异常）


def server_rss_mb() -> int:
    """取服务端进程内存（本机同一台机器，用 CIM 查命令行含 serve.py 的进程）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*serve.py*' } | "
          "ForEach-Object { [math]::Round($_.WorkingSetSize/1MB) }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        vals = [int(x) for x in out.stdout.split() if x.isdigit()]
        return max(vals) if vals else 0
    except Exception:
        return 0


def restart_server() -> bool:
    """重启服务端：躲开「跑几本后进程被静默杀死」的问题。"""
    print("  重启服务端…", flush=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$p=(Get-NetTCPConnection -LocalPort 8000 -State Listen "
         "-ErrorAction SilentlyContinue).OwningProcess; "
         "if($p){Stop-Process -Id $p -Force}"], timeout=120)
    time.sleep(5)
    subprocess.Popen([sys.executable, "-u", "scripts/serve.py"],
                     cwd=str(_BACKEND_DIR))
    for _ in range(100):                      # 载入 Store 约 10 分钟
        time.sleep(30)
        try:
            _req("/api/health", timeout=30)
            print("  服务端已就绪", flush=True)
            return True
        except Exception:
            continue
    print("  [失败] 服务端重启超时", flush=True)
    return False


def wait_idle(timeout: int = 7200) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _req("/api/ingest", timeout=60)
        if not j.get("running"):
            return True
        time.sleep(POLL)
    return False


def build(bid: str) -> bool:
    # 书号可能是中文（《史通》《读通鉴论》），直接拼进 URL 会被 http.client
    # 按 ascii 编码而崩，必须转义
    _req(f"/api/ingest/{quote(bid, safe='')}", {"use_llm": True, "concurrency": CONCURRENCY,
                                "repair": True, "max_rounds": 2,
                                "keep_best": False}, timeout=120)
    t0 = time.time()
    while True:
        time.sleep(POLL)
        j = _req("/api/ingest", timeout=60)
        job = next((x for x in j["jobs"] if x["book_id"] == bid
                    and x["state"] == "running"), None)
        if job is None:
            break
        if time.time() - t0 > 10800:          # 单本 3 小时兜底
            print(f"  [超时] {bid} 构建超过 3 小时，放弃", flush=True)
            return False
        print(f"  …{bid} {job['processed']}/{job['todo']}", flush=True)
    job = next((x for x in _req("/api/ingest", timeout=60)["jobs"]
                if x["book_id"] == bid), None)
    ok = bool(job and job.get("state") == "done")
    print(f"  构建{'' if ok else '未'}完成：state={job and job.get('state')} "
          f"error={job and job.get('error')} 耗时 {(time.time() - t0) / 60:.1f}min",
          flush=True)
    return ok


def mount(bid: str) -> dict | None:
    st = _req("/api/mounts/start", {"book_id": bid}, timeout=3600)
    props = st["proposals"]
    kinds: dict[str, int] = {}
    for p in props:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"  提案 {len(props)} {kinds}", flush=True)
    acc = [bool(p.get("default_accepted", True)) for p in props]
    print(f"  接受 {sum(1 for x in acc if x)} / {len(acc)}", flush=True)
    res = _req("/api/mounts/commit",
               {"thread_id": st["thread_id"], "accepted": acc}, timeout=7200)
    return res


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    no_mount = "--no-mount" in sys.argv
    from app import config          # 与本进程同一套解析：CHRONOSINO_DATA_DIR
    data_dir = Path(config.DATA_DIR)
    # 书目以清单为准：store 里只有「已入库」的书，清单里那 24 本正史从未
    # 构建过，根本不在 /api/books 里（构建第一次才会入库）。
    manifest = json.loads((data_dir / "books_manifest.json").read_text(
        encoding="utf-8"))
    mounted = {b["id"] for b in _req("/api/books", timeout=300)["books"]
               if b["mounted"]}
    # 小书在前：先铺开覆盖面，大书（宋史 14MB）放最后，出问题也只影响尾部
    def size_of(bid: str) -> int:
        f = data_dir / "books" / manifest[bid].get("file", f"{bid}.txt")
        return f.stat().st_size if f.exists() else 0

    ids = [bid for bid in manifest if bid not in mounted]
    if args:
        ids = [bid for bid in ids if bid in args]
    ids.sort(key=size_of)
    print(f"待构建 {len(ids)} 本：{ids}", flush=True)
    todo = [{"id": bid, "title": manifest[bid].get("title", bid)}
            for bid in ids]
    for i, b in enumerate(todo, 1):
        free = free_mb_of(str(data_dir))
        if free < MIN_FREE_MB:
            print(f"[中止] 数据盘只剩 {free:.0f} MB，低于 {MIN_FREE_MB} MB",
                  flush=True)
            return
        print(f"=== [{i}/{len(todo)}] 《{b['title']}》( {b['id']} ) "
              f"盘余 {free:.0f} MB ===", flush=True)
        t0 = time.time()
        try:
            wait_idle()
            if not build(b["id"]):
                continue
            if no_mount:
                continue
            res = mount(b["id"])
            print(f"  挂载结果 {json.dumps(res, ensure_ascii=False)[:300]}",
                  flush=True)
        except urllib.error.HTTPError as e:
            print(f"  [失败] HTTP {e.code} {e.read()[:200]}", flush=True)
            continue
        except Exception as e:      # 服务端被原生层静默杀死时是连接错误，不是 HTTP 错误
            print(f"  [连接中断] {type(e).__name__}: {e}", flush=True)
            if not restart_server():
                print("[中止] 服务拉不起来，停止批量构建", flush=True)
                return
        print(f"  本本耗时 {(time.time() - t0) / 60:.1f}min", flush=True)
        rss = server_rss_mb()
        print(f"  服务端内存 {rss} MB", flush=True)
        if rss > RESTART_RSS_MB and not restart_server():
            return


if __name__ == "__main__":
    main()
