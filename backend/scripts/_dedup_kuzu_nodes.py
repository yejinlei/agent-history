"""清理 Kuzu 图库里「同 id 重复」的节点（**需停服运行**）。

为什么会有重复：挂载中途失败（缓冲池爆 / 进程被杀）时，已写进 Kuzu 的那部分
节点留了下来；重试时事件 id 是按 `<book_id>_e<n>` 重新从 0 生成的，于是同一个
id 被写了两遍。

后果不只是冗余：`entities` 副本主键是 (kind, entity_id)，重复行会让 INSERT 抛
UNIQUE 冲突，**整批副本写不进去**，而副本是时间窗/空间窗查询的唯一来源。

做法：对每个重复 id，读出第一行属性 → DETACH DELETE 掉该 id 的全部行 →
重新 CREATE 一行。属性按原样回填，不丢字段。

用法：
    python scripts/_dedup_kuzu_nodes.py          # 报告 + 修复
    python scripts/_dedup_kuzu_nodes.py --dry    # 只报告不动手
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
import kuzu  # noqa: E402

# 各标签的主键列：Source 是 sid，其余是 id
LABELS = {
    "Dynasty": "id", "Event": "id", "Person": "id", "Place": "id",
    "Book": "id", "Source": "sid", "Institution": "id",
    "Artifact": "id", "Concept": "id",
}


def main() -> int:
    dry = "--dry" in sys.argv
    db_path = Path(config.DATA_DIR) / "kg" / "kuzu_db"
    db = kuzu.Database(str(db_path),
                       buffer_pool_size=int(config.KUZU_BUFFER_POOL_GB * 1024 ** 3))
    conn = kuzu.Connection(db)

    total = 0
    for label, key in LABELS.items():
        q = conn.execute(
            f"MATCH (n:{label}) RETURN n.{key} AS k, count(*) AS c")
        dups = []
        while q.has_next():
            k, c = q.get_next()
            if c and c > 1:
                dups.append((k, c))
        extra = sum(c - 1 for _, c in dups)
        print(f"{label}: 重复 id {len(dups)} 个（多余 {extra} 行）", flush=True)
        if not dups or dry:
            total += extra
            continue

        fixed = 0
        for k, c in dups:
            r = conn.execute(
                f"MATCH (n:{label}) WHERE n.{key} = $k RETURN n LIMIT 1", {"k": k})
            if not r.has_next():
                continue
            props = dict(r.get_next()[0])
            # 有边挂着：用 DETACH DELETE，否则 Kuzu 会拒绝删节点
            conn.execute(
                f"MATCH (n:{label}) WHERE n.{key} = $k DETACH DELETE n", {"k": k})
            cols = list(props.keys())
            sql = "CREATE (n:%s {%s})" % (
                label, ", ".join(f"{c0}: ${c0}" for c0 in cols))
            conn.execute(sql, props)
            fixed += 1
        print(f"  已重建 {fixed} 个节点（多余 {extra} 行已删）", flush=True)
        total += extra

    print(f"合计多余 {total} 行" + ("（干跑，未修改）" if dry else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
