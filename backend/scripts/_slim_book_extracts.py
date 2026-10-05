"""把 Book 节点里过大的 extracts 列清空（真值源是 data/ingest/<id>.json）。

背景：提案整包内联在 Book 节点里，单本可达 26 MB 字符串。Kuzu 扫描时必须把
这些巨型行整页 pin 住，缓冲池会直接爆（"buffer pool is full"），写一半被中断
还会留下坏 JSON 让服务起不来。库本身没坏（单独打开 5 秒、计数正常），
所以用脚本直连 Kuzu 把这些列瘦身，服务就能正常载入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
import kuzu  # noqa: E402

db_path = Path(config.DATA_DIR) / "kg" / "kuzu_db"
db = kuzu.Database(str(db_path),
                   buffer_pool_size=int(config.KUZU_BUFFER_POOL_GB * 1024 ** 3))
conn = kuzu.Connection(db)

qr = conn.execute("MATCH (n:Book) RETURN n.id, size(n.extracts)")
big = []
while qr.has_next():
    bid, size = qr.get_next()
    if size > 100000:
        big.append((bid, size))
print("巨型 extracts 的书：%d 本" % len(big), flush=True)
for bid, size in big[:10]:
    print(f"  {bid}: {size/1048576:.1f} MB", flush=True)

for bid, _size in big:
    conn.execute("MATCH (n:Book {id:$bid}) SET n.extracts = '[]'", {"bid": bid})
print("已清空 %d 本" % len(big), flush=True)

qr2 = conn.execute("MATCH (n:Book) RETURN n.id, size(n.extracts)")
total = mx = 0
while qr2.has_next():
    _bid, size = qr2.get_next()
    total += size
    mx = max(mx, size)
print(f"清完：Book.extracts 总字符数 {total}（{total/1024:.0f} KB），"
      f"最大单行 {mx}", flush=True)
