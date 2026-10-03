# -*- coding: utf-8 -*-
"""本地加书 CLI —— 只走 HTTP API（严禁直接打开 Kuzu：单写者锁）。

用法：
  python scripts/ingest_book.py <文件路径> [--title 书名] [--mount]

- 上传 PDF/TXT/EPUB/MD（≤50MB）：后端解析 → 切块 → 启发式/LLM 抽取 → RAG；
- --mount：全部接受挂载提案并提交（等价于界面上逐项勾选后确认）。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import uuid
from pathlib import Path
from urllib import request as urlreq

BASE = "http://127.0.0.1:8000"
SUPPORTED = {".pdf", ".txt", ".epub", ".md"}
MAX_BYTES = 50 * 1024 * 1024


def _post_json(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urlreq.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urlreq.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def upload(path: Path, title: str | None) -> dict:
    boundary = "----cli" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []
    fields: list[tuple[str, str]] = []
    if title:
        fields.append(("title", title))
    for k, v in fields:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        parts.append(f"{v}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        ('Content-Disposition: form-data; name="file"; '
         f'filename="{path.name}"\r\n').encode("utf-8"))
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urlreq.Request(
        f"{BASE}/api/library/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urlreq.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))
    except urlreq.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"上传失败 HTTP {e.code}：{detail}")


def mount_all(book_id: str) -> dict:
    started = _post_json(f"{BASE}/api/mounts/start", {"book_id": book_id})
    accepted = [True] * len(started["proposals"])
    return _post_json(f"{BASE}/api/mounts/commit",
                      {"thread_id": started["thread_id"], "accepted": accepted})


def main() -> None:
    ap = argparse.ArgumentParser(description="经 HTTP 上传并（可选）挂载一本书")
    ap.add_argument("file", help="PDF/TXT/EPUB/MD 文件路径")
    ap.add_argument("--title", help="书名（默认取文件名）")
    ap.add_argument("--mount", action="store_true",
                    help="全部接受提案并直接挂载")
    args = ap.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")
    if path.suffix.lower() not in SUPPORTED:
        sys.exit(f"不支持的类型：{path.suffix}（支持 {sorted(SUPPORTED)}）")
    if path.stat().st_size > MAX_BYTES:
        sys.exit("文件超过 50MB 限制")

    r = upload(path, args.title)
    book = r["book"]
    print(f"✓ 已入库：《{book['title']}》 id={book['id']}")
    print(f"  切块 {r['chunks']} · 提炼提案 {r['proposals']} · 模式 {r['extraction_mode']}")

    if args.mount:
        res = mount_all(book["id"])
        print(f"✓ 已挂载：{res['result']}（stage={res['stage']}）")
    else:
        print("  未挂载；如需挂载加 --mount，或在界面「挂载书库」中逐项确认。")


if __name__ == "__main__":
    main()
