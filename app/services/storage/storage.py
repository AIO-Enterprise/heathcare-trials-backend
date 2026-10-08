"""
File Storage Service — provider-agnostic backend for user assets.

Stored path values ("/uploads/...", "/outputs/...") are identical for every
provider; only the bytes move. Select with STORAGE_BACKEND=local|s3|azure.

Usage:
    from app.services.storage import file_storage

    url = file_storage.save(file, subfolder="logos", filename="myfile.png")
    # returns media_url("/uploads/logos/myfile.png") on every backend
"""

import logging
import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass

from fastapi import UploadFile

from app.core.config import settings
from app.core.media import media_url

logger = logging.getLogger(__name__)

# Resolve absolute path to <backend_root>/uploads/ regardless of CWD.
# This file lives at backend/app/services/storage/storage.py, so:
#   __file__         → .../backend/app/services/storage/storage.py
#   4x dirname       → .../backend/
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _resolve_dir(configured: str, default: str) -> str:
    """Absolute configured dirs win; relative ones anchor at the backend root."""
    if os.path.isabs(configured):
        return configured
    return os.path.join(_BACKEND_ROOT, configured or default)


def sanitize_filename(original: str | None) -> str:
    """
    Build a collision-free, URL-safe filename from user-supplied input.

    Strips directory components, replaces non-[A-Za-z0-9._-] chars with '_',
    and prepends a short random token so concurrent uploads of the same name
    never overwrite each other.

    Every write path in this module applies this automatically; callers that
    build object keys elsewhere (e.g. S3 keys) should use it explicitly too.
    """
    base = os.path.basename(original or "upload")
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "file"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext).lower()
    return f"{secrets.token_hex(4)}_{stem}{ext}"


def _norm_key(path: str) -> str:
    """Strip backend base URL + leading slash → object key ("uploads/a/b")."""
    from app.core.config import settings

    base = (settings.BACKEND_PUBLIC_URL or "").rstrip("/")
    if base and path.startswith(base):
        path = path[len(base) :]
    return path.lstrip("/")


@dataclass
class PresignedUpload:
    upload_url: str
    key: str
    content_type: str


class StorageBackend(ABC):
    """Sync provider interface. Paths are stored values ("/uploads/...")."""

    @abstractmethod
    def save_bytes(self, data: bytes, subfolder: str, filename: str) -> str:
        """Persist bytes, return the stored path value."""

    @abstractmethod
    def open_bytes(self, path: str) -> bytes | None:
        """Read bytes, or None when missing."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        ...

    @abstractmethod
    def delete(self, path: str) -> None:
        """Best-effort removal; never raises for missing keys."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Best-effort removal of everything under a stored prefix ("/outputs/c1/a1")."""

    @abstractmethod
    def presign_upload(self, subfolder: str, filename: str, content_type: str) -> PresignedUpload | None:
        """Browser-direct PUT URL, or None when the provider has no presigning (local)."""

    def save(self, file: UploadFile, subfolder: str, filename: str) -> str:
        """Sync read + save (UploadFile.read is sync in Starlette's SpooledFile)."""
        # ponytail: blocks the loop on large files exactly like the old async version did — wrap in to_thread when measured
        return self.save_bytes(file.file.read(), subfolder, filename)


