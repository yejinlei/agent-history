# -*- coding: utf-8 -*-
"""主干权威数据本地化抓取器（CIDOC CRM / OWL-Time / Wikidata / CHGIS / CBDB）。

定位：这些是**主干**（ground truth，origin=core）；
《史记》《二十四史》《资治通鉴》等书是**扩展知识**（from_book + dispute）。

用法：
    python scripts/fetch_core.py                 # 全量并发抓取（默认）
    python scripts/fetch_core.py --only owltime wikidata
    python scripts/fetch_core.py --discover      # 只探测/列出候选，不下载
    python scripts/fetch_core.py --wikidata-rows 3000
产物：
    app/data/core/raw/<source>/...
    app/data/core/manifest.json   # 每个文件的 url / bytes / sha256 / 抓取时间
    app/data/core/SOURCES.md      # 人可读的来源清单（自动追加）
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_CORE = _BACKEND / "app" / "data" / "core"
_RAW = _CORE / "raw"
UA = "ChronoSino/0.1 (local research build; contact: local)"

# ---------------------------------------------------------------- 抓取源定义
# 每个源：候选 URL 列表（逐个尝试，谁先通就用谁）
CIDOC_CANDIDATES = [
    "https://cidoc-crm.org/owl/7.1.3/CIDOC_CRM_v7.1.3.owl",
    "https://cidoc-crm.org/owl/7.1.2/CIDOC_CRM_v7.1.2.owl",
    "http://cidoc-crm.org/sites/default/files/CIDOCCRM_ecrm.owl",
    "https://cidoc-crm.org/cidoc-crm/",
]
OWLTIME_CANDIDATES = [
    "https://www.w3.org/2006/time.rdf",
    "https://www.w3.org/2006/time.owl",
]
# Harvard Dataverse（CHGIS / CBDB 官网自身已不可达，走 Dataverse API 发现）
DV_API = "https://dataverse.harvard.edu/api"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"

# 收紧范围：只要「与中国史相关」的人物，避免把乔治·华盛顿这类混进主干。
# 1) anchor：带 CBDB ID（P1749）的人物 —— Wikidata ↔ CBDB 的交叉锚点，对齐价值最高
# 2) cn：国籍中国（Q148）的人物 —— 补充覆盖
_SPARQL_ANCHOR = """
SELECT ?p ?pLabel ?cbdb ?birth ?death WHERE {{
  ?p wdt:P31 wd:Q5 ;
     wdt:P1749 ?cbdb ;
     rdfs:label ?pLabel .
  FILTER(LANG(?pLabel) = "zh")
  OPTIONAL {{ ?p wdt:P569 ?birth. }}
  OPTIONAL {{ ?p wdt:P570 ?death. }}
}}
LIMIT {limit} OFFSET {offset}
"""

_SPARQL_CN = """
SELECT ?p ?pLabel ?birth ?death WHERE {{
  ?p wdt:P31 wd:Q5 ;
     wdt:P27 wd:Q148 ;
     rdfs:label ?pLabel .
  FILTER(LANG(?pLabel) = "zh")
  OPTIONAL {{ ?p wdt:P569 ?birth. }}
  OPTIONAL {{ ?p wdt:P570 ?death. }}
}}
LIMIT {limit} OFFSET {offset}
"""

WIKIDATA_QUERIES = (("persons_anchor", _SPARQL_ANCHOR), ("persons_cn", _SPARQL_CN))


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": UA})


def download(url: str, dest: Path, tries: int = 4, pause: float = 2.0,
             stall: float = 60.0) -> dict | None:
    """下载到 dest：支持断点续传、停滞检测、重试（幂等）。

    Dataverse 未登录时不返回 Content-Length，所以「下完没下完」只能靠
    流的自然结束判断；停滞 stall 秒无新数据即判定卡死，重试用 Range 继续。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for i in range(1, tries + 1):
        try:
            with _client() as cli:
                head = cli.head(url)
                size = int(head.headers.get("Content-Length") or 0)
                if dest.exists() and size and dest.stat().st_size == size:
                    return {"url": url, "file": str(dest.relative_to(_CORE)),
                            "bytes": size, "sha256": sha256_of(dest),
                            "cached": True}
                done = tmp.stat().st_size if tmp.exists() else 0
                headers = {"Range": f"bytes={done}-"} if done else {}
                with cli.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416:          # 已完整
                        tmp.replace(dest)
                        return {"url": url, "file": str(dest.relative_to(_CORE)),
                                "bytes": dest.stat().st_size,
                                "sha256": sha256_of(dest), "cached": True}
                    r.raise_for_status()
                    mode = "ab" if r.status_code == 206 else "wb"
                    if mode == "wb":
                        done = 0
                    n, last = done, time.monotonic()
                    with open(tmp, mode) as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            n += len(chunk)
                            if time.monotonic() - last > stall:
                                raise TimeoutError(
                                    f"停滞 {stall}s 无数据（已下 {n/1e6:.1f} MB）")
                            last = time.monotonic()
                    tmp.replace(dest)
                return {"url": url, "file": str(dest.relative_to(_CORE)),
                        "bytes": dest.stat().st_size,
                        "sha256": sha256_of(dest), "cached": False}
        except Exception as e:
            log(f"  重试 {i}/{tries}：{e.__class__.__name__}: {str(e)[:80]}"
                f"（已有 {tmp.stat().st_size/1e6:.1f} MB，下次续传）"
                if tmp.exists() else f"  重试 {i}/{tries}：{e.__class__.__name__}")
            time.sleep(pause * i)
    return None


