# -*- coding: utf-8 -*-
"""批量预取 OSGeo 历史地图瓦片到本地缓存（backend/app/data/tiles/）。

用法（在 backend/ 目录下）：
    python scripts/prefetch_tiles.py                 # 全部注册地图，z=3..5，中国范围
    python scripts/prefetch_tiles.py --zmin 3 --zmax 6
    python scripts/prefetch_tiles.py --only mp0305 mp030b

已存在的瓦片自动跳过；上游 404（该区域无图）不计入下载。
完成后 tiles/ 目录可直接 git add 提交。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "app" / "data"
TILE_DIR = DATA_DIR / "tiles"
MAPS_JSON = DATA_DIR / "maps.json"
UPSTREAM = "https://tile.osgeo.cn/wmts"

# 中国范围（web mercator 覆盖框）
LON_MIN, LON_MAX = 73.0, 135.5
LAT_MIN, LAT_MAX = 17.0, 54.0


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_range(z: int) -> tuple[range, range]:
    x0, y1 = lonlat_to_tile(LON_MIN, LAT_MIN, z)  # 西北角（y 大）
    x1, y0 = lonlat_to_tile(LON_MAX, LAT_MAX, z)  # 东南角（y 小）
    return range(x0, x1 + 1), range(y0, y1 + 1)


def _norm_tile(tile: str) -> str:
    """OSGeo WMTS 图层名带 mp 前缀（maps.json 里的 p0152 -> mp0152）。"""
    return tile if tile.startswith("m") else "m" + tile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zmin", type=int, default=3)
    ap.add_argument("--zmax", type=int, default=5)
    ap.add_argument("--only", nargs="*", default=None, help="只预取指定 tile id")
    ap.add_argument("--proxy", default=None,
                    help="HTTP 代理，如 http://127.0.0.1:8001（本机直连不通时使用）")
    ap.add_argument("--workers", type=int, default=16, help="并发下载线程数")
    args = ap.parse_args()

    groups: dict[str, list[dict]] = json.loads(MAPS_JSON.read_text(encoding="utf-8"))
    tiles = sorted({_norm_tile(m["tile"]) for ms in groups.values() for m in ms})
    if args.only:
        want = {_norm_tile(t) for t in args.only}
        tiles = [t for t in tiles if t in want]
    print(f"共 {len(tiles)} 幅地图，zoom {args.zmin}..{args.zmax}，并发 {args.workers}"
          + (f"，代理 {args.proxy}" if args.proxy else ""), flush=True)

    # 汇总全部待下载任务（跳过已存在）
    tasks: list[tuple[str, int, int, int, Path]] = []
    skip = 0
    for tile in tiles:
        for z in range(args.zmin, args.zmax + 1):
            xs, ys = tile_range(z)
            for x in xs:
                for y in ys:
                    local = TILE_DIR / tile / str(z) / str(x) / f"{y}.png"
                    if local.exists():
                        skip += 1
                        continue
                    tasks.append((tile, z, x, y, local))
    print(f"待下载 {len(tasks)} 张，已存在跳过 {skip} 张", flush=True)

    counters = {"done": 0, "miss": 0, "fail": 0}
    lock = threading.Lock()

    def fetch_one(cli: httpx.Client, t: tuple[str, int, int, int, Path]) -> None:
        tile, z, x, y, local = t
        url = f"{UPSTREAM}/{tile}/webmercator/{z}/{x}/{y}.png"
        try:
            r = cli.get(url)
        except httpx.HTTPError:
            with lock:
                counters["fail"] += 1
            return
        if r.status_code != 200 or not r.content:
            with lock:
                counters["miss"] += 1
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".png.tmp")
        tmp.write_bytes(r.content)
        tmp.replace(local)
        with lock:
            counters["done"] += 1
            n = counters["done"] + counters["miss"] + counters["fail"]
            if n % 200 == 0 or n == len(tasks):
                print(f"  进度 {n}/{len(tasks)}：下载{counters['done']} "
                      f"无图{counters['miss']} 失败{counters['fail']}", flush=True)

    with httpx.Client(timeout=15, follow_redirects=True,
                      proxy=args.proxy) as cli:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(lambda t: fetch_one(cli, t), tasks))

    print(f"完成：下载 {counters['done']}，已存在跳过 {skip}，"
          f"上游无图 {counters['miss']}，失败 {counters['fail']}")
    print(f"缓存目录：{TILE_DIR}")
    return 0 if counters["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
