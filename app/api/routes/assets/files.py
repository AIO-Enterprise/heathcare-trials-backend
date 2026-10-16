"""
Auth-checked file serving for stored user assets.

Creative output URLs ("/outputs/<company>/<ad>/<file>") and upload URLs
("/uploads/...") have no open mount by design — <img>/<video> tags resolve
them through here as GET /api/files/outputs/... with the auth cookie
(same-origin), so reads stay user-protected per the onboarding rule.
"""

import mimetypes

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.core.security import get_current_user
from app.models.models import User, UserRole
from app.services.storage import file_storage

router = APIRouter(tags=["Files"])

_SERVED = ("/outputs/", "/uploads/")


def _cache_control_for(key: str, media_type: str) -> str:
    lowered = key.lower()
    if media_type == "text/html" or lowered.endswith((".html", ".htm")):
        return "no-cache, no-store, must-revalidate"
    if lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm", ".pdf")):
        return "private, max-age=31536000, immutable"
    return "private, max-age=3600"


@router.get("/files/{file_path:path}")
async def serve_file(file_path: str, user: User = Depends(get_current_user)):
    key = "/" + (file_path or "").lstrip("/")
    if not key.startswith(_SERVED):
        raise HTTPException(status_code=404, detail="Not found")
    if ".." in key.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    # Tenant scope — the company id is embedded in every stored path
    # (outputs/<company>/…, uploads/logos/<company>_…, docs/<company>/…).
    # 404 (not 403) so cross-tenant ids look nonexistent. SUPER_USER bypasses.
    if user.role != UserRole.SUPER_USER and (not user.company_id or user.company_id not in key):
        raise HTTPException(status_code=404, detail="Not found")
    data = file_storage.open_bytes(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Not found")
    # ponytail: whole-file Response; FileResponse + Range when video seeking is measured
    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": _cache_control_for(key, media_type)},
    )