def fetch_first(candidates: list[str], subdir: str, name: str) -> dict | None:
    for url in candidates:
        try:
            with _client(20) as cli:
                r = cli.head(url)
            if r.status_code >= 400:
                continue
        except Exception:
            continue
        ext = Path(url).suffix
        if not ext or url.endswith("/"):
            ext = ".html"
        dest = _RAW / subdir / f"{name}{ext}"
        log(f"{name}: 命中 {url}")
        return download(url, dest)
    log(f"{name}: 所有候选均不可达")
    return None


# ---------------------------------------------------------------- Dataverse
def dv_search(query: str, per_page: int = 10) -> list[dict]:
    with _client(30) as cli:
        r = cli.get(f"{DV_API}/search",
                    params={"q": query, "type": "dataset", "per_page": per_page})
        r.raise_for_status()
        return [it for it in r.json().get("data", {}).get("items", [])]


def dv_files(doi: str) -> list[dict]:
    with _client(30) as cli:
        r = cli.get(f"{DV_API}/datasets/:persistentId/",
                    params={"persistentId": f"doi:{doi}"})
        r.raise_for_status()
        return r.json().get("data", {}).get("latestVersion", {}).get("files", [])


def dv_pick(items: list[dict], keywords: tuple[str, ...],
            max_bytes: int, exclude_ext: tuple[str, ...] = (),
            prefer_ext: tuple[str, ...] = ()) -> tuple[str, dict] | None:
    """按关键词挑数据集，再挑体积合适、格式可解析的文件。

    Dataverse 未登录时 size 恒为 0，因此 max_bytes 只能作为兜底；
    真正的选择靠扩展名白/黑名单（.mdb 这类 Access 二进制解析成本高，排除）。
    """
    for it in items:
        if not any(k.lower() in (it.get("name") or "").lower() for k in keywords):
            continue
        doi = (it.get("global_id") or "").replace("doi:", "")
        if not doi:
            continue
        try:
            files = dv_files(doi)
        except Exception as e:
            log(f"  {it.get('name')}: 文件列表失败 {e.__class__.__name__}")
            continue

        def ok(f: dict) -> bool:
            fn = (f.get("dataFile", {}).get("filename") or "").lower()
            if exclude_ext and fn.endswith(exclude_ext):
                return False
            size = int(f.get("dataFile", {}).get("size", 0))
            return size <= max_bytes or size == 0

        cands = [f for f in files if ok(f)]
        if prefer_ext:
            pref = [f for f in cands
                    if (f.get("dataFile", {}).get("filename") or "").lower()
                    .endswith(prefer_ext)]
            if pref:
                cands = pref
        cands.sort(key=lambda f: -int(f.get("dataFile", {}).get("size", 0)))
        if cands:
            return doi, cands[0]
    return None


