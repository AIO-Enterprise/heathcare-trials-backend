"""
Review workflow: submit for review, create/list reviews, publish, minor edits,
optimizer change approval/rejection.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.routes.campaigns.ads_generation import _bg_generate_creatives, _bg_submit_for_review
from app.core.media import media_url
from app.core.security import get_current_user, require_roles
from app.db.database import get_db
from app.models.models import AdStatus, Advertisement, Review, User, UserRole
from app.schemas.schemas import AdvertisementOut, BotConfigUpdate, MinorEditRequest, ReviewCreate, ReviewOut

router = APIRouter(prefix="/advertisements", tags=["Advertisement Reviews"])
logger = logging.getLogger(__name__)


@router.post("/{ad_id}/submit-for-review", response_model=AdvertisementOut)
async def submit_for_review(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Kick off async reviewer pre-analysis. Returns immediately."""
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    if ad.status != AdStatus.STRATEGY_CREATED:
        raise HTTPException(
            status_code=400,
            detail=f"Campaign can only be submitted for review from STRATEGY_CREATED, not '{ad.status.value}'",
        )

    background_tasks.add_task(_bg_submit_for_review, ad_id, user.company_id)
    return ad


@router.post("/{ad_id}/reviews", response_model=ReviewOut)
async def create_review(
    ad_id: str,
    body: ReviewCreate,
    user: User = Depends(require_roles([UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    ad_result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = ad_result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    review = Review(
        advertisement_id=ad_id,
        reviewer_id=user.id,
        review_type=body.review_type,
        status=body.status,
        comments=body.comments,
        suggestions=body.suggestions,
        edited_strategy=body.edited_strategy,
    )
    db.add(review)

    if body.status == "approved":
        if body.review_type == "strategy":
            if ad.status != AdStatus.UNDER_REVIEW:
                raise HTTPException(
                    status_code=400,
                    detail="This campaign is no longer waiting for strategy review. Please refresh and try again.",
                )
            ad.status = AdStatus.ETHICS_REVIEW
        elif body.review_type == "ethics":
            if ad.status != AdStatus.ETHICS_REVIEW:
                raise HTTPException(
                    status_code=400,
                    detail="This campaign is no longer waiting for ethics review. Please refresh and try again.",
                )
            ad.status = AdStatus.APPROVED
    elif body.status in ("revision", "rejected"):
        if ad.status not in (AdStatus.UNDER_REVIEW, AdStatus.ETHICS_REVIEW):
            raise HTTPException(
                status_code=400,
                detail="This campaign can't be sent back for changes from its current stage. Please refresh and try again.",
            )
        ad.status = AdStatus.UNDER_REVIEW

    await db.flush()
    return review


@router.get("/{ad_id}/reviews", response_model=list[ReviewOut])
async def list_reviews(
    ad_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Review)
        .join(Advertisement, Review.advertisement_id == Advertisement.id)
        .where(
            Review.advertisement_id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    return result.scalars().all()


@router.post("/{ad_id}/publish", response_model=AdvertisementOut)
async def publish_advertisement(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
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
    if ad.status != AdStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Advertisement must be approved before publishing")

    ethics_result = await db.execute(
        select(Review).where(
            Review.advertisement_id == ad_id,
            Review.review_type == "ethics",
            Review.status == "approved",
        )
    )
    if not ethics_result.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="Ethics review must be completed and approved before publishing",
        )

    ad.status = AdStatus.PUBLISHED
    return ad


@router.patch("/{ad_id}/bot-config", response_model=AdvertisementOut)
async def update_bot_config(
    ad_id: str,
    body: BotConfigUpdate,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
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

    merged = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
    merged.update(body.model_dump(exclude_unset=True))
    ad.bot_config = merged
    flag_modified(ad, "bot_config")
    await db.commit()
    return ad


@router.post("/{ad_id}/minor-edit", response_model=AdvertisementOut)
async def minor_edit_strategy(
    ad_id: str,
    body: MinorEditRequest,
    user: User = Depends(require_roles([UserRole.PUBLISHER, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Patch a single field in strategy_json and record an audit-trail system review.
    field is a dot-path, e.g. "executive_summary" or "messaging.core_message".
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
    if not ad.strategy_json:
        raise HTTPException(status_code=400, detail="No strategy to edit")

    strategy = dict(ad.strategy_json if isinstance(ad.strategy_json, dict) else {})
    keys = body.field.split(".")
    node = strategy
    for key in keys[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = body.new_value
    ad.strategy_json = strategy
    flag_modified(ad, "strategy_json")

    audit = Review(
        advertisement_id=ad_id,
        reviewer_id=user.id,
        review_type="system",
        status="pending",
        comments=f"{body.field} changed from '{body.old_value[:120]}' to '{body.new_value[:120]}'",
    )
    db.add(audit)

    creative_fields = {"caption", "content_note", "ad_caption", "hashtags"}
    if user.role == UserRole.PUBLISHER and body.field in creative_fields:
        db.add(
            Review(
                advertisement_id=ad_id,
                reviewer_id=user.id,
                review_type="optimizer",
                status="pending",
                comments=f"field:{body.field}",
                suggestions={"field": body.field, "new_value": body.new_value, "old_value": body.old_value},
            )
        )

    await db.flush()
    return ad


@router.post("/{ad_id}/optimizer-changes/approve")
async def approve_optimizer_changes(
    ad_id: str,
    body: dict,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Approve a set of optimizer changes for a campaign.
    Body: {"review_ids": ["id1", "id2", ...]}  — or omit to approve all pending.

    For each approved review, auto-deploys the change:
      - field update → already in strategy_json, no extra deploy needed
      - regenerate_website → re-hosts the already-generated HTML
      - regenerate_creative → triggers re-upload to Meta
    """
    from app.models.models import PlatformConnection

    review_ids = body.get("review_ids") or []

    ad_result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = ad_result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    q = select(Review).where(
        Review.advertisement_id == ad_id,
        Review.review_type == "optimizer",
        Review.status == "pending",
    )
    if review_ids:
        q = q.where(Review.id.in_(review_ids))
    reviews_result = await db.execute(q)
    pending = reviews_result.scalars().all()

    if not pending:
        raise HTTPException(status_code=404, detail="No pending optimizer changes found")

    deployed = []
    for review in pending:
        sugg_approve = review.suggestions if isinstance(review.suggestions, dict) else {}
        action = sugg_approve.get("action")
        field = sugg_approve.get("field")

        if action == "regenerate_website":
            if ad.output_url:
                ad.hosted_url = media_url(f"/api/advertisements/{ad_id}/website")
                deployed.append("website re-hosted")
            else:
                deployed.append("website: no generated file found")

        elif action == "regenerate_creative":
            bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
            campaign_id = bot.get("meta_campaign_id")
            if campaign_id and ad.output_files:
                conn_result = await db.execute(
                    select(PlatformConnection).where(
                        PlatformConnection.company_id == user.company_id,
                        PlatformConnection.platform == "meta",
                    )
                )
                conn = conn_result.scalar_one_or_none()
                if conn:
                    background_tasks.add_task(_bg_generate_creatives, ad_id, user.company_id)
                    deployed.append("creatives queued for regeneration")
            else:
                deployed.append("creatives: no Meta campaign or no output files")

        elif field:
            new_val = sugg_approve.get("new_value")
            if new_val is not None:
                strategy = dict(ad.strategy_json if isinstance(ad.strategy_json, dict) else {})
                keys = field.split(".")
                node = strategy
                for key in keys[:-1]:
                    node = node.setdefault(key, {})
                node[keys[-1]] = new_val
                ad.strategy_json = strategy
                flag_modified(ad, "strategy_json")
                deployed.append(f"field '{field}' confirmed in strategy")
            else:
                deployed.append(f"field '{field}' — no new_value in suggestion")

        review.status = "approved"

    await db.flush()
    return {"approved": len(pending), "deployed": deployed}


@router.post("/{ad_id}/optimizer-changes/reject")
async def reject_optimizer_changes(
    ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a set of optimizer changes. Marks reviews as 'rejected' and reverts
    the staged field back to old_value in strategy_json (if available).
    Body: {"review_ids": ["id1", ...]}  — or omit to reject all pending.
    """
    review_ids = body.get("review_ids") or []

    ad_result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = ad_result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    q = select(Review).where(
        Review.advertisement_id == ad_id,
        Review.review_type == "optimizer",
        Review.status == "pending",
    )
    if review_ids:
        q = q.where(Review.id.in_(review_ids))
    reviews_result = await db.execute(q)
    pending = reviews_result.scalars().all()

    if not pending:
        raise HTTPException(status_code=404, detail="No pending optimizer changes found")

    strategy = dict(ad.strategy_json if isinstance(ad.strategy_json, dict) else {})
    modified = False
    for review in pending:
        sugg = review.suggestions if isinstance(review.suggestions, dict) else {}
        field = sugg.get("field")
        old_value = sugg.get("old_value")
        if field and old_value is not None:
            keys = field.split(".")
            node = strategy
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = old_value
            modified = True
        review.status = "rejected"

    if modified:
        ad.strategy_json = strategy
        flag_modified(ad, "strategy_json")

    await db.flush()
    return {"rejected": len(pending)}
