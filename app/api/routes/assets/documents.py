"""
M3: Company Documents Routes
Owner: Backend Dev 2
Dependencies: M1, M2

CRUD for company documents — USP, Compliances, Policies, Marketing Goals, etc.
Used by Study Coordinator (My Company) and Ethics Manager (Document Updation).
"""

import asyncio
import logging
import mimetypes
import os
import traceback
import uuid as uuid_mod

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, get_current_user, require_roles
from app.db.database import async_session_factory, get_db
from app.models.models import CompanyDocument, User, UserRole
from app.schemas.schemas import (
    ConfirmUploadRequest,
    DocumentCreate,
    DocumentOut,
    DocumentUpdate,
    PresignRequest,
    PresignResponse,
)
from app.services.storage import file_storage, get_s3_client, sanitize_filename
from app.services.storage.extractor import extract_bytes

logger = logging.getLogger(__name__)


def _safe_filename(original: str | None) -> str:
    """Deprecated alias — see app.services.storage.storage.sanitize_filename."""
    return sanitize_filename(original)


async def _background_train(company_id: str) -> None:
    """Re-train curator + reviewer skills after a company doc is uploaded."""
    async with async_session_factory() as db:
        try:
            from app.services.training.trainer import TrainingService

            await TrainingService(db).train_company_skills(company_id)
        except Exception as exc:
            logger.error("Background training failed for company %s: %s", company_id, exc)


router = APIRouter(prefix="/documents", tags=["Company Documents"])

ALLOWED_DOC_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
}
ALLOWED_DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md"}


def _is_allowed_doc(file: UploadFile) -> bool:
    """Accept by content-type (ignoring charset param) OR by file extension."""
    base_ct = (file.content_type or "").split(";")[0].strip()
    ext = os.path.splitext(file.filename or "")[1].lower()
    return base_ct in ALLOWED_DOC_TYPES or ext in ALLOWED_DOC_EXTENSIONS


async def _user_from_query_token(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Auth dependency for the file-serve route, which is called directly by the
    browser (iframe src / anchor href) and cannot send an Authorization header.
    Validates the JWT from ?token= instead.

    When storage is migrated to Azure Blob Storage, serve_document_file will
    simply redirect to the blob SAS URL and this dependency can be removed.
    """
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


@router.get("/", response_model=list[DocumentOut])
async def list_documents(
    doc_type: str = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(CompanyDocument).where(CompanyDocument.company_id == user.company_id)
    if doc_type:
        query = query.where(CompanyDocument.doc_type == doc_type)
    result = await db.execute(query.order_by(CompanyDocument.priority.desc()))
    return result.scalars().all()


@router.post("/", response_model=DocumentOut)
async def create_document(
    body: DocumentCreate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    doc = CompanyDocument(
        company_id=user.company_id,
        doc_type=body.doc_type,
        title=body.title,
        content=body.content,
    )
    db.add(doc)
    await db.flush()
    return doc


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    doc_type: str = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a document with a file attachment.
    Accepts PDF, DOCX, DOC, TXT, MD.
    Text is extracted immediately and stored in the content field so the
    Curator + Reviewer skills receive actual document text.
    Re-trains AI skills in the background after saving.
    """
    if not _is_allowed_doc(file):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: PDF, DOCX, DOC, TXT, MD.",
        )

    safe_name = _safe_filename(file.filename)
    try:
        file_path = file_storage.save(
            file=file,
            subfolder=f"docs/{user.company_id}",
            filename=safe_name,
        )
    except Exception as exc:
        logger.error(
            "Document upload: file_storage.save failed (company=%s, filename=%s): %s\n%s",
            user.company_id,
            file.filename,
            exc,
            traceback.format_exc(),
        )
        raise HTTPException(
            status_code=500,
            detail=f"Could not save file to storage: {exc}",
        )

    try:
        content = await asyncio.to_thread(extract_bytes, file_storage.open_bytes(file_path), file_path)
    except Exception as exc:
        logger.warning(
            "Document upload: extract_text failed for %s: %s — saving with empty content",
            file_path,
            exc,
        )
        content = None

    doc = CompanyDocument(
        company_id=user.company_id,
        doc_type=doc_type,
        title=title,
        content=content,
        file_path=file_path,
    )
    db.add(doc)
    await db.flush()

    background_tasks.add_task(_background_train, user.company_id)
    return doc