def fetch_dataverse(query: str, keywords: tuple[str, ...], subdir: str,
                    max_bytes: int, exclude_ext: tuple[str, ...] = (),
                    prefer_ext: tuple[str, ...] = ()) -> dict | None:
    try:
        items = dv_search(query)
    except Exception as e:
        log(f"{subdir}: 搜索失败 {e.__class__.__name__}")
        return None
    picked = dv_pick(items, keywords, max_bytes, exclude_ext, prefer_ext)
    if not picked:
        log(f"{subdir}: 未找到合适数据集")
        return None
    doi, f = picked
    fid = f.get("dataFile", {}).get("id")
    fname = f.get("dataFile", {}).get("filename") or f"datafile{fid}"
    size = int(f.get("dataFile", {}).get("size", 0))
    log(f"{subdir}: 选中 {fname}（{size/1e6:.1f} MB，doi {doi}）")
    dest = _RAW / subdir / fname
    return download(f"{DV_API}/access/datafile/{fid}", dest)


# ---------------------------------------------------------------- Wikidata
def fetch_wikidata(rows: int, batch: int) -> dict | None:
    """按两组查询拉取人物子集（锚点组 / 中国籍组），分别落盘。"""
    out_dir = _RAW / "wikidata"
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: dict[str, dict] = {}
    with _client(120) as cli:
        for name, tmpl in WIKIDATA_QUERIES:
            got: list[dict] = []
            offset = 0
            while offset < rows:
                q = tmpl.format(limit=min(batch, rows - offset), offset=offset)
                bindings = None
                for attempt in range(1, 4):
                    try:
                        r = cli.get(WIKIDATA_ENDPOINT,
                                    params={"query": q, "format": "json"})
                        if r.status_code == 429 or r.status_code >= 500:
                            time.sleep(5 * attempt)
                            continue
                        r.raise_for_status()
                        bindings = r.json()["results"]["bindings"]
                        break
                    except Exception as e:
                        log(f"  {name} 重试 {attempt}: {e.__class__.__name__}")
                        time.sleep(4 * attempt)
                if bindings is None:
                    log(f"{name}: 连续失败，停止翻页")
                    break
                got.extend(bindings)
                log(f"{name}: +{len(bindings)} 行（累计 {len(got)}）")
                if not bindings or len(bindings) < batch:
                    break
                offset += batch
                time.sleep(2.0)      # 公共端点保守限速
            if got:
                dest = out_dir / f"{name}.json"
                dest.write_text(json.dumps(got, ensure_ascii=False),
                                encoding="utf-8")
                parts[name] = {"file": f"raw/wikidata/{name}.json",
                               "rows": len(got), "bytes": dest.stat().st_size}
    if not parts:
        return None
    return {"url": WIKIDATA_ENDPOINT, "file": "raw/wikidata/",
            "bytes": sum(v["bytes"] for v in parts.values()),
            "rows": sum(v["rows"] for v in parts.values()),
            "parts": parts}


