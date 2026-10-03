# -*- coding: utf-8 -*-
"""本地冷备份：保护「不入库但丢了很贵」的数据。

版本库里有的是瓦片、书、模型、主干派生表和代码；
**不在**版本库里、但重建代价最高的三样是：
    app/data/kg/        Kuzu 图库真值源（实体 + 语义边）
    app/data/warehouse/ DuckDB（切块原文 chunks、LLM 抽取结果 extract_raw、进度台账）
    app/data/core/raw/  主干原始大件（可重下，但 877 MB 很慢，故只备份 manifest）

建议**停服后**执行（Kuzu/DuckDB 是单写者，运行中复制可能拿到中间状态）。

用法：
    python scripts/backup_data.py                 # 备份到 _backup/<时间戳>/，保留最近 3 份
    python scripts/backup_data.py --keep 5        # 保留最近 5 份
    python scripts/backup_data.py --what kg       # 只备份图库
    python scripts/backup_data.py --list          # 列出现有备份
恢复：
    停服 → 把备份目录里的 kg/、warehouse/ 覆盖回 app/data/ → 启服 → e2e_flow.py 验收
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_BACKEND = Path(__file__).resolve().parent.parent
_DATA = _BACKEND / "app" / "data"
_BACKUP = _BACKEND / "_backup"

TARGETS = {
    # 名称: (源路径, 是否目录)
    "kg": (_DATA / "kg", True),
    "warehouse": (_DATA / "warehouse", True),
    "core": (_DATA / "core", True),          # 只取 manifest/SOURCES，raw 太大不备份
    "ingest": (_DATA / "ingest", True),      # 离线抽取产物（小）
}


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def copy_one(src: Path, dst: Path, is_dir: bool) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return size_of(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", nargs="*", choices=list(TARGETS),
                    help="备份哪些（默认全部）")
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        if not _BACKUP.exists():
            print("暂无备份")
            return 0
        for d in sorted(_BACKUP.iterdir()):
            mb = size_of(d) / 1e6
            print(f"  {d.name}  {mb:,.1f} MB")
        return 0

    what = a.what or list(TARGETS)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _BACKUP / stamp
    dest.mkdir(parents=True, exist_ok=True)
    print(f"备份 → {dest}")

    t0 = time.time()
    total = 0
    for name in what:
        src, is_dir = TARGETS[name]
        if not src.exists():
            print(f"  跳过 {name}（不存在）")
            continue
        if name == "core":                     # 只备份元数据，原始大件靠 fetch_core.py 重建
            for f in ("manifest.json", "SOURCES.md"):
                p = src / f
                if p.exists():
                    total += copy_one(p, dest / "core" / f, False)
            print(f"  core: manifest/SOURCES 已备份")
            continue
        n = copy_one(src, dest / name, is_dir)
        total += n
        print(f"  {name}: {n/1e6:,.1f} MB")

    print(f"完成：{total/1e6:,.1f} MB，用时 {time.time()-t0:.0f}s")

    olds = sorted(d for d in _BACKUP.iterdir() if d.is_dir())[:-a.keep]
    for d in olds:
        shutil.rmtree(d)
        print(f"  清理旧备份 {d.name}（保留最近 {a.keep} 份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
