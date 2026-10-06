"""查清 CBDB 各类人物地址到底代表什么（哪个才是真籍贯）。"""
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

DB = str(config.DATA_DIR / "core" / "raw" / "cbdb" / "CBDB_20240208_sqlite.db")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

tables = [t[0] for t in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND (name LIKE '%ADDR_TYPE%' OR name LIKE '%ADDRESS_TYPE%')").fetchall()]
print("=== 地址类型表 ===")
for t in tables:
    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})").fetchall()]
    print(f"\n表 {t} 列: {cols}")
    for r in con.execute(f"SELECT * FROM {t}").fetchall():
        print("   ", r)

print("\n=== BIOG_ADDR_DATA 里各类型记录数（含存疑文件 Billy）===")
rows = con.execute(
    "SELECT c_addr_type, count(*) FROM BIOG_ADDR_DATA "
    "GROUP BY 1 ORDER BY 2 DESC LIMIT 15").fetchall()
for t, n in rows:
    print(f"   类型 {t} : {n:,} 条")

print("\n=== ADDRESSES 表结构（有别于 ADDR_CODES）===")
cols = [c[1] for c in con.execute("PRAGMA table_info(ADDRESSES)").fetchall()]
print("   列:", cols)
try:
    for r in con.execute("SELECT * FROM ADDRESSES LIMIT 2").fetchall():
        print("   样本:", r)
except Exception as e:
    print("   读取失败:", e)

print("\n=== BIOG_MAIN 关键列 ===")
cols = [c[1] for c in con.execute("PRAGMA table_info(BIOG_MAIN)").fetchall()]
print("   ", cols[:24])
con.close()