@router.get("/{doc_id}/file")
async def serve_document_file(
    doc_id: str,
    user: User = Depends(_user_from_query_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Stream the raw file back to the browser.
    Auth is via ?token= query param so iframe/anchor can load it directly.
    Bytes come from the active storage backend (local/S3/Azure).
    """
    result = await db.execute(
        select(CompanyDocument).where(
            CompanyDocument.id == doc_id,
            CompanyDocument.company_id == user.company_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.file_path:
        raise HTTPException(status_code=404, detail="No file attached to this document")

    data = file_storage.open_bytes(doc.file_path)
    if data is None:
        raise HTTPException(status_code=404, detail="File not found on disk")

    filename = doc.file_path.rstrip("/").split("/")[-1]
    media_type, _ = mimetypes.guess_type(filename)
    media_type = media_type or "application/octet-stream"

    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.patch("/{doc_id}", response_model=DocumentOut)
async def update_document(
    doc_id: str,
    body: DocumentUpdate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CompanyDocument).where(
            CompanyDocument.id == doc_id,
            CompanyDocument.company_id == user.company_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    doc.version += 1
    return doc


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CompanyDocument).where(
            CompanyDocument.id == doc_id,
            CompanyDocument.company_id == user.company_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await db.delete(doc)
    return {"detail": "Document deleted"}


# ─── S3 Pre-signed Upload (WAF-safe company-document upload) ──────────────────
# Mirrors the advertisement-document flow: browser PUTs directly to S3 so the
# multipart body never traverses CloudFront WAF, which blocks our binary PDF/
# DOCX uploads via the Core Rule Set (size + XSS body rules).
#
# Flow:  frontend → POST /documents/presign  (small JSON, WAF-safe)
#        frontend → PUT  <s3_url>            (direct to S3, WAF bypassed)
#        frontend → POST /documents/confirm  (backend fetches from S3, saves)

_MAX_COMPANY_DOC_BYTES = 50 * 1024 * 1024  # 50 MB


def _effective_content_type(req_ct: str, filename: str) -> str:
    """If the browser sent application/octet-stream, infer the real MIME from extension."""
    if req_ct != "application/octet-stream":
        return req_ct
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "md": "text/markdown",
    }.get(ext, req_ct)


@router.post("/presign", response_model=PresignResponse)
async def get_company_document_presign_url(
    req: PresignRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.ETHICS_MANAGER])),
):
    """
    Return a pre-signed S3 PUT URL so the browser can upload directly to S3,
    bypassing CloudFront WAF body inspection.

    Returns method="direct" when S3_UPLOAD_BUCKET is unset so localhost dev
    keeps working via the legacy multipart /documents/upload endpoint.
    """
    from app.core.config import settings as _s

    if req.file_size > _MAX_COMPANY_DOC_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large (max {_MAX_COMPANY_DOC_BYTES // (1024 * 1024)} MB)",
        )

    effective_ct = _effective_content_type(req.content_type, req.filename)
    if effective_ct not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: PDF, DOCX, DOC, TXT, MD.",
        )

    # Active storage backend first (S3/Azure presigned PUT straight to final location).
    try:
        presigned = file_storage.presign_upload(
            subfolder=f"docs/{user.company_id}", filename=req.filename, content_type=effective_ct
        )
    except Exception as e:
        logger.error("Failed to generate company-doc pre-signed URL: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail="Could not generate upload URL")
    if presigned:
        return PresignResponse(
            method="presigned", upload_url=presigned.upload_url, s3_key=presigned.key, content_type=effective_ct
        )

    # Legacy staging: local backend + S3 transfer bucket (WAF bypass only).
    if not _s.S3_UPLOAD_BUCKET:
        return PresignResponse(method="direct")

    safe_name = _safe_filename(req.filename)
    s3_key = f"{_s.S3_UPLOAD_PREFIX}/company/{user.company_id}/{uuid_mod.uuid4()}_{safe_name}"
    try:
        client = get_s3_client()
        upload_url = await asyncio.to_thread(
            client.generate_presigned_url,
            "put_object",
            Params={
                "Bucket": _s.S3_UPLOAD_BUCKET,
                "Key": s3_key,
                "ContentType": effective_ct,
            },
            ExpiresIn=3600,
        )
    except Exception as e:
        logger.error("Failed to generate company-doc S3 pre-signed URL: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail="Could not generate upload URL")

    return PresignResponse(method="presigned", upload_url=upload_url, s3_key=s3_key, content_type=effective_ct)


@router.post("/confirm", response_model=DocumentOut)
async def confirm_company_document_upload(
    req: ConfirmUploadRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Called after the browser finishes PUTting the file.
    Final-location keys ("/uploads/...", from backend presigning) skip the
    copy: bytes are already in place. Legacy staging keys download from S3,
    save via the backend, then delete the staging object.
    """
    from app.core.config import settings as _s

    if req.s3_key.startswith("/uploads/"):
        # Tenant scope — presigned keys embed docs/{company_id}.
        if f"docs/{user.company_id}" not in req.s3_key:
            raise HTTPException(status_code=400, detail="Invalid upload key")
        file_path = req.s3_key
    else:
        if not _s.S3_UPLOAD_BUCKET:
            raise HTTPException(status_code=400, detail="S3 upload not configured on this server")

        expected_prefix = f"{_s.S3_UPLOAD_PREFIX}/company/{user.company_id}/"
        if not req.s3_key.startswith(expected_prefix):
            raise HTTPException(status_code=400, detail="Invalid upload key")

        try:
            client = get_s3_client()
            s3_obj = await asyncio.to_thread(client.get_object, Bucket=_s.S3_UPLOAD_BUCKET, Key=req.s3_key)
            file_bytes = await asyncio.to_thread(lambda: s3_obj["Body"].read())
        except Exception as e:
            logger.error("Failed to download company doc from S3 key %s: %s\n%s", req.s3_key, e, traceback.format_exc())
            raise HTTPException(status_code=500, detail="Could not retrieve uploaded file")

        safe_name = _safe_filename(req.filename)
        try:
            file_path = file_storage.save_bytes(
                data=file_bytes,
                subfolder=f"docs/{user.company_id}",
                filename=safe_name,
            )
        except Exception as e:
            logger.error(
                "Failed to persist company doc (company=%s): %s\n%s", user.company_id, e, traceback.format_exc()
            )
            raise HTTPException(status_code=500, detail="Could not save file to storage")

        # Best-effort cleanup of the staging S3 object
        try:
            await asyncio.to_thread(client.delete_object, Bucket=_s.S3_UPLOAD_BUCKET, Key=req.s3_key)
        except Exception:
            logger.warning("Could not delete staging S3 object %s", req.s3_key)

    try:
        content = await asyncio.to_thread(extract_bytes, file_storage.open_bytes(file_path), file_path)
    except Exception as exc:
        logger.warning("confirm: extract_text failed for %s: %s", file_path, exc)
        content = None

    doc = CompanyDocument(
        company_id=user.company_id,
        doc_type=req.doc_type,
        title=req.title,
        content=content,
        file_path=file_path,
    )
    db.add(doc)
    await db.flush()

    background_tasks.add_task(_background_train, user.company_id)
    return doc
