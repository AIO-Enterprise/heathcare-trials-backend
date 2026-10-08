"""
M3: Onboarding Routes
Owner: Backend Dev 2
Dependencies: M1 (models), M2 (auth), M4 (training service)

POST /onboarding/             — Register company + admin (one-time)
POST /onboarding/documents    — Upload company documents
POST /onboarding/train        — Trigger AI training (skill initialization)

File storage note:
  Document files are saved via app/services/storage.py (currently local disk).
  To migrate to Azure Blob Storage, update storage.py only — no changes needed here.
"""

import asyncio
import logging
import traceback

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, require_roles
from app.db.database import async_session_factory, get_db
from app.models.models import Company, CompanyDocument, User, UserRole
from app.schemas.schemas import (
    DocumentOut,
    OnboardingRequest,
    OnboardingResponse,
)
from app.services.storage import file_storage, sanitize_filename
from app.services.storage.extractor import extract_bytes
from app.services.training.trainer import TrainingService


def _safe_filename(original: str | None) -> str:
    """Deprecated alias — see app.services.storage.storage.sanitize_filename."""
    return sanitize_filename(original)


logger = logging.getLogger(__name__)


async def _background_train(company_id: str) -> None:
    async with async_session_factory() as db:
        try:
            await TrainingService(db).train_company_skills(company_id)
        except Exception as exc:
            logger.error("Background training failed for company %s: %s", company_id, exc)


async def _background_train_and_mark(company_id: str) -> None:
    """Run AI skill training in the background. Company is already marked onboarded before this runs."""
    async with async_session_factory() as db:
        try:
            await TrainingService(db).train_company_skills(company_id)
        except Exception as exc:
            logger.error("Background training failed for company %s: %s", company_id, exc)


router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


@router.post("/", response_model=OnboardingResponse)
async def onboard_company(body: OnboardingRequest, db: AsyncSession = Depends(get_db)):
    """
    One-time company onboarding.
    Creates the company record and registers the ADMIN user.
    Returns 409 with detail "company_exists" or "email_exists" for duplicates
    so the frontend can show a targeted message.
    """
    # Check duplicate company name
    existing = await db.execute(select(Company).where(Company.name == body.company_name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="company_exists")

    # Check duplicate email
    existing_user = await db.execute(select(User).where(User.email == body.admin_email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email_exists")

    # Create company
    company = Company(
        name=body.company_name,
        industry=body.industry,
        logo_url=body.logo_url,
    )
    db.add(company)
    await db.flush()

    # Create the company owner as a Super User — the merged account that can
    # perform every stage itself. They may later add Study Coordinators (and the
    # other delegate roles) from User Management if they want to split the work.
    admin = User(
        company_id=company.id,
        email=body.admin_email,
        hashed_pw=hash_password(body.admin_password),
        full_name=body.admin_name,
        role=UserRole.SUPER_USER,
    )
    db.add(admin)

    try:
        await db.flush()
    except IntegrityError:
        # Safety net for race condition between the check above and the insert
        await db.rollback()
        raise HTTPException(status_code=409, detail="email_exists")

    # onboarded stays False until POST /onboarding/train succeeds.

    return OnboardingResponse(company_id=company.id, admin_user_id=admin.id)


@router.post("/documents", response_model=DocumentOut)
async def upload_document(
    doc_type: str = Form(...),
    title: str = Form(...),
    content: str = Form(None),
    file: UploadFile = File(None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload company-specific documents during or after onboarding.
    If a file is provided, its text is extracted and stored in content
    so the AI skills receive the actual document text.
    Re-trains AI skills in the background after saving.
    """
    file_path = None
    if file:
        safe_name = _safe_filename(file.filename)
        try:
            file_path = file_storage.save(
                file=file,
                subfolder=f"docs/{user.company_id}",
                filename=safe_name,
            )
        except Exception as exc:
            logger.error(
                "Onboarding upload: file_storage.save failed (company=%s, filename=%s): %s\n%s",
                user.company_id,
                file.filename,
                exc,
                traceback.format_exc(),
            )
            raise HTTPException(
                status_code=500,
                detail=f"Could not save file to storage: {exc}",
            )

        if not content:
            try:
                content = await asyncio.to_thread(
                    extract_bytes, file_storage.open_bytes(file_path), file_path
                )
            except Exception as exc:
                logger.warning(
                    "Onboarding upload: extract_text failed for %s: %s — saving with empty content",
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


@router.post("/train", status_code=202)
async def trigger_training(
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger AI Training (skill-creator) — runs in background to avoid gateway timeouts.
    Returns 202 immediately; training (Claude × 2 skills) continues asynchronously.
    1. Mark company as onboarded immediately so the user can access the platform.
    2. Read skill.md templates for Curator and Reviewer.
    3. Fill placeholders with company-specific data from onboarding.
    4. Generate customized skill.md files in the background.
    """
    company_result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = company_result.scalar_one_or_none()
    if company and not company.onboarded:
        company.onboarded = True
        await db.commit()

    background_tasks.add_task(_background_train_and_mark, user.company_id)
    return {"status": "training_started", "company_id": user.company_id}
