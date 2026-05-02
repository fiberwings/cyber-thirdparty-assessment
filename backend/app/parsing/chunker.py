"""Dispatch a file to the right parser and yield page/section-aware chunks.

A `ParsedChunk` carries enough metadata that, downstream, the AI can quote it
and the UI can render the citation as "Filename — page 7 — Section 3.1.2".
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedChunk:
    ord: int
    text: str
    page: int | None = None
    section_path: str = ""
    meta: dict = field(default_factory=dict)


def _detect_kind(filename: str, mime: str) -> str:
    name = filename.lower()
    if name.endswith((".pdf",)) or mime == "application/pdf":
        return "pdf"
    if name.endswith((".xlsx", ".xlsm", ".xltx")) or "spreadsheetml" in mime:
        return "xlsx"
    if name.endswith((".docx",)) or "wordprocessingml" in mime:
        return "docx"
    if name.endswith((".txt", ".md", ".csv")):
        return "text"
    return "unknown"


def parse_document(path: Path, filename: str, mime: str | None = None) -> list[ParsedChunk]:
    if mime is None:
        mime = mimetypes.guess_type(filename)[0] or ""
    kind = _detect_kind(filename, mime)
    if kind == "pdf":
        from app.parsing.pdf import parse_pdf
        return parse_pdf(path)
    if kind == "xlsx":
        from app.parsing.xlsx import parse_xlsx
        return parse_xlsx(path)
    if kind == "docx":
        from app.parsing.docx import parse_docx
        return parse_docx(path)
    if kind == "text":
        return _parse_text(path)
    return _parse_text(path, fallback=True)


def _parse_text(path: Path, fallback: bool = False) -> list[ParsedChunk]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        if not fallback:
            raise
        text = f"[unparseable: {e}]"
    chunks: list[ParsedChunk] = []
    for i, block in enumerate(_split_paragraphs(text)):
        if block.strip():
            chunks.append(ParsedChunk(ord=i, text=block.strip(), section_path=""))
    return chunks


def _split_paragraphs(text: str, max_chars: int = 1500) -> list[str]:
    paragraphs: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if size + len(para) > max_chars and buf:
            paragraphs.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para)
    if buf:
        paragraphs.append("\n\n".join(buf))
    return paragraphs
