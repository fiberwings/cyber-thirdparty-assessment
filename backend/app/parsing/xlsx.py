"""XLSX parsing with openpyxl.

We treat each sheet as a section. For every row, we emit a chunk that
preserves the column headers so the AI can cite "Sheet 'CAIQ' — row 42 —
question/answer pair". This is the standard form for vendor questionnaires
(SIG, CAIQ, custom).
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from app.parsing.chunker import ParsedChunk

MAX_CHARS_PER_CHUNK = 1500
MAX_ROWS_PER_CHUNK = 6


def _normalize(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return s


def _row_to_text(headers: list[str], row: tuple) -> str:
    parts = []
    for h, v in zip(headers, row):
        text = _normalize(v)
        if not text:
            continue
        if h:
            parts.append(f"{h}: {text}")
        else:
            parts.append(text)
    return " | ".join(parts)


def parse_xlsx(path: Path) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    ordinal = 0
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet in wb.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            # Find the header row: the first row with ≥2 non-empty cells.
            header_idx = 0
            for i, r in enumerate(rows):
                non_empty = sum(1 for c in r if _normalize(c))
                if non_empty >= 2:
                    header_idx = i
                    break
            headers = [_normalize(c) for c in rows[header_idx]]
            data_rows = rows[header_idx + 1 :]

            buffer: list[str] = []
            buffer_size = 0
            buffer_rows = 0
            first_row_no = header_idx + 2  # 1-indexed for citation

            def flush(end_row_no: int):
                nonlocal buffer, buffer_size, buffer_rows, ordinal, first_row_no
                if buffer:
                    chunks.append(
                        ParsedChunk(
                            ord=ordinal,
                            text="\n".join(buffer).strip(),
                            page=None,
                            section_path=f"Sheet '{sheet.title}' (rows {first_row_no}-{end_row_no})",
                            meta={"sheet": sheet.title},
                        )
                    )
                    ordinal += 1
                    buffer, buffer_size, buffer_rows = [], 0, 0

            for offset, r in enumerate(data_rows):
                row_no = header_idx + 2 + offset
                if not any(_normalize(c) for c in r):
                    continue
                line = _row_to_text(headers, r)
                if not line:
                    continue
                if buffer and (
                    buffer_size + len(line) > MAX_CHARS_PER_CHUNK
                    or buffer_rows >= MAX_ROWS_PER_CHUNK
                ):
                    flush(row_no - 1)
                    first_row_no = row_no
                buffer.append(line)
                buffer_size += len(line)
                buffer_rows += 1
            flush(header_idx + 1 + len(data_rows))
    finally:
        wb.close()

    return chunks
