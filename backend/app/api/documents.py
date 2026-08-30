from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.ai.agents import cross_correlation as corr_agent
from app.ai.agents import document_weaknesses as docw_agent
from app.api.deps import db_session, get_assessment
from app.api.serializers import serialize_document
from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Document
from app.parsing import parse_document
from app.schemas.api import ChunkRead, DocumentRead
from app.storage.files import signed_token, store_file, verify_token
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

router = APIRouter(prefix="/api", tags=["documents"])

_VALID_KINDS = {"questionnaire", "soc", "iso", "pentest", "policy", "other"}


@router.get("/assessments/{assessment_id}/documents", response_model=list[DocumentRead])
def list_documents(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    return [serialize_document(d) for d in a.documents]


@router.post(
    "/assessments/{assessment_id}/documents",
    response_model=DocumentRead,
    status_code=201,
)
async def upload_document(
    assessment_id: int,
    kind: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(db_session),
):
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind. Allowed: {_VALID_KINDS}")
    a = get_assessment(assessment_id, db)

    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB")

    with NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        tmp_path = Path(tmp.name)

    target_path, sha, size = store_file(tmp_path, file.filename or "upload.bin")
    tmp_path.unlink(missing_ok=True)

    doc = Document(
        assessment_id=a.id,
        kind=kind,
        filename=file.filename or "upload.bin",
        mime=file.content_type or "application/octet-stream",
        sha256=sha,
        size_bytes=size,
    )
    db.add(doc)
    db.flush()

    chunks = parse_document(target_path, doc.filename, doc.mime)
    for c in chunks:
        db.add(
            Chunk(
                document_id=doc.id,
                page=c.page,
                section_path=c.section_path,
                ord=c.ord,
                text=c.text,
                meta=c.meta,
            )
        )
    doc.parsed_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)

    # Auto-fire per-document weakness extraction. After extraction completes,
    # if every document in this assessment has weakness_extracted_at set, also
    # fire cross-correlation so emergent scenarios + score impact are reflected
    # without requiring an extra click. The user gets a single task id to poll.
    doc_id = doc.id
    assessment_id = a.id

    async def job(handle):
        await handle.update(progress=0.05, detail="Extracting weaknesses...")

        async def extract_progress(p: float, detail: str):
            # Reserve [0.0, 0.85] for extraction; [0.85, 1.0] for the optional
            # cross-correlation step.
            await handle.update(progress=min(p * 0.85, 0.85), detail=detail)

        with SessionLocal() as inner:
            await docw_agent.extract(
                inner, doc_id, on_progress=extract_progress
            )
            unprocessed = (
                inner.query(Document)
                .filter(
                    Document.assessment_id == assessment_id,
                    Document.weakness_extracted_at.is_(None),
                )
                .count()
            )

        if unprocessed == 0:
            await handle.update(progress=0.85, detail="All documents extracted; cross-correlating...")

            async def corr_progress(p: float, detail: str):
                await handle.update(
                    progress=0.85 + p * 0.15,
                    detail=f"Correlation: {detail}",
                )

            mark_phase_started(assessment_id, "cross_correlation", handle.id)
            try:
                with SessionLocal() as inner:
                    await corr_agent.run(
                        inner, assessment_id, on_progress=corr_progress
                    )
                mark_phase_done(assessment_id, "cross_correlation")
            except Exception as e:
                mark_phase_error(assessment_id, "cross_correlation", str(e))
                raise

    handle = registry.submit(job, kind="cross_correlation", assessment_id=assessment_id)
    # Attach the task id to the response so the frontend can poll
    # /api/tasks/{id} for extraction progress.
    doc.weakness_task_id = handle.id  # transient field on ORM instance
    return serialize_document(doc)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: int, db: Session = Depends(db_session)):
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404)
    db.delete(d)
    db.commit()


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkRead])
def list_chunks(
    document_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(db_session),
):
    qd = db.query(Chunk).filter(Chunk.document_id == document_id)
    if q:
        like = f"%{q}%"
        qd = qd.filter(Chunk.text.ilike(like))
    rows = qd.order_by(Chunk.ord).limit(limit).all()
    return [
        ChunkRead(
            id=c.id,
            document_id=c.document_id,
            page=c.page,
            section_path=c.section_path,
            text=c.text,
        )
        for c in rows
    ]


@router.get("/chunks/{chunk_id}", response_model=ChunkRead)
def get_chunk(chunk_id: int, db: Session = Depends(db_session)):
    c = db.get(Chunk, chunk_id)
    if c is None:
        raise HTTPException(status_code=404)
    return ChunkRead(
        id=c.id,
        document_id=c.document_id,
        page=c.page,
        section_path=c.section_path,
        text=c.text,
    )


@router.get("/documents/{document_id}/url")
def get_signed_url(document_id: int, db: Session = Depends(db_session)):
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404)
    token = signed_token(d.id)
    return {"url": f"/api/documents/{d.id}/file?t={token}", "filename": d.filename}


@router.get("/documents/{document_id}/file")
def download_document(
    document_id: int,
    t: str = Query(...),
    db: Session = Depends(db_session),
):
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404)
    if not verify_token(d.id, t):
        raise HTTPException(status_code=403, detail="Invalid or expired token")
    target = settings.storage_dir / d.sha256 / d.filename
    if not target.exists():
        raise HTTPException(status_code=410, detail="File missing on disk")
    return FileResponse(target, media_type=d.mime, filename=d.filename)
