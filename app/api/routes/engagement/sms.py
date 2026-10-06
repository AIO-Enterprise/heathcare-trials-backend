"""
SMS routes — one-off sends and bulk SMS campaigns.

Bulk campaigns mirror the voice calling campaigns (CSV/XLSX upload, queued →
running → done lifecycle, paced background worker). File parsing and phone
validation are reused from ads_voice to avoid duplication.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the battle-tested upload parser + phone validator from the voice routes.
from app.api.routes.engagement.ads_voice import _clean_phone, _parse_upload_rows
from app.core.config import settings
from app.core.security import require_roles
from app.db.database import get_db
from app.models.models import User, UserRole
from app.models.sms import SmsCampaign, SmsMessage, SmsRecord
from app.models.survey import Appointment
from app.services.messaging import sms_service
from app.services.messaging.workers.sms_worker import start_campaign

router = APIRouter(prefix="/sms", tags=["SMS"])
logger = logging.getLogger(__name__)

_CAMPAIGN_ROLES = [UserRole.PUBLISHER, UserRole.PROJECT_MANAGER]


def _scope_campaign_stmt(stmt, user: User):
    """Tenant-scope a SmsCampaign query. SUPER_USER sees all tenants."""
    if user.role == UserRole.SUPER_USER:
        return stmt
    return stmt.where(SmsCampaign.company_id == user.company_id)


async def _get_scoped_campaign(db: AsyncSession, user: User, campaign_id: str) -> SmsCampaign:
    stmt = _scope_campaign_stmt(select(SmsCampaign).where(SmsCampaign.id == campaign_id), user)
    campaign = (await db.execute(stmt)).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _campaign_dict(c: SmsCampaign) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "status": c.status,
        "total": c.total,
        "completed": c.completed,
        "failed_count": c.failed_count,
        "per_minute": c.per_minute,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
    }


# ── One-off send ────────────────────────────────────────────────────────────


class SmsSendRequest(BaseModel):
    to: str
    body: str
    as_whatsapp: bool = False

    @field_validator("to")
    @classmethod
    def _validate_to(cls, v: str) -> str:
        cleaned = _clean_phone(v)
        if not cleaned:
            raise ValueError("'to' must be a valid E.164 phone number (e.g. +1...)")
        return cleaned

    @field_validator("body")
    @classmethod
    def _validate_body(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be empty")
        return v


@router.post("/send")
async def send_one(
    body: SmsSendRequest,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Send a single SMS (or WhatsApp message) and log it."""
    record = await sms_service.send_and_log(
        db,
        body.to,
        body.body,
        kind="manual",
        company_id=user.company_id,
        as_whatsapp=body.as_whatsapp,
    )
    if record.status == "failed":
        raise HTTPException(status_code=502, detail="SMS send failed — see server logs")
    return {"status": record.status, "sid": record.provider_sid, "to": body.to}


# ── Status & activity log ─────────────────────────────────────────────────────


@router.get("/status")
async def sms_status(user: User = Depends(require_roles(_CAMPAIGN_ROLES))):
    """Report whether real sending is configured (else dev-mode console logging)."""
    return {
        "configured": bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_FROM_NUMBER),
        "from_number": settings.TWILIO_FROM_NUMBER or None,
        "whatsapp_configured": bool(settings.TWILIO_WHATSAPP_FROM),
        "reminder_hours_before": settings.SMS_REMINDER_HOURS_BEFORE,
    }


