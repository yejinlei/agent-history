# -*- coding: utf-8 -*-
"""离线自测：电子书解析（T2）。运行：python scripts/selftest_ingest.py
后续任务向本脚本追加 RAG / 抽取用例。
"""
from __future__ import annotations

import importlib.util
import tempfile
import zipfile
from pathlib import Path

# 直接按路径加载 parse 模块，避免 app.ingestion 包 __init__ 连锁加载 store（与运行中的后端争 Kuzu 锁）
def _load(name: str):
    p = Path(__file__).resolve().parents[1] / "app" / "ingestion" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_mod", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

parse_mod = _load("parse")
parse_document = parse_mod.parse_document
ParseError = parse_mod.ParseError
rag_mod = _load("rag")

PDF_OK = "ZQLINZIPARSEOK777"
EPUB_OK = "ZQLINZIEPUBOK777"

_CONTAINER = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<manifest><item id="x1" href="content.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="x1"/></spine>
</package>"""

_XHTML = ("""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><p>%s</p><p>second section</p></body></html>"""
          % EPUB_OK).encode("utf-8")

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def expect_error(name: str, path: Path) -> None:
    try:
        parse_document(path)
        check(name, False, "did not raise")
    except ParseError:
        check(name, True)


def test_rag() -> None:
    filler = ("春耕夏耘秋收冬藏，乡里编户齐民，置仓廪府库，习射御读法。"
              "山川道里桥梁津渡，车马辇挽，往来如织。")
    special = "武帝通漕运，置常平仓以抑物价，岁在戊己。漕运常平仓戊己。"
    text_a = filler * 22 + special + filler * 8
    text_b = filler * 30
    docs = [("bookA", 0, text_a), ("bookB", 0, text_b)]

    # 切块
    pieces = rag_mod.chunk_text(text_a, size=800, overlap=100)
    check("chunk-count", len(pieces) >= 2 and all(len(p) <= 820 for p in pieces),
          f"{len(pieces)} pieces")

    # BM25 top1 属于含特征词的 bookA
    hits = rag_mod.BM25(docs).search("漕运 常平仓 戊己", topk=3)
    check("bm25-top1", bool(hits) and hits[0]["book_id"] == "bookA",
          str(hits[0]["book_id"]) if hits else "no hit")

    # 过滤：只允许 bookB → bookA 命中被排除
    hits_f = rag_mod.BM25(docs).search("漕运", book_ids={"bookB"})
    check("bm25-filter", all(h["book_id"] == "bookB" for h in hits_f))

    # 分词：拉丁整词 + 中文二元组
    toks = rag_mod.tokenize("漕运 hello123")
    check("tokenize", "hello123" in toks and "漕运" in toks)

    # 向量检索（确定性手工向量）
    vdocs = [("bookA", 0, special, [1.0, 0.0]),
             ("bookB", 0, text_b[:10], [0.0, 1.0])]
    vh = rag_mod.vector_search([0.9, 0.1], vdocs)
    check("vector-order", bool(vh) and vh[0]["book_id"] == "bookA")

    # 统一入口：假 embed_fn + get_embedding → vector 模式
    def fake_embed(texts):
        return [[1.0, 0.0]] * len(texts)
    get_emb = lambda bid, idx: {"bookA": [1.0, 0.0], "bookB": [0.0, 1.0]}[bid]
    uh, mode = rag_mod.retrieve_chunks("漕运常平仓", docs, fake_embed, get_emb)
    check("retrieve-vector", mode == "vector" and uh[0]["book_id"] == "bookA", mode)

    # embed_fn 抛异常 → BM25 回退
    def bad_embed(texts):
        raise RuntimeError("no api")
    uh, mode = rag_mod.retrieve_chunks("漕运常平仓戊己", docs, bad_embed, get_emb)
    check("retrieve-fallback", mode == "bm25" and uh and uh[0]["book_id"] == "bookA", mode)

    # 不传 embed_fn → 纯 BM25
    uh, mode = rag_mod.retrieve_chunks("常平仓戊己", docs)
    check("retrieve-bm25-only", mode == "bm25" and uh[0]["book_id"] == "bookA")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdir = Path(td)

        # --- TXT UTF-8 / GBK ---
        p_utf8 = tdir / "u.txt"
        p_utf8.write_text("正文段落。" * 400 + PDF_OK, encoding="utf-8")
        r = parse_document(p_utf8)
        check("txt-utf8", r["kind"] == "txt" and PDF_OK in r["text"])

        p_gbk = tdir / "g.txt"
        p_gbk.write_bytes("编码测试段落。齐取临淄。".encode("gb18030") * 50)
        r = parse_document(p_gbk)
        check("txt-gbk", "齐取临淄" in r["text"])

        # --- MD ---
        p_md = tdir / "m.md"
        p_md.write_text("# 标题\n\n正文 " + PDF_OK, encoding="utf-8")
        r = parse_document(p_md)
        check("md", r["kind"] == "md" and PDF_OK in r["text"])

        # --- PDF：用 pypdf 构造含文本层的有效 PDF ---
        from pypdf import PdfWriter
        from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)
        writer = PdfWriter()
        page = writer.add_blank_page(width=320, height=200)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica")})
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        raw = f"BT /F1 14 Tf 24 160 Td ({PDF_OK}) Tj ET".encode("latin-1")
        content = DecodedStreamObject()
        content.set_data(raw)
        page[NameObject("/Contents")] = writer._add_object(content)
        p_pdf = tdir / "ok.pdf"
        with open(p_pdf, "wb") as f:
            writer.write(f)
        r = parse_document(p_pdf)
        check("pdf-text-layer", r["kind"] == "pdf" and PDF_OK in r["text"].replace(" ", ""))

        # --- EPUB 最小构造 ---
        p_epub = tdir / "ok.epub"
        with zipfile.ZipFile(p_epub, "w") as zf:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, "application/epub+zip")
            zf.writestr("META-INF/container.xml", _CONTAINER, zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/content.opf", _OPF, zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/content.xhtml", _XHTML, zipfile.ZIP_DEFLATED)
        r = parse_document(p_epub)
        check("epub-spine", r["kind"] == "epub" and EPUB_OK in r["text"]
              and "second section" in r["text"])

        # --- 错误路径 ---
        p_fake_pdf = tdir / "fake.pdf"
        p_fake_pdf.write_text("not a real pdf", encoding="utf-8")
        expect_error("pdf-corrupted", p_fake_pdf)

        p_bad_epub = tdir / "bad.epub"
        with zipfile.ZipFile(p_bad_epub, "w") as zf:
            zf.writestr("hello.txt", "no container")
        expect_error("epub-corrupted", p_bad_epub)

        p_exe = tdir / "x.exe"
        p_exe.write_bytes(b"MZbinary")
        expect_error("unsupported-type", p_exe)

        p_empty = tdir / "empty.txt"
        p_empty.write_text("   \n ", encoding="utf-8")
        expect_error("empty-text", p_empty)

    test_rag()
    print(f"\n{'ALL PASS' if not _failures else str(len(_failures)) + ' FAILURES'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