class LocalStorageBackend(StorageBackend):
    """
    Saves files under <backend_root>/{uploads,outputs,static}/.
    Files persist across container restarts only if a Docker volume is mounted.
    """

    def _disk_path(self, path: str) -> str | None:
        key = _norm_key(path)
        roots = {
            "uploads/": _resolve_dir(settings.UPLOAD_DIR, "uploads"),
            "outputs/": _resolve_dir(settings.OUTPUT_DIR, "outputs"),
            "static/": _resolve_dir(settings.STATIC_DIR, "static"),
        }
        for prefix, root in roots.items():
            if key.startswith(prefix):
                # Boundary-check against path traversal (e.g. ../../etc/passwd).
                base = os.path.realpath(root)
                disk = os.path.realpath(os.path.join(base, key[len(prefix):]))
                if disk == base or disk.startswith(base + os.sep):
                    return disk
                return None
        return None

    def save_bytes(self, data: bytes, subfolder: str, filename: str) -> str:
        # subfolder may be "docs/<company_id>" (→ uploads/) or a full
        # "outputs/<company_id>/<ad_id>" / "static/..." prefix for generated assets.
        filename = sanitize_filename(filename)
        head, _, rest = (subfolder.strip("/") + "/").partition("/")
        if head in ("outputs", "static"):
            dest_dir = os.path.join(
                _resolve_dir(settings.OUTPUT_DIR if head == "outputs" else settings.STATIC_DIR, head),
                rest.rstrip("/"),
            )
            stored = f"/{head}/{rest.rstrip('/')}/{filename}" if rest.rstrip("/") else f"/{head}/{filename}"
        else:
            dest_dir = os.path.join(_resolve_dir(settings.UPLOAD_DIR, "uploads"), subfolder)
            stored = f"/uploads/{subfolder}/{filename}"
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, filename), "wb") as f:
            f.write(data)
        logger.info("Saved %d bytes → %s", len(data), stored)
        return media_url(stored)

    def open_bytes(self, path: str) -> bytes | None:
        disk = self._disk_path(path)
        if not disk or not os.path.exists(disk):
            return None
        with open(disk, "rb") as f:
            return f.read()

    def exists(self, path: str) -> bool:
        disk = self._disk_path(path)
        return bool(disk and os.path.exists(disk))

    def delete(self, path: str) -> None:
        disk = self._disk_path(path)
        if disk and os.path.exists(disk):
            os.remove(disk)

    def delete_prefix(self, prefix: str) -> None:
        import shutil

        key = _norm_key(prefix).rstrip("/") + "/"
        roots = {"uploads/": settings.UPLOAD_DIR, "outputs/": settings.OUTPUT_DIR, "static/": settings.STATIC_DIR}
        for root_prefix, configured in roots.items():
            if not key.startswith(root_prefix):
                continue
            base = os.path.realpath(_resolve_dir(configured, root_prefix.rstrip("/")))
            target = os.path.realpath(os.path.join(base, key[len(root_prefix):]))
            if target == base or target.startswith(base + os.sep):
                shutil.rmtree(target, ignore_errors=True)

    def presign_upload(self, subfolder: str, filename: str, content_type: str) -> PresignedUpload | None:
        return None


def get_s3_client():
    """Boto3 S3 client from the configured AWS credentials/region (shared)."""
    import boto3
    from botocore.config import Config as _BotocoreConfig

    kwargs: dict = {
        "region_name": settings.AWS_REGION,
        "config": _BotocoreConfig(signature_version="s3v4"),
    }
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def _bucket() -> str:
    return settings.STORAGE_BUCKET or settings.S3_UPLOAD_BUCKET or ""


def _object_key(path: str) -> str:
    key = _norm_key(path)
    prefix = (settings.STORAGE_PREFIX or settings.S3_UPLOAD_PREFIX or "").strip("/")
    return f"{prefix}/{key}" if prefix else key


class S3StorageBackend(StorageBackend):
    """AWS S3. Stored values stay backend-relative; bytes live under STORAGE_PREFIX."""

    def save_bytes(self, data: bytes, subfolder: str, filename: str) -> str:
        filename = sanitize_filename(filename)
        head, _, rest = (subfolder.strip("/") + "/").partition("/")
        if head in ("outputs", "static"):
            stored = f"/{head}/{rest.rstrip('/')}/{filename}" if rest.rstrip("/") else f"/{head}/{filename}"
        else:
            stored = f"/uploads/{subfolder}/{filename}"
        get_s3_client().put_object(Bucket=_bucket(), Key=_object_key(stored), Body=data)
        logger.info("Saved %d bytes → s3:%s", len(data), stored)
        return media_url(stored)

    def open_bytes(self, path: str) -> bytes | None:
        try:
            return get_s3_client().get_object(Bucket=_bucket(), Key=_object_key(path))["Body"].read()
        except Exception:
            return None

    def exists(self, path: str) -> bool:
        try:
            get_s3_client().head_object(Bucket=_bucket(), Key=_object_key(path))
            return True
        except Exception:
            return False

    def delete(self, path: str) -> None:
        try:
            get_s3_client().delete_object(Bucket=_bucket(), Key=_object_key(path))
        except Exception:
            pass

    def delete_prefix(self, prefix: str) -> None:
        # ponytail: list+delete in 1000-key pages; lifecycle rules are cheaper at real scale — switch when measured
        try:
            client = get_s3_client()
            key = _object_key(prefix).rstrip("/") + "/"
            token = None
            while True:
                kwargs = {"Bucket": _bucket(), "Prefix": key, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                page = client.list_objects_v2(**kwargs)
                objs = page.get("Contents", [])
                if objs:
                    client.delete_objects(
                        Bucket=_bucket(), Delete={"Objects": [{"Key": o["Key"]} for o in objs]}
                    )
                token = page.get("NextContinuationToken")
                if not token:
                    break
        except Exception:
            pass

    def presign_upload(self, subfolder: str, filename: str, content_type: str) -> PresignedUpload | None:
        filename = sanitize_filename(filename)
        stored = f"/uploads/{subfolder}/{filename}"
        url = get_s3_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": _bucket(), "Key": _object_key(stored), "ContentType": content_type},
            ExpiresIn=3600,
        )
        return PresignedUpload(upload_url=url, key=stored, content_type=content_type)


