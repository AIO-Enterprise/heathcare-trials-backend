"""
Protocol document upload endpoints (direct, chunked, and S3 pre-signed).

CloudFront WAF blocks request bodies > 8 KB (SizeRestrictions_BODY rule).
Three upload strategies are supported:
  1. Direct multipart (small files / internal use)
  2. Chunked JSON (base64 chunks, each under WAF limit)
  3. S3 pre-signed PUT + confirm (large files, bypasses WAF entirely)

Chunked-upload sessions are stored as temp files:
  <SESSIONS_DIR>/<upload_id>/meta.json
  <SESSIONS_DIR>/<upload_id>/chunk_N.bin
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid as uuid_mod
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, require_roles
from app.db.database import get_db
from app.models.models import Advertisement, AdvertisementDocument, User, UserRole
from app.schemas.schemas import (
    AdvertisementDocumentOut,
    ChunkUploadRequest,
    ConfirmUploadRequest,
    FinalizeUploadRequest,
    PresignRequest,
    PresignResponse,
    StartUploadRequest,
)
from app.services.storage import file_storage
from app.services.storage.extractor import BACKEND_ROOT, extract_text, url_to_disk_path

router = APIRouter(prefix="/advertisements", tags=["Advertisement Documents"])
logger = logging.getLogger(__name__)

# ── Chunked-upload session storage ───────────────────────────────────────────
_SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "ad_upload_sessions")
os.makedirs(_SESSIONS_DIR, exist_ok=True)

ALLOWED_PROTOCOL_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
}

_EXT_MAP = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB hard cap


def _session_dir(upload_id: str) -> str:
    return os.path.join(_SESSIONS_DIR, upload_id)


def _load_session_meta(upload_id: str) -> dict[str, Any] | None:
    meta_path = os.path.join(_session_dir(upload_id), "meta.json")
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _delete_session(upload_id: str) -> None:
    try:
        shutil.rmtree(_session_dir(upload_id), ignore_errors=True)
    except Exception:
        logger.warning("Could not remove chunked-upload session dir %s", upload_id)


def _resolve_content_type(content_type: str, filename: str) -> str:
    """Infer MIME type from extension when browser reports application/octet-stream."""
    if content_type == "application/octet-stream":
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return _EXT_MAP.get(ext, content_type)
    return content_type


def _s3_client():
    import boto3
    from botocore.config import Config as _BotocoreConfig

    from app.core.config import settings as _s

    kwargs: dict = {
        "region_name": _s.AWS_REGION,
        "config": _BotocoreConfig(signature_version="s3v4"),
    }
    if _s.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = _s.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = _s.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


async def _fetch_ad_or_404(db: AsyncSession, ad_id: str, company_id: str) -> Advertisement:
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")
    return ad


# ── Token-based auth for browser-opened file URLs ────────────────────────────


async def _user_from_query_token(
    token: str,
    db: AsyncSession,
) -> User:
    from app.core.security import decode_token

    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


# ── Direct upload ─────────────────────────────────────────────────────────────


@router.post("/{ad_id}/documents", response_model=AdvertisementDocumentOut)
async def upload_protocol_document(
    ad_id: str,
    doc_type: str = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a campaign-specific protocol document.
    Stored separately from company documents — never appears on My Company page.
    Saved to uploads/docs/<company_id>/<ad_id>/<filename>.
    Created with priority=10 so the curator treats these as higher-priority
    context than generic company documents (priority=0).
    """
    await _fetch_ad_or_404(db, ad_id, user.company_id)

    if file.content_type not in ALLOWED_PROTOCOL_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: PDF, DOCX, DOC, TXT, MD.",
        )

    file_path = await file_storage.save(
        file=file,
        subfolder=f"docs/{user.company_id}/{ad_id}",
        filename=file.filename,
    )

    disk_path = url_to_disk_path(file_path, BACKEND_ROOT)
    content = extract_text(disk_path)

    doc = AdvertisementDocument(
        company_id=user.company_id,
        advertisement_id=ad_id,
        doc_type=doc_type,
        title=title,
        content=content,
        file_path=file_path,
        priority=10,
    )
    db.add(doc)
    await db.flush()
    return doc


