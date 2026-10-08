import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, require_roles
from app.db.database import get_db
from app.models.models import AdStatus, Advertisement, Review, User, UserRole
from app.schemas.schemas import AdvertisementCreate, AdvertisementOut, AdvertisementUpdate
from app.services.ai.voicebot_agent import VoicebotAgentService
from app.services.storage import file_storage

router = APIRouter(prefix="/advertisements", tags=["Advertisements"])
logger = logging.getLogger(__name__)


@router.post("/", response_model=AdvertisementOut)
async def create_advertisement(
    body: AdvertisementCreate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    # Build booking_config with guaranteed defaults so downstream logic
    # never needs to guess. Coordinator can override later via PATCH.
    raw_bc = body.booking_config.model_dump() if body.booking_config else {}
    booking_config = {
        "slot_duration_minutes": raw_bc.get("slot_duration_minutes") or 30,
        "max_per_slot": raw_bc.get("max_per_slot") or 3,
    }

    ad = Advertisement(
        company_id=user.company_id,
        title=body.title,
        ad_type=body.ad_type,
        campaign_category=body.campaign_category,
        budget=body.budget,
        duration=body.duration,
        platforms=body.platforms,
        target_audience=body.target_audience,
        trial_location=body.trial_location,
        patients_required=body.patients_required,
        trial_start_date=body.trial_start_date,
        trial_end_date=body.trial_end_date,
        special_instructions=body.special_instructions,
        booking_config=booking_config,
        status=AdStatus.DRAFT,
    )
    db.add(ad)
    await db.flush()
    return ad


@router.get("/", response_model=list[AdvertisementOut])
async def list_advertisements(
    status: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Advertisement).where(Advertisement.company_id == user.company_id)
    if status:
        query = query.where(Advertisement.status == status)
    result = await db.execute(query.order_by(Advertisement.created_at.desc()))
    return result.scalars().all()


@router.get("/optimizer-changes")
async def list_optimizer_changes(
    user: User = Depends(require_roles([UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """Return all pending optimizer-change reviews grouped by advertisement."""
    result = await db.execute(
        select(Review)
        .join(Advertisement, Review.advertisement_id == Advertisement.id)
        .where(
            Advertisement.company_id == user.company_id,
            Review.review_type == "optimizer",
            Review.status == "pending",
        )
        .order_by(Review.advertisement_id)
    )
    reviews = result.scalars().all()

    ad_ids = list({r.advertisement_id for r in reviews})
    ads_result = await db.execute(select(Advertisement).where(Advertisement.id.in_(ad_ids)))
    ad_map = {a.id: a for a in ads_result.scalars().all()}

    grouped = {}
    for r in reviews:
        ad = ad_map.get(r.advertisement_id)
        if r.advertisement_id not in grouped:
            grouped[r.advertisement_id] = {
                "ad_id": r.advertisement_id,
                "ad_title": ad.title if ad else r.advertisement_id,
                "changes": [],
            }
        grouped[r.advertisement_id]["changes"].append(
            {
                "review_id": r.id,
                "comments": r.comments,
                "suggestions": r.suggestions or {},
                "created_at": r.created_at.isoformat() if hasattr(r, "created_at") and r.created_at else None,
            }
        )

    return list(grouped.values())


@router.get("/{ad_id}", response_model=AdvertisementOut)
async def get_advertisement(
    ad_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")
    return ad


@router.patch("/{ad_id}", response_model=AdvertisementOut)
async def update_advertisement(
    ad_id: str,
    body: AdvertisementUpdate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    updates = body.model_dump(exclude_unset=True)

    # Merge booking_config patch with existing values and enforce defaults
    if "booking_config" in updates:
        existing_bc = ad.booking_config if isinstance(ad.booking_config, dict) else {}
        merged = {**existing_bc, **(updates["booking_config"] or {})}
        merged.setdefault("slot_duration_minutes", 30)
        merged.setdefault("max_per_slot", 3)
        updates["booking_config"] = merged

    for field, value in updates.items():
        setattr(ad, field, value)
    return ad


@router.delete("/{ad_id}", status_code=204)
async def delete_advertisement(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently delete a campaign and all its associated data:
      - Database record (cascades to reviews, documents, etc.)
      - ElevenLabs voice agent (if provisioned)
      - Generated output files on disk  (outputs/<company_id>/<ad_id>/)
      - Uploaded protocol documents     (uploads/docs/<company_id>/<ad_id>/)

    Note: externally-deployed websites (Vercel, Netlify, etc.) cannot be
    torn down automatically because deployment project IDs are not stored.
    The Publisher must manually remove those deployments from the platform.
    """
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    bot_config = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    if bot_config.get("elevenlabs_agent_id"):
        try:
            svc = VoicebotAgentService(db)
            await svc.delete_agent(ad_id)
            logger.info("Deleted ElevenLabs agent for ad %s", ad_id)
        except Exception as exc:
            logger.warning("Could not delete ElevenLabs agent for ad %s: %s", ad_id, exc)

    outputs_prefix = f"outputs/{user.company_id}/{ad_id}"
    try:
        file_storage.delete_prefix(outputs_prefix)
        logger.info("Deleted output files at %s", outputs_prefix)
    except Exception as exc:
        logger.warning("Could not delete output files for ad %s: %s", ad_id, exc)

    docs_prefix = f"uploads/docs/{user.company_id}/{ad_id}"
    try:
        file_storage.delete_prefix(docs_prefix)
        logger.info("Deleted protocol docs at %s", docs_prefix)
    except Exception as exc:
        logger.warning("Could not delete protocol docs for ad %s: %s", ad_id, exc)

    await db.delete(ad)
    await db.commit()
