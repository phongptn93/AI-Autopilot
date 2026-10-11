"""Turn a document a BA uploads (PRD, BRD, SRS, spec…) into plain text for a review.

Text is extracted HERE rather than handing the agent a file path, for two reasons:
the review must be reproducible from what is stored (a rerun weeks later uses the same
text, not whatever sits at that path), and the size cap and the "this is data, not
instructions" framing can only be enforced on text we hold.

``.docx`` is read straight from its XML (it is a zip) — no dependency. ``.pdf`` needs
``pypdf``, an optional extra (``pip install "ai-autopilot[docs]"``); without it the
upload is refused with that instruction instead of failing obscurely.
"""

from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

ALLOWED = (".md", ".markdown", ".txt", ".html", ".htm", ".docx", ".pdf")
MAX_BYTES = 15 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(ValueError):
    """An upload we cannot read — the message is shown to the user as is."""


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1258", errors="replace")   # legacy Vietnamese Windows files


class _HtmlText(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table",
              "section", "article", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html(text: str) -> str:
    p = _HtmlText()
    p.feed(text)
    return "".join(p.out)


def _docx(data: bytes) -> str:
    """Paragraphs in order, headings marked, table rows as ``a | b | c``."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentError("File .docx bị hỏng hoặc không phải Word.") from exc
    root = ElementTree.fromstring(xml)
    body = root.find(f"{_W}body")
    lines: list[str] = []

    def para(p) -> str:
        style = p.find(f"{_W}pPr/{_W}pStyle")
        text = "".join(t.text or "" for t in p.iter(f"{_W}t"))
        name = (style.get(f"{_W}val") if style is not None else "") or ""
        m = re.match(r"(?i)heading\s*(\d)", name)
        return ("#" * int(m.group(1)) + " " + text) if m and text.strip() else text

    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            lines.append(para(el))
        elif el.tag == f"{_W}tbl":
            for row in el.iter(f"{_W}tr"):
                cells = [" ".join(para(p) for p in c.iter(f"{_W}p")).strip()
                         for c in row.iter(f"{_W}tc")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError(
            "Máy này chưa cài bộ đọc PDF. Chạy: pip install \"ai-autopilot[docs]\" "
            "(hoặc pip install pypdf), hoặc lưu tài liệu thành .docx / .md rồi tải lại."
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as exc:  # noqa: BLE001 — any parser failure is "unreadable"
        raise DocumentError("Không đọc được PDF (có thể bị mã hoá hoặc chỉ là ảnh scan).") from exc
    return "\n\n".join(f"--- Trang {i} ---\n{t}" for i, t in enumerate(pages, 1) if t.strip())


def extract(filename: str, data: bytes) -> str:
    """Plain text of an uploaded document. Raises :class:`DocumentError` with a message
    a person can act on."""
    name = (filename or "").lower()
    ext = next((e for e in ALLOWED if name.endswith(e)), "")
    if not ext:
        raise DocumentError("Chỉ nhận " + ", ".join(ALLOWED) + ".")
    if not data:
        raise DocumentError("File rỗng.")
    if len(data) > MAX_BYTES:
        raise DocumentError(f"File quá lớn (tối đa {MAX_BYTES // (1024 * 1024)} MB).")
    if ext == ".docx":
        text = _docx(data)
    elif ext == ".pdf":
        text = _pdf(data)
    elif ext in (".html", ".htm"):
        text = _html(_decode(data))
    else:
        text = _decode(data)
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if len(text) < 20:
        raise DocumentError("Không trích được nội dung chữ từ file (file ảnh/scan?).")
    return text


def title_of(text: str, filename: str) -> str:
    """The document's first heading, else its first line, else the file name."""
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and len(s) <= 160:
            return s
    return filename