@router.get("/messages")
async def list_messages(
    advertisement_id: str | None = None,
    limit: int = 50,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Recent SMS activity (confirmations, reminders, cancellations, manual sends).

    When advertisement_id is given, scopes to messages tied to that campaign's
    appointments (i.e. its confirmations / reminders / cancellations).
    """
    limit = max(1, min(limit, 200))
    # Tenant scope — messages are stamped with company_id at send time; legacy
    # rows (NULL company) are only visible to super_users.
    stmt = select(SmsMessage).order_by(SmsMessage.created_at.desc()).limit(limit)
    if advertisement_id:
        stmt = stmt.where(
            SmsMessage.appointment_id.in_(
                select(Appointment.id).where(Appointment.advertisement_id == advertisement_id)
            )
        )
    if user.role != UserRole.SUPER_USER:
        stmt = stmt.where(SmsMessage.company_id == user.company_id)
    result = await db.execute(stmt)
    return [
        {
            "id": m.id,
            "to": m.to_number,
            "body": m.body,
            "kind": m.kind,
            "status": m.status,
            "error": m.error,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in result.scalars().all()
    ]


# ── Bulk campaigns ──────────────────────────────────────────────────────────


@router.post("/campaigns")
async def create_campaign(
    name: str = Form(...),
    body_template: str = Form(...),
    per_minute: int = Form(60),
    advertisement_id: str | None = Form(None),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a CSV/XLSX/XLS of phone numbers and create a bulk SMS campaign.
    File must have a 'phone' column (E.164). Optional 'name' column is
    substituted into the message via the {name} placeholder.
    Campaign starts 'queued' — call /start to begin sending.
    """
    if not body_template.strip():
        raise HTTPException(status_code=400, detail="body_template must not be empty")
    if not (1 <= per_minute <= 120):
        raise HTTPException(status_code=400, detail="per_minute must be 1–120")

    # Tenant scope — reject an advertisement_id belonging to another company.
    ad_company_id = user.company_id
    if advertisement_id:
        from app.core.security import get_company_ad_or_404

        await get_company_ad_or_404(db, user, advertisement_id)

    fname = file.filename or ""
    if not any(fname.lower().endswith(ext) for ext in (".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="File must be a .csv, .xlsx, or .xls file")

    content = await file.read()
    records_data, skipped = _parse_upload_rows(content, fname)
    if not records_data:
        raise HTTPException(status_code=400, detail="No valid E.164 phone numbers found in file")

    campaign = SmsCampaign(
        company_id=ad_company_id,
        advertisement_id=advertisement_id,
        name=name.strip(),
        body_template=body_template,
        status="queued",
        total=len(records_data),
        per_minute=per_minute,
    )
    db.add(campaign)
    await db.flush()

    for phone, contact_name in records_data:
        db.add(
            SmsRecord(
                campaign_id=campaign.id,
                phone_e164=phone,
                contact_name=contact_name,
            )
        )
    await db.commit()
    await db.refresh(campaign)

    return {
        "campaign": _campaign_dict(campaign),
        "skipped": skipped,
        "message": f"Campaign created with {len(records_data)} numbers ({skipped} skipped).",
    }


@router.get("/campaigns")
async def list_campaigns(
    advertisement_id: str | None = None,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(SmsCampaign).order_by(SmsCampaign.created_at.desc())
    if advertisement_id:
        stmt = stmt.where(SmsCampaign.advertisement_id == advertisement_id)
    stmt = _scope_campaign_stmt(stmt, user)
    result = await db.execute(stmt)
    return [_campaign_dict(c) for c in result.scalars().all()]


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: str,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_scoped_campaign(db, user, campaign_id)
    recs = await db.execute(
        select(SmsRecord).where(SmsRecord.campaign_id == campaign_id).order_by(SmsRecord.created_at)
    )
    return {
        "campaign": _campaign_dict(campaign),
        "records": [
            {
                "phone": r.phone_e164,
                "name": r.contact_name,
                "status": r.status,
                "error": r.last_error,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in recs.scalars().all()
        ],
    }


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign_route(
    campaign_id: str,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Start or resume a queued/paused campaign."""
    campaign = await _get_scoped_campaign(db, user, campaign_id)
    if campaign.status not in ("queued", "paused"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start a campaign with status '{campaign.status}'",
        )
    campaign.status = "running"
    campaign.started_at = campaign.started_at or datetime.utcnow()
    await db.commit()
    start_campaign(campaign_id)
    return {"status": "running", "campaign_id": campaign_id}


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign_route(
    campaign_id: str,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_scoped_campaign(db, user, campaign_id)
    if campaign.status == "running":
        campaign.status = "paused"
        await db.commit()
    return {"status": campaign.status, "campaign_id": campaign_id}


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign_route(
    campaign_id: str,
    user: User = Depends(require_roles(_CAMPAIGN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_scoped_campaign(db, user, campaign_id)
    if campaign.status in ("done", "failed", "cancelled"):
        return {"status": campaign.status, "campaign_id": campaign_id}
    campaign.status = "cancelled"
    campaign.finished_at = datetime.utcnow()
    await db.commit()
    return {"status": "cancelled", "campaign_id": campaign_id}
