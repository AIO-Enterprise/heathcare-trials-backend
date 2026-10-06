from datetime import datetime

from pydantic import BaseModel

from app.schemas.enums import DocumentTypeEnum

# ─── Company Documents ────────────────────────────────────────────────────────


class DocumentCreate(BaseModel):
    doc_type: DocumentTypeEnum
    title: str
    content: str | None = None


class DocumentOut(BaseModel):
    id: str
    doc_type: str
    title: str
    content: str | None = None
    file_path: str | None = None
    priority: int
    version: int
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    priority: int | None = None


# ─── Advertisement Documents ──────────────────────────────────────────────────


class AdvertisementDocumentOut(BaseModel):
    id: str
    advertisement_id: str
    doc_type: str
    title: str
    file_path: str | None = None
    priority: int
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Chunked Upload ───────────────────────────────────────────────────────────


class StartUploadRequest(BaseModel):
    doc_type: str
    title: str
    filename: str
    content_type: str
    total_chunks: int


class ChunkUploadRequest(BaseModel):
    upload_id: str
    chunk_index: int
    data: str  # base64-encoded chunk bytes


class FinalizeUploadRequest(BaseModel):
    upload_id: str


# ─── S3 Pre-signed Upload ─────────────────────────────────────────────────────


class PresignRequest(BaseModel):
    doc_type: str
    title: str
    filename: str
    content_type: str
    file_size: int


class PresignResponse(BaseModel):
    method: str  # "s3" | "direct"
    upload_url: str | None = None
    s3_key: str | None = None
    content_type: str | None = None


class ConfirmUploadRequest(BaseModel):
    s3_key: str
    doc_type: str
    title: str
    filename: str
    content_type: str
