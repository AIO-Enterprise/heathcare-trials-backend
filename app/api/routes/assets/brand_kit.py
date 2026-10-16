"""
Brand Kit Routes
Owner: Backend Dev 2
Dependencies: M1, M2

POST /onboarding/logo        — Upload company logo, returns stored URL
POST /brand-kit/             — Create brand kit during onboarding (Admin only)
GET  /brand-kit/             — Fetch company brand kit
PATCH /brand-kit/            — Update brand kit (editable from settings)
POST /brand-kit/extract-pdf  — Extract brand colors/fonts from a PDF (returns JSON, does not save)
"""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.security import get_current_user, require_roles
from app.db.database import get_db
from app.models.models import BrandKit, User, UserRole
from app.schemas.schemas import BrandKitCreate, BrandKitOut, BrandKitUpdate, LogoUploadResponse
from app.services.storage import file_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Brand Kit"])

ALLOWED_LOGO_TYPES = {"image/jpeg", "image/jpg", "image/png"}
# SVG is intentionally not accepted: a stored SVG can carry scripts that
# execute when the file is opened directly on our origin (stored XSS).


# ─── Logo Upload ──────────────────────────────────────────────────────────────


@router.post("/onboarding/logo", response_model=LogoUploadResponse)
async def upload_logo(
    file: UploadFile = File(...),
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
):
    """
    Upload company logo after registration.
    Accepts JPEG, PNG, SVG. Returns the stored URL.
    File is saved via the storage service (currently local, swap to Azure Blob
    by updating app/services/storage.py only).
    """
    if file.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Accepted: JPEG, PNG.",
        )

    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"
    safe_ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8] or "png"
    filename = f"{user.company_id}_{uuid.uuid4().hex}.{safe_ext}"

    logo_url = file_storage.save(
        file=file,
        subfolder="logos",
        filename=filename,
    )

    return LogoUploadResponse(logo_url=logo_url)


# ─── Brand Kit CRUD ───────────────────────────────────────────────────────────


@router.post("/brand-kit/", response_model=BrandKitOut)
async def create_brand_kit(
    body: BrandKitCreate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Create brand kit for the company during onboarding.
    If one already exists, raises 409 — use PATCH to update.
    """
    existing = await db.execute(select(BrandKit).where(BrandKit.company_id == user.company_id))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Brand kit already exists. Use PATCH /brand-kit/ to update.",
        )

    brand_kit = BrandKit(
        company_id=user.company_id,
        primary_color=body.primary_color,
        secondary_color=body.secondary_color,
        accent_color=body.accent_color,
        primary_font=body.primary_font,
        secondary_font=body.secondary_font,
        adjectives=body.adjectives,
        dos=body.dos,
        donts=body.donts,
        preset_name=body.preset_name,
        pdf_path=body.pdf_path,
    )
    db.add(brand_kit)
    await db.flush()
    return brand_kit


@router.get("/brand-kit/", response_model=BrandKitOut)
async def get_brand_kit(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch the company brand kit."""
    result = await db.execute(select(BrandKit).where(BrandKit.company_id == user.company_id))
    brand_kit = result.scalar_one_or_none()
    if not brand_kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")
    return brand_kit


@router.patch("/brand-kit/", response_model=BrandKitOut)
async def update_brand_kit(
    body: BrandKitUpdate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Update brand kit — callable from onboarding or settings page."""
    result = await db.execute(select(BrandKit).where(BrandKit.company_id == user.company_id))
    brand_kit = result.scalar_one_or_none()
    if not brand_kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(brand_kit, field, value)
    return brand_kit


# ─── Brand Kit PDF Storage ───────────────────────────────────────────────────


@router.post("/brand-kit/upload-pdf")
async def upload_brand_pdf(
    file: UploadFile = File(...),
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
):
    """
    Upload and persist the brand guidelines PDF for a company.
    Returns {"pdf_path": "<stored path>"} — pass this as pdf_path
    when calling POST or PATCH /brand-kit/.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    filename = f"{user.company_id}_{uuid.uuid4().hex}.pdf"
    pdf_path = file_storage.save(
        file=file,
        subfolder="brand-pdfs",
        filename=filename,
    )
    return {"pdf_path": pdf_path}


# ─── PDF Brand Extraction ─────────────────────────────────────────────────────


@router.post("/brand-kit/extract-pdf")
async def extract_brand_from_pdf(file: UploadFile = File(...)):
    """
    Upload a brand guidelines PDF and extract color/font/tone signals from it.
    Uses pymupdf to read vector colors and font names, then the generic LLM
    slot to interpret them into clean hex values and Google Font names.

    Public (no auth): the onboarding brand step calls this before the company
    registers, so no user/company exists yet to scope to. It is stateless —
    returns the extracted brand fields as JSON, saves nothing. Abuse is
    bounded by the rate-limit bucket plus the file-size cap below.

    Returns the extracted brand fields as JSON — does NOT save to the database.
    The client should review and then call POST/PATCH /brand-kit/ to persist.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted for brand extraction.",
        )

    pdf_bytes = await file.read()
    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(pdf_bytes) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds the 12 MB limit.")

    try:
        from app.services.storage.brand_kit_extractor import extract_brand_from_pdf

        result = await extract_brand_from_pdf(pdf_bytes)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="pymupdf is not installed on this server. Run `pip install pymupdf` and restart.",
        )
    except AppError:
        raise
    except Exception:
        logger.exception("Brand extraction failed")
        raise HTTPException(status_code=500, detail="Brand extraction failed — please check the PDF and retry")

    return result
