from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.ai.agents import attestation as attestation_agent
from app.ai.agents import document_weaknesses as docw_agent
from app.api.deps import db_session, get_assessment
from app.api.serializers import serialize_document
from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Document
from app import workflow
from app.parsing import parse_document
from app.schemas.api import ChunkRead, DocumentRead, TaskStatusRead
from app.storage.files import signed_token, store_file, verify_token
from app.tasks import TaskHandle, registry

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
    # Evidence follows scenarios; uploads may proceed while another document
    # is still extracting (same step) but not while any other job runs.
    workflow.require_step_ready(a, "evidence")
    workflow.require_no_run_in_flight(a, allow_kinds=(workflow.KIND_EXTRACTION,))

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
    # A new document changes the evidence bundle: correlation and everything
    # after it must be re-run before the assessment can progress.
    workflow.invalidate_downstream(a, "correlation", reason=f"Document uploaded: {doc.filename}")
    db.commit()
    db.refresh(doc)

    # Auto-fire per-document weakness extraction. The task id is persisted on
    # the row so the evidence phase and the document list can report it.
    _submit_extraction(db, doc)
    return serialize_document(doc)


def _set_extraction_error(doc_id: int, message: str) -> None:
    with SessionLocal() as inner:
        d = inner.get(Document, doc_id)
        if d is None:
            return
        d.weakness_error = message[:500]
        inner.commit()


def _submit_extraction(db: Session, doc: Document) -> TaskHandle:
    """Start per-document weakness extraction (+ best-effort attestation
    profile) and persist the task id on the document. Extraction only:
    confirmation against the whole bundle, merge and cross-correlation run in
    the explicit cross-correlate step (R3) — doing them per upload would judge
    candidates against a partial bundle."""
    doc_id, assessment_id = doc.id, doc.assessment_id

    async def job(handle):
        # The extraction shape (single call / windowed / two-phase) is decided
        # inside the agent, which reports its own stages and unit counts via
        # app.activity; the job only forwards the detail text.
        handle.stage("Extracting findings", detail="Extracting weaknesses...")

        async def extract_progress(p: float, detail: str):
            await handle.update(detail=detail)

        try:
            with SessionLocal() as inner:
                await docw_agent.extract(inner, doc_id, on_progress=extract_progress)
                # Typed attestation profile for assurance documents (fast, cheap).
                # Best-effort: a failed profile never fails the extraction job —
                # the document simply has no profile (deterministic checks stay
                # silent) and the profile can be re-extracted via the API.
                profile_warning = ""
                handle.stage("Attestation profile")
                try:
                    await attestation_agent.extract_profile(inner, doc_id)
                except Exception as e:
                    profile_warning = f" — attestation profile failed (re-run available): {str(e)[:150]}"
                    attestation_agent.record_profile_error(inner, doc_id, str(e))
        except Exception as e:
            # Surface on the row: the evidence phase turns "error" with this
            # document listed and the UI offers a retry. Never leave a silent
            # NULL weakness_extracted_at behind.
            _set_extraction_error(doc_id, f"Extraction failed: {str(e)[:400]}")
            raise
        await handle.update(progress=1.0, detail="Extracted candidates" + profile_warning)

    handle = registry.submit(job, kind=workflow.KIND_EXTRACTION, assessment_id=assessment_id)
    doc.weakness_task_id = handle.id
    doc.weakness_error = None
    db.commit()
    return handle


def _get_document_with_assessment(db: Session, document_id: int) -> Document:
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return d


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: int, db: Session = Depends(db_session)):
    d = _get_document_with_assessment(db, document_id)
    a = d.assessment
    workflow.require_no_run_in_flight(a)
    workflow.invalidate_downstream(a, "correlation", reason=f"Document deleted: {d.filename}")
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


@router.post("/documents/{document_id}/attestation-profile", response_model=DocumentRead)
async def rerun_attestation_profile(document_id: int, db: Session = Depends(db_session)):
    """(Re)extract the typed attestation profile for one assurance document
    (synchronous — one fast-profile call)."""
    d = _get_document_with_assessment(db, document_id)
    if d.kind not in attestation_agent.ATTESTATION_KINDS:
        raise HTTPException(status_code=400, detail="Not an assurance document (soc/iso/pentest)")
    a = d.assessment
    workflow.require_no_run_in_flight(a)
    # The profile feeds the deterministic attestation checks in correlation.
    workflow.invalidate_downstream(
        a, "correlation", reason=f"Attestation profile re-extracted: {d.filename}"
    )
    db.commit()
    try:
        await attestation_agent.extract_profile(db, document_id)
    except Exception as e:
        attestation_agent.record_profile_error(db, document_id, str(e))
        raise HTTPException(
            status_code=502,
            detail=f"Attestation profile extraction failed: {str(e)[:300]}",
        )
    db.refresh(d)
    return serialize_document(d)


@router.post("/documents/{document_id}/extract-weaknesses", response_model=TaskStatusRead)
async def extract_weaknesses(document_id: int, db: Session = Depends(db_session)):
    """(Re)run weakness extraction for one document — the retry path for a
    failed or interrupted extraction. Re-attaches when this document's own
    extraction is still running."""
    d = _get_document_with_assessment(db, document_id)
    a = d.assessment
    if d.weakness_task_id:
        live = registry.get(d.weakness_task_id)
        if live is not None and live.status in ("pending", "running"):
            return workflow.reattach_response(live)
    workflow.require_no_run_in_flight(a, allow_kinds=(workflow.KIND_EXTRACTION,))
    workflow.invalidate_downstream(
        a, "correlation", reason=f"Weaknesses re-extracted: {d.filename}"
    )
    db.commit()
    handle = _submit_extraction(db, d)
    return workflow.reattach_response(handle)
