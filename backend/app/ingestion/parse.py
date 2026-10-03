# -*- coding: utf-8 -*-
"""电子书文档解析：txt / md / pdf / epub → 纯文本

- TXT/MD：UTF-8 优先，GBK 回退
- PDF：pypdf 抽取文本层；无文本层（扫描件）抛 ParseError
- EPUB：标准库 zipfile + xml 按 spine 阅读顺序抽取 XHTML 正文（不引 lxml）
"""
from __future__ import annotations

import posixpath
import urllib.parse
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED = {".txt": "txt", ".md": "md", ".pdf": "pdf", ".epub": "epub"}


class ParseError(Exception):
    """解析失败（文件类型/损坏/无文本层）；消息可直接展示给用户。"""


# ---------------- TXT / MD ----------------
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 永不失败
        raise ParseError("无法识别文本编码")
    if not text.strip():
        raise ParseError("文件内容为空")
    return text


# ---------------- PDF ----------------
def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ParseError("PDF 解析依赖 pypdf 未安装") from e
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ParseError("PDF 文件损坏或无法打开") from e
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n".join(pages).strip()
    if not text:
        raise ParseError("未抽取到文本，疑似扫描版 PDF（请先 OCR 后再上传）")
    return text


# ---------------- EPUB ----------------
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "section", "article", "header"}
_SKIP_TAGS = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    """XHTML → 纯文本；块级标签换行，script/style 内容丢弃。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        import re
        out = "".join(self.parts)
        out = re.sub(r"[ \t　]+", " ", out)
        out = re.sub(r"\n{2,}", "\n\n", out)
        return out.strip()


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _read_epub(path: Path) -> str:
    try:
        zf_ctx = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, OSError) as e:
        raise ParseError("EPUB 文件损坏或无法打开") from e

    with zf_ctx:
        names = set(zf_ctx.namelist())
        try:
            container = zf_ctx.read("META-INF/container.xml")
            root = ET.fromstring(container)
            opf_path = None
            for el in root.iter():
                if _strip_ns(el.tag) == "rootfile":
                    opf_path = el.attrib.get("full-path")
                    break
            if not opf_path:
                raise ParseError("EPUB 缺少 OPF 清单")
            opf_dir = str(Path(opf_path).parent)
            opf = ET.fromstring(zf_ctx.read(opf_path))
        except (KeyError, ET.ParseError) as e:
            raise ParseError("EPUB 结构不完整（container/opf 缺失）") from e

        manifest: dict[str, str] = {}
        spine: list[str] = []
        for el in opf.iter():
            tag = _strip_ns(el.tag)
            if tag == "item":
                href = el.attrib.get("href")
                iid = el.attrib.get("id")
                if href and iid:
                    manifest[iid] = href
            elif tag == "itemref":
                ref = el.attrib.get("idref")
                if ref:
                    spine.append(ref)

        if not spine:
            raise ParseError("EPUB 缺少 spine 阅读顺序")

        sections: list[str] = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            href = urllib.parse.unquote(href)  # 部分 EPUB href 做了百分号编码
            item_path = (f"{opf_dir}/{href}" if opf_dir not in ("", ".") else href)
            item_path = posixpath.normpath(item_path)  # 保持 ZIP 内部的正斜杠（不能用 pathlib，Windows 会变成反斜杠）
            if item_path not in names:
                # 部分 EPUB opf 在根目录时 Path 不产生多余前缀；再尝试 href 本身
                item_path = href
                if item_path not in names:
                    continue
            try:
                raw = zf_ctx.read(item_path).decode("utf-8", errors="ignore")
            except KeyError:
                continue
            parser = _TextExtractor()
            try:
                parser.feed(raw)
            except Exception:
                continue
            t = parser.text()
            if t:
                sections.append(t)

    text = "\n\n".join(sections).strip()
    if not text:
        raise ParseError("EPUB 正文为空")
    return text


# ---------------- 统一入口 ----------------
def parse_document(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ParseError(f"文件不存在：{p.name}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise ParseError(f"不支持的文件类型 {ext or '（无扩展名）'}；支持：PDF / TXT / EPUB / MD")
    if ext in (".txt", ".md"):
        text = _read_text(p)
    elif ext == ".pdf":
        text = _read_pdf(p)
    else:
        text = _read_epub(p)
    return {"text": text, "kind": SUPPORTED[ext]}
