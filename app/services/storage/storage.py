"""
File Storage Service
Owner: Backend Dev 2

Abstraction layer for all file persistence.
Currently saves to local disk under <backend_root>/uploads/.

BASE_DIR is resolved to an absolute path anchored to this file's location,
so it is always correct regardless of where uvicorn is launched from.

TODO: Swap to Azure Blob Storage here only — no other files need to change.
  Steps when ready:
    1. pip install azure-storage-blob
    2. Add AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER to .env
    3. Replace LocalStorageBackend with AzureBlobBackend below
    4. AzureBlobBackend.save() uploads to blob and returns the public URL
    5. Remove the Docker volume mount for ./uploads in docker-compose.yml

Usage:
    from app.services.storage import file_storage

    url = await file_storage.save(file, subfolder="logos", filename="myfile.png")
    # returns "/uploads/logos/myfile.png" (local) or full Azure URL (blob)
"""

import os
import re
import secrets

from fastapi import UploadFile

# Resolve absolute path to <backend_root>/uploads/ regardless of CWD.
# This file lives at backend/app/services/storage/storage.py, so:
#   __file__         → .../backend/app/services/storage/storage.py
#   4x dirname       → .../backend/
#   + "uploads"      → .../backend/uploads/
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


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


class LocalStorageBackend:
    """
    Saves files to <backend_root>/uploads/<subfolder>/ on the local filesystem.
    Returns a relative URL path string stored in the DB.
    Files persist across container restarts only if a Docker volume
    is mounted at ./uploads — see docker-compose.yml TODO.
    """

    BASE_DIR = os.path.join(_BACKEND_ROOT, "uploads")

    async def save_bytes(self, data: bytes, subfolder: str, filename: str) -> str:
        """
        Save raw bytes and return the stored path/URL.
        Used by the chunked upload flow to assemble and persist a file
        without requiring a FastAPI UploadFile object.
        """
        filename = sanitize_filename(filename)
        dest_dir = os.path.join(self.BASE_DIR, subfolder)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)
        with open(dest_path, "wb") as f:
            f.write(data)
        return f"/uploads/{subfolder}/{filename}"

    async def save(self, file: UploadFile, subfolder: str, filename: str) -> str:
        """
        Save an uploaded file and return the stored path/URL.

        Args:
            file:      FastAPI UploadFile object
            subfolder: e.g. "logos" or "docs/<company_id>"
            filename:  final filename including extension

        Returns:
            Relative URL string, e.g. "/uploads/logos/abc123.png"
        """
        filename = sanitize_filename(filename)
        dest_dir = os.path.join(self.BASE_DIR, subfolder)
        os.makedirs(dest_dir, exist_ok=True)

        dest_path = os.path.join(dest_dir, filename)
        contents = await file.read()
        with open(dest_path, "wb") as f:
            f.write(contents)

        return f"/uploads/{subfolder}/{filename}"


# ── Active backend ────────────────────────────────────────────────────────────
# To swap to Azure Blob Storage, replace LocalStorageBackend() with
# AzureBlobBackend() here. Nothing else in the codebase needs to change.

file_storage = LocalStorageBackend()
