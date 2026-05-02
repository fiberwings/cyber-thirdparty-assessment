"""PDF parsing with PyMuPDF.

We emit one chunk per (page, paragraph block), and we maintain a running
section heading by detecting lines whose font size is significantly larger
than the body font on the same page. That lets us cite "Section 3.1.2 –
Access Control" rather than just "page 7".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf  # PyMuPDF >= 1.24 ships as `pymupdf`

from app.parsing.chunker import ParsedChunk

MAX_CHARS_PER_CHUNK = 1500


@dataclass
class _Block:
    text: str
    avg_size: float
    is_heading: bool


def _block_lines(block: dict) -> list[tuple[str, float]]:
    """Flatten a block's lines into (text, avg_font_size)."""
    out: list[tuple[str, float]] = []
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        if not spans:
            continue
        text = "".join(s.get("text", "") for s in spans).strip()
        if not text:
            continue
        sizes = [float(s.get("size", 0)) for s in spans if s.get("size")]
        avg = sum(sizes) / len(sizes) if sizes else 0.0
        out.append((text, avg))
    return out


def _classify_blocks(page_dict: dict) -> tuple[list[_Block], float]:
    body_size_samples: list[float] = []
    raw: list[_Block] = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # text only
            continue
        lines = _block_lines(block)
        if not lines:
            continue
        text = "\n".join(t for t, _ in lines)
        avg = sum(s for _, s in lines) / len(lines)
        body_size_samples.append(avg)
        raw.append(_Block(text=text, avg_size=avg, is_heading=False))

    if not body_size_samples:
        return [], 0.0

    # Body font ~= median of all block sizes; headings are 1.18× body or larger
    sorted_sizes = sorted(body_size_samples)
    body = sorted_sizes[len(sorted_sizes) // 2]
    heading_threshold = body * 1.18

    for b in raw:
        if b.avg_size >= heading_threshold and len(b.text) < 200:
            b.is_heading = True
    return raw, body


def parse_pdf(path: Path) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    section_path = ""
    ordinal = 0

    with pymupdf.open(path) as doc:
        for page_no, page in enumerate(doc, start=1):
            page_dict = page.get_text("dict")
            blocks, _body = _classify_blocks(page_dict)
            buffer_text: list[str] = []
            buffer_size = 0

            def flush():
                nonlocal buffer_text, buffer_size, ordinal
                if buffer_text:
                    chunks.append(
                        ParsedChunk(
                            ord=ordinal,
                            text="\n\n".join(buffer_text).strip(),
                            page=page_no,
                            section_path=section_path,
                        )
                    )
                    ordinal += 1
                    buffer_text, buffer_size = [], 0

            for b in blocks:
                if b.is_heading:
                    flush()
                    section_path = b.text.strip()
                    continue
                if buffer_size + len(b.text) > MAX_CHARS_PER_CHUNK and buffer_text:
                    flush()
                buffer_text.append(b.text)
                buffer_size += len(b.text)
            flush()

    return chunks