class AzureBlobStorageBackend(StorageBackend):
    """Azure Blob Storage. Container = STORAGE_BUCKET. Untested locally — verify against Azurite/prod.
    # ponytail: no emulator run yet; interface mirrors S3 1:1 so local-tested call sites carry over"""

    def _client(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise RuntimeError("pip install azure-storage-blob (STORAGE_BACKEND=azure)")
        if not settings.AZURE_STORAGE_CONNECTION_STRING:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required (STORAGE_BACKEND=azure)")
        return BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)

    def _blob(self, path: str):
        return self._client().get_blob_client(container=_bucket(), blob=_object_key(path))

    def save_bytes(self, data: bytes, subfolder: str, filename: str) -> str:
        filename = sanitize_filename(filename)
        head, _, rest = (subfolder.strip("/") + "/").partition("/")
        if head in ("outputs", "static"):
            stored = f"/{head}/{rest.rstrip('/')}/{filename}" if rest.rstrip("/") else f"/{head}/{filename}"
        else:
            stored = f"/uploads/{subfolder}/{filename}"
        self._blob(stored).upload_blob(data, overwrite=True)
        logger.info("Saved %d bytes → azure:%s", len(data), stored)
        return media_url(stored)

    def open_bytes(self, path: str) -> bytes | None:
        try:
            return self._blob(path).download_blob().readall()
        except Exception:
            return None

    def exists(self, path: str) -> bool:
        try:
            return self._blob(path).exists()
        except Exception:
            return False

    def delete(self, path: str) -> None:
        try:
            self._blob(path).delete_blob()
        except Exception:
            pass

    def delete_prefix(self, prefix: str) -> None:
        try:
            container = self._client().get_container_client(_bucket())
            key = _object_key(prefix).rstrip("/") + "/"
            for blob in container.list_blobs(name_starts_with=key):
                try:
                    container.delete_blob(blob.name)
                except Exception:
                    pass
        except Exception:
            pass

    def presign_upload(self, subfolder: str, filename: str, content_type: str) -> PresignedUpload | None:
        from datetime import UTC, datetime, timedelta

        try:
            from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        except ImportError:
            raise RuntimeError("pip install azure-storage-blob (STORAGE_BACKEND=azure)")
        filename = sanitize_filename(filename)
        stored = f"/uploads/{subfolder}/{filename}"
        sas = generate_blob_sas(
            account_name=self._client().account_name,
            container_name=_bucket(),
            blob_name=_object_key(stored),
            account_key=self._client().credential.account_key,
            permission=BlobSasPermissions(write=True, create=True),
            expiry=datetime.now(UTC) + timedelta(hours=1),
            content_type=content_type,
        )
        return PresignedUpload(
            upload_url=f"{self._blob(stored).url}?{sas}", key=stored, content_type=content_type
        )


# ── Active backend ────────────────────────────────────────────────────────────
# Selected once at import. Priority:
#   1. Explicit STORAGE_BACKEND=s3|azure|local overrides everything.
#   2. AZURE_STORAGE_CONNECTION_STRING present → azure
#   3. STORAGE_BUCKET or S3_UPLOAD_BUCKET present → s3
#   4. Fallback → local

file_storage: StorageBackend


def _select_backend() -> StorageBackend:
    explicit = (settings.STORAGE_BACKEND or "").lower()
    if explicit == "s3":
        logger.info("Storage backend: s3 (explicit)")
        return S3StorageBackend()
    if explicit == "azure":
        logger.info("Storage backend: azure (explicit)")
        return AzureBlobStorageBackend()
    if explicit == "local":
        logger.info("Storage backend: local (explicit)")
        return LocalStorageBackend()
    # Auto-detect from present credentials
    if settings.AZURE_STORAGE_CONNECTION_STRING:
        logger.info("Storage backend: azure (auto — AZURE_STORAGE_CONNECTION_STRING present)")
        return AzureBlobStorageBackend()
    if settings.STORAGE_BUCKET or settings.S3_UPLOAD_BUCKET:
        logger.info("Storage backend: s3 (auto — STORAGE_BUCKET present)")
        return S3StorageBackend()
    logger.info("Storage backend: local (default)")
    return LocalStorageBackend()


def storage_is_local() -> bool:
    """True when the active backend writes to local disk (not S3 or Azure)."""
    return isinstance(file_storage, LocalStorageBackend)


file_storage = _select_backend()
