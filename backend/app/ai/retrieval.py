"""FTS5-backed candidate-evidence retrieval for the gap analyzer."""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Chunk

_SAFE = re.compile(r"[A-Za-z0-9]+")


def _to_fts_query(*terms: str) -> str:
    """Build a tolerant OR query: tokens are joined with OR, special chars stripped."""
    tokens: list[str] = []
    for t in terms:
        for tok in _SAFE.findall(t):
            tok = tok.strip()
            if len(tok) >= 2 and tok.lower() not in {"and", "the", "for", "with"}:
                tokens.append(tok)
    if not tokens:
        return ""
    # Quote each token to avoid FTS5 parsing collisions.
    return " OR ".join(f'"{t}"' for t in tokens)


def search(
    db: Session,
    assessment_id: int,
    *terms: str,
    top_k: int | None = None,
) -> list[Chunk]:
    """Return candidate chunks for an assessment's documents, ranked by FTS bm25."""
    query = _to_fts_query(*terms)
    if not query:
        return []
    top_k = top_k or settings.fts_topk

    sql = text(
        """
        SELECT chunk.id, bm25(chunk_fts) AS rank
        FROM chunk_fts
        JOIN chunk ON chunk.id = chunk_fts.rowid
        JOIN document ON document.id = chunk.document_id
        WHERE chunk_fts MATCH :q
          AND document.assessment_id = :aid
        ORDER BY rank
        LIMIT :k
        """
    )
    rows = db.execute(sql, {"q": query, "aid": assessment_id, "k": top_k}).all()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    chunks = db.query(Chunk).filter(Chunk.id.in_(ids)).all()
    by_id = {c.id: c for c in chunks}
    return [by_id[i] for i in ids if i in by_id]