# ---------------------------------------------------------------- 编排
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*",
                    choices=["cidoc", "owltime", "wikidata", "chgis", "cbdb"],
                    help="只抓指定源")
    ap.add_argument("--discover", action="store_true", help="只探测不下载")
    ap.add_argument("--wikidata-rows", type=int, default=3000)
    ap.add_argument("--wikidata-batch", type=int, default=500)
    ap.add_argument("--max-bytes", type=int, default=400 * 1024 * 1024,
                    help="单个大数据集文件大小上限（默认 400MB）")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    _CORE.mkdir(parents=True, exist_ok=True)
    want = set(a.only or ["cidoc", "owltime", "wikidata", "chgis", "cbdb"])

    if a.discover:
        log("探测模式：")
        for url in CIDOC_CANDIDATES + OWLTIME_CANDIDATES:
            try:
                with _client(15) as cli:
                    code = cli.head(url).status_code
            except Exception as e:
                code = e.__class__.__name__
            log(f"  {code}  {url}")
        for q in ("CHGIS", "CBDB"):
            try:
                for it in dv_search(q, 6):
                    log(f"  DV[{q}] {it.get('name')} → {it.get('global_id')}")
            except Exception as e:
                log(f"  DV[{q}] 搜索失败 {e.__class__.__name__}")
        return 0

    jobs: dict[str, callable] = {}
    if "cidoc" in want:
        jobs["cidoc"] = lambda: fetch_first(CIDOC_CANDIDATES, "cidoc", "cidoc_crm")
    if "owltime" in want:
        jobs["owltime"] = lambda: fetch_first(OWLTIME_CANDIDATES, "owltime", "owl_time")
    if "wikidata" in want:
        jobs["wikidata"] = lambda: fetch_wikidata(a.wikidata_rows, a.wikidata_batch)
    if "chgis" in want:
        # 优先 MySQL dump（纯文本 SQL，可直接解析）；.mdb 是 Access 二进制，排除
        jobs["chgis"] = lambda: fetch_dataverse(
            "CHGIS", ("relational", "time series", "tgaz", "v6", "v5"),
            "chgis", a.max_bytes,
            exclude_ext=(".mdb", ".jpg", ".pdf"),
            prefer_ext=(".zip", ".csv", ".txt"))
    if "cbdb" in want:
        # 优先 .db（可直接 sqlite3 打开）；.7z 需额外解压工具
        jobs["cbdb"] = lambda: fetch_dataverse(
            "CBDB", ("sqlite", "access"), "cbdb", a.max_bytes,
            exclude_ext=(".7z", ".mdb"),
            prefer_ext=(".db", ".sqlite", ".sqlite3", ".zip"))

    results: dict[str, dict | None] = {}
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                log(f"{name}: 异常 {e.__class__.__name__}: {e}")
                results[name] = None

    manifest_path = _CORE / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) \
        if manifest_path.exists() else {}
    old.update({k: v for k, v in results.items() if v})
    old["_updated"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(old, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    lines = ["# 主干数据来源（core）", "",
             "抓取器：`scripts/fetch_core.py`；明细见 `manifest.json`。", "",
             "| 源 | 状态 | 文件 | 大小 |", "|---|---|---|---|"]
    for name in ("cidoc", "owltime", "wikidata", "chgis", "cbdb"):
        r = results.get(name)
        if r:
            size = r.get("bytes", 0)
            lines.append(f"| {name} | ✅ | `{r['file']}` | {size/1e6:.2f} MB |")
        elif name in want:
            lines.append(f"| {name} | ❌ 未获取 | — | — |")
    lines += ["", f"更新时间：{datetime.now():%Y-%m-%d %H:%M:%S}", "",
              "许可与署名：CIDOC CRM（ISO 21127）、OWL-Time（W3C）、"
              "Wikidata（CC0）、CHGIS（Harvard Dataverse）、CBDB（Harvard）；"
              "导入前请核对各自许可条款。", ""]
    (_CORE / "SOURCES.md").write_text("\n".join(lines), encoding="utf-8")

    got = sum(1 for v in results.values() if v)
    log(f"完成：{got}/{len(want)} 个源已落盘 → {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
