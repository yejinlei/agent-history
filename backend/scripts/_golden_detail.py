"""把黄金人物在 CBDB 里的全部地址记录摊开，反推「哪类地址＝籍贯」。

同时看**同名消歧**：同一个人名可能指着好几个 CBDB 条目，这里把每个候选人的
生卒/指数年都列出来，用来判断「取哪条」才不会张冠李戴。
"""
import math
import sqlite3
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                    # noqa: E402
from zhconv import convert as zh_convert                  # noqa: E402

DB = str(config.DATA_DIR / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db")

GOLDEN = [
    ("曹操", 115.78, 33.87), ("诸葛亮", 118.40, 35.55), ("岳飞", 114.35, 35.93),
    ("王安石", 116.30, 27.95), ("司马迁", 110.45, 35.48), ("杜甫", 112.98, 34.75),
    ("苏轼", 103.83, 30.05), ("曾国藩", 112.20, 27.48), ("张居正", 112.24, 30.35),
    ("朱元璋", 117.56, 32.87), ("李白", 104.75, 31.78), ("欧阳修", 114.99, 27.12),
    ("司马光", 111.22, 35.14), ("范仲淹", 120.62, 31.31),
]


def hav(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for name, wlon, wlat in GOLDEN:
        forms = {name, zh_convert(name, "zh-hant")}
        pids = []
        for nm in forms:
            for (pid,) in con.execute(
                    "SELECT c_personid FROM BIOG_MAIN WHERE c_name_chn = ?", (nm,)):
                if pid not in pids:
                    pids.append(pid)
        print(f"\n{'=' * 78}\n{name}  期望 {wlon},{wlat}   CBDB 同名 {len(pids)} 人")
        for pid in pids[:6]:
            r = con.execute(
                "SELECT c_index_year, c_birthyear, c_deathyear, c_dy, "
                "c_index_addr_id, c_index_addr_type_code FROM BIOG_MAIN "
                "WHERE c_personid = ?", (pid,)).fetchone()
            print(f"  候选人 pid={pid}  指数年={r[0]} 生={r[1]} 卒={r[2]} 朝={r[3]} "
                  f"index_addr={r[4]} type={r[5]}")
            for t, aid, anm, x, y in con.execute(
                    "SELECT d.c_addr_type, d.c_addr_id, a.c_name_chn, a.x_coord, "
                    "a.y_coord FROM BIOG_ADDR_DATA d LEFT JOIN ADDR_CODES a "
                    "ON a.c_addr_id = d.c_addr_id WHERE d.c_personid = ? "
                    "ORDER BY d.c_addr_type", (pid,)):
                if x is None or y is None:
                    continue
                try:
                    d = hav(float(x), float(y), wlon, wlat)
                except Exception:
                    continue
                mark = "✓" if d <= 120 else "✗"
                print(f"     type={t:<3} 《{anm}》 {float(x):.2f},{float(y):.2f} "
                      f"{mark}{d:>7.0f}km")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
