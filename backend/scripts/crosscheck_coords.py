# -*- coding: utf-8 -*-
"""用 CHGIS 交叉验证已采纳的 CBDB 坐标，揪出「CBDB 自己填错」的条目。

为什么能做这件事
----------------
CBDB 里有个别条目坐标是错的（已实证：addr 4042《益州》被填到吉林）。靠逐个看是
看不完的，但手上正好有从原始 ZIP 救回来的 CHGIS 12.2 万条地名坐标
（core/normalized/chgis_coords.jsonl）——它是一个**独立来源**，两者对同一地名若相距
很远，就至少有一方错了，值得人工复核。

注意 CHGIS 是清代以降的地名体系（见 §10.8），所以：
- **查不到**（两地名字典里没有）不算证据，直接跳过——它本来就不收先秦汉唐地名
- **对得上**才算交叉确认通过将CBDB 的采用(prfávky)2019 坐标认可
- 只在「同一个简体地名」下比较；政区治所千年间搬过家的会有假警报，所以本脚本
  只出报告、不自动改数据。

用法：python scripts/crosscheck_coords.py [距离阈值km，默认 100]
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402

import duckdb                                             # noqa: E402
from zhconv import convert as zh_convert                  # noqa: E402

CHGIS = Path(config.DATA_DIR) / "core" / "normalized" / "chgis_coords.jsonl"
DUCK = str(config.DATA_DIR / "warehouse" / "chronosino.duckdb")
THRESHOLD_KM = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0


def hav(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def load_chgis() -> dict[str, list[tuple[float, float, str]]]:
    """简体地名 → [(lon, lat, 原名)]。"""
    idx: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    with CHGIS.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            lon, lat = d.get("lon"), d.get("lat")
            if lon is None or lat is None:
                continue
            idx[zh_convert((d.get("name") or "").strip(), "zh-cn")].append(
                (lon, lat, d.get("name") or ""))
    return idx


def main() -> int:
    chgis = load_chgis()
    print(f"CHGIS 词典：{len(chgis):,} 个简体地名")

    con = duckdb.connect(DUCK, read_only=True)
    rows = con.execute(
        "SELECT place, cbdb_addr_id, addr_name, lon, lat, how, count(*) "
        "FROM event_coords GROUP BY 1,2,3,4,5,6 ORDER BY 7 DESC").fetchall()
    con.close()
    print(f"已采纳的坐标组合 {len(rows):,} 组（按 地名+词条+坐标 聚合）")

    checked = skipped = agree = 0
    suspects: list[tuple] = []
    for place, aid, aname, lon, lat, how, cnt in rows:
        cands = chgis.get(zh_convert(place, "zh-cn"), [])
        if not cands:
            skipped += 1
            continue
        checked += 1
        best = min(hav(lon, lat, c[0], c[1]) for c in cands)
        if best <= THRESHOLD_KM:
            agree += 1
        else:
            suspects.append((round(best), place, aid, aname, lon, lat, how, cnt,
                             cands[0][2], cands[0][0], cands[0][1]))
    print(f"\n可在 CHGIS 里核到的 {checked:,} 组（另有 {skipped:,} 组 CHGIS 不收，无从下手）")
    print(f"  两源相符（≤{THRESHOLD_KM:.0f} km） {agree:,} 组")
    print(f"  **相距过远，需人工复核** {len(suspects)} 组")

    suspects.sort(key=lambda x: -x[0])
    print("\n=== 疑似坐标有误（按偏差距离降序）===")
    print(f"{'偏差km':>7} {'图上place':<10} {'词条':<12} {'CBDB坐标':<22} "
          f"{'CHGIS同名首选':<12} {'事件数':>5}  来源档位")
    for s in suspects:
        print(f"{s[0]:>7} {s[1]:<10} {str(s[3]):<12} "
              f"{str(s[4]) + ',' + str(s[5]):<22} "
              f"{str(s[8]):<12} {s[7]:>5}  {s[6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