# ── Chunked upload ────────────────────────────────────────────────────────────


@router.post("/{ad_id}/documents/start")
async def start_document_upload(
    ad_id: str,
    req: StartUploadRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Begin a chunked upload session. Returns an upload_id for subsequent calls."""
    await _fetch_ad_or_404(db, ad_id, user.company_id)

    effective_content_type = _resolve_content_type(req.content_type, req.filename)

    if effective_content_type not in ALLOWED_PROTOCOL_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: PDF, DOCX, DOC, TXT, MD.",
        )

    upload_id = str(uuid_mod.uuid4())
    sess_dir = _session_dir(upload_id)
    os.makedirs(sess_dir, exist_ok=True)
    meta = {
        "ad_id": ad_id,
        "company_id": user.company_id,
        "doc_type": req.doc_type,
        "title": req.title,
        "filename": req.filename,
        "content_type": effective_content_type,
        "total_chunks": req.total_chunks,
    }
    with open(os.path.join(sess_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    return {"upload_id": upload_id}


@router.post("/{ad_id}/documents/chunk")
async def upload_document_chunk(
    ad_id: str,
    req: ChunkUploadRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
):
    """Receive one base64-encoded chunk for an in-progress upload."""
    session = _load_session_meta(req.upload_id)
    if not session or session["ad_id"] != ad_id or session["company_id"] != user.company_id:
        raise HTTPException(status_code=404, detail="Upload session not found")

    try:
        chunk_bytes = base64.b64decode(req.data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 chunk data")

    chunk_path = os.path.join(_session_dir(req.upload_id), f"chunk_{req.chunk_index}.bin")
    with open(chunk_path, "wb") as f:
        f.write(chunk_bytes)

    return {"received": req.chunk_index}


@router.post("/{ad_id}/documents/finalize", response_model=AdvertisementDocumentOut)
async def finalize_document_upload(
    ad_id: str,
    req: FinalizeUploadRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Assemble all chunks and create the AdvertisementDocument record."""
    session = _load_session_meta(req.upload_id)
    if not session or session["ad_id"] != ad_id or session["company_id"] != user.company_id:
        raise HTTPException(status_code=404, detail="Upload session not found")

    expected = session["total_chunks"]
    sess_dir = _session_dir(req.upload_id)
    missing = [i for i in range(expected) if not os.path.exists(os.path.join(sess_dir, f"chunk_{i}.bin"))]
    if missing:
        raise HTTPException(status_code=400, detail="Incomplete upload — missing chunks")

    parts = []
    for i in range(expected):
        with open(os.path.join(sess_dir, f"chunk_{i}.bin"), "rb") as f:
            parts.append(f.read())
    file_bytes = b"".join(parts)
    _delete_session(req.upload_id)

    file_path = await file_storage.save_bytes(
        data=file_bytes,
        subfolder=f"docs/{user.company_id}/{ad_id}",
        filename=session["filename"],
    )

    disk_path = url_to_disk_path(file_path, BACKEND_ROOT)
    content = extract_text(disk_path)

    doc = AdvertisementDocument(
        company_id=user.company_id,
        advertisement_id=ad_id,
        doc_type=session["doc_type"],
        title=session["title"],
        content=content,
        file_path=file_path,
        priority=10,
    )
    db.add(doc)
    await db.flush()
    return doc


# ── S3 pre-signed upload ──────────────────────────────────────────────────────


@router.post("/{ad_id}/documents/presign", response_model=PresignResponse)
async def get_document_presign_url(
    ad_id: str,
    req: PresignRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Return a pre-signed S3 PUT URL so the browser can upload directly to S3,
    bypassing CloudFront WAF body-size restrictions.

    If S3 is not configured (S3_UPLOAD_BUCKET unset) returns {"method": "direct"}
    and the frontend falls back to the regular multipart endpoint.
    """
    from app.core.config import settings as _s

    await _fetch_ad_or_404(db, ad_id, user.company_id)

    if req.file_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")

    effective_ct = _resolve_content_type(req.content_type, req.filename)

    if effective_ct not in ALLOWED_PROTOCOL_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: PDF, DOCX, DOC, TXT, MD.",
        )

    if not _s.S3_UPLOAD_BUCKET:
        return PresignResponse(method="direct")

    from app.services.storage import sanitize_filename

    s3_key = f"{_s.S3_UPLOAD_PREFIX}/{user.company_id}/{ad_id}/{uuid_mod.uuid4()}_{sanitize_filename(req.filename)}"
    try:
        client = _s3_client()
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
        logger.error("Failed to generate S3 pre-signed URL: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not generate upload URL")

    return PresignResponse(method="s3", upload_url=upload_url, s3_key=s3_key, content_type=effective_ct)


@router.post("/{ad_id}/documents/confirm", response_model=AdvertisementDocumentOut)
async def confirm_s3_document_upload(
    ad_id: str,
    req: ConfirmUploadRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Called after the browser has PUT the file to S3.
    Downloads the file from S3, extracts text, saves to EFS, creates DB record.
    Deletes the temporary S3 object when done.
    """
    from app.core.config import settings as _s

    if not _s.S3_UPLOAD_BUCKET:
        raise HTTPException(status_code=400, detail="S3 upload not configured on this server")

    await _fetch_ad_or_404(db, ad_id, user.company_id)

    expected_prefix = f"{_s.S3_UPLOAD_PREFIX}/{user.company_id}/{ad_id}/"
    if not req.s3_key.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="Invalid upload key")

    try:
        client = _s3_client()
        s3_obj = await asyncio.to_thread(client.get_object, Bucket=_s.S3_UPLOAD_BUCKET, Key=req.s3_key)
        file_bytes = await asyncio.to_thread(lambda: s3_obj["Body"].read())
    except Exception as e:
        logger.error("Failed to download from S3 key %s: %s", req.s3_key, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not retrieve uploaded file")

    file_path = await file_storage.save_bytes(
        data=file_bytes,
        subfolder=f"docs/{user.company_id}/{ad_id}",
        filename=req.filename,
    )

    disk_path = url_to_disk_path(file_path, BACKEND_ROOT)
    content = extract_text(disk_path)

    doc = AdvertisementDocument(
        company_id=user.company_id,
        advertisement_id=ad_id,
        doc_type=req.doc_type,
        title=req.title,
        content=content,
        file_path=file_path,
        priority=10,
    )
    db.add(doc)
    await db.flush()

    try:
        await asyncio.to_thread(client.delete_object, Bucket=_s.S3_UPLOAD_BUCKET, Key=req.s3_key)
    except Exception:
        logger.warning("Could not delete staging S3 object %s", req.s3_key)

    return doc


# ── Document list + file serving ──────────────────────────────────────────────


@router.get("/{ad_id}/documents", response_model=list[AdvertisementDocumentOut])
async def list_protocol_documents(
    ad_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all protocol documents for an advertisement."""
    result = await db.execute(
        select(AdvertisementDocument).where(
            AdvertisementDocument.advertisement_id == ad_id,
            AdvertisementDocument.company_id == user.company_id,
        )
    )
    return result.scalars().all()


@router.get("/{ad_id}/documents/{doc_id}/file")
async def serve_protocol_document_file(
    ad_id: str,
    doc_id: str,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Stream a campaign protocol document file. Auth via ?token= query param."""
    user = await _user_from_query_token(token, db)
    result = await db.execute(
        select(AdvertisementDocument).where(
            AdvertisementDocument.id == doc_id,
            AdvertisementDocument.advertisement_id == ad_id,
            AdvertisementDocument.company_id == user.company_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.file_path:
        raise HTTPException(status_code=404, detail="No file attached to this document")

    relative = doc.file_path.lstrip("/").removeprefix("uploads/")
    disk_path = os.path.normpath(os.path.join(BACKEND_ROOT, "uploads", relative))

    if not os.path.exists(disk_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    media_type, _ = mimetypes.guess_type(disk_path)
    return FileResponse(path=disk_path, media_type=media_type or "application/octet-stream")
