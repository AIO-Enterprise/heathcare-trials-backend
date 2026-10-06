"""
AI generation endpoints: strategy, creatives, website, questionnaire, rewrite-strategy.
Background tasks are defined here and imported by other modules that need them.
"""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.exceptions import AppError
from app.core.media import media_to_disk, media_url
from app.core.security import require_roles
from app.db.database import get_db
from app.models.models import (
    AdStatus,
    Advertisement,
    AdvertisementDocument,
    BrandKit,
    Company,
    CompanyDocument,
    Review,
    User,
    UserRole,
)
from app.schemas.schemas import (
    AdvertisementOut,
    QuestionnaireUpdate,
    RewriteQuestionRequest,
    RewriteStrategyRequest,
)
from app.services.ai.curator import CuratorService
from app.services.ai.voicebot_agent import VoicebotAgentService

router = APIRouter(prefix="/advertisements", tags=["Advertisement Generation"])
logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _load_all_docs(db, ad_id: str, company_id: str):
    company_docs_result = await db.execute(select(CompanyDocument).where(CompanyDocument.company_id == company_id))
    protocol_docs_result = await db.execute(
        select(AdvertisementDocument).where(
            AdvertisementDocument.advertisement_id == ad_id,
            AdvertisementDocument.company_id == company_id,
        )
    )
    return sorted(
        list(company_docs_result.scalars().all()) + list(protocol_docs_result.scalars().all()),
        key=lambda d: d.priority,
        reverse=True,
    )


# ── Background tasks (imported by ads_review.py for optimizer approval) ──────


async def _bg_generate_strategy(ad_id: str, company_id: str) -> None:
    """Background task: run Curator + Questionnaire, update ad when done."""
    from app.db.database import async_session_factory

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return

            all_docs = await _load_all_docs(db, ad_id, company_id)
            curator = CuratorService(db, company_id)
            strategy, questionnaire = await asyncio.gather(
                curator.generate_strategy(ad, all_docs),
                curator.generate_questionnaire(ad, all_docs),
            )

            if strategy.get("parse_error"):
                logger.error("Strategy parse error for ad %s: %s", ad_id, strategy.get("raw_response", "")[:200])
                ad.status = AdStatus.DRAFT
                ad.generation_error = "AI strategy output could not be parsed — please retry generation."
                await db.commit()
                return

            ad.strategy_json = strategy
            ad.questionnaire = questionnaire
            ad.generation_error = None
            ad.status = AdStatus.STRATEGY_CREATED
            flag_modified(ad, "strategy_json")
            flag_modified(ad, "questionnaire")

            if "voicebot" in (ad.ad_type if isinstance(ad.ad_type, list) else []):
                await db.flush()
                try:
                    vb_svc = VoicebotAgentService(db)
                    rec = await vb_svc.recommend_voice(ad_id)
                    cfg = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
                    cfg.setdefault("voice_id", rec["voice_id"])
                    cfg.setdefault("conversation_style", rec["conversation_style"])
                    cfg.setdefault("first_message", rec["first_message"])
                    cfg["_voice_rec"] = {"voice_name": rec["voice_name"], "reason": rec["reason"]}
                    ad.bot_config = cfg
                    flag_modified(ad, "bot_config")
                except Exception:
                    logger.warning(
                        "Voice recommendation failed for ad %s — continuing without it", ad_id, exc_info=True
                    )

            await db.commit()
    except Exception as e:
        logger.error("Background strategy generation failed for ad %s: %s", ad_id, e, exc_info=True)
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                result = await db2.execute(select(Advertisement).where(Advertisement.id == ad_id))
                ad = result.scalar_one_or_none()
                if ad:
                    ad.generation_error = f"Strategy generation failed: {e}"
                    if ad.status == AdStatus.GENERATING:
                        ad.status = AdStatus.DRAFT
                    await db2.commit()
        except Exception:
            logger.exception("Failed to persist generation error state for ad %s", ad_id)


async def _bg_generate_creatives(ad_id: str, company_id: str) -> None:
    """Background task: generate ad creatives."""
    from app.db.database import async_session_factory
    from app.services.ai.creative import CreativeService

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return
            svc = CreativeService(company_id=company_id)
            creatives = await svc.generate_creatives(ad)
            ad.output_files = creatives
            ad.generation_error = None
            flag_modified(ad, "output_files")
            await db.commit()
    except Exception as e:
        logger.error("Background creative generation failed for ad %s: %s", ad_id, e, exc_info=True)
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                result = await db2.execute(select(Advertisement).where(Advertisement.id == ad_id))
                ad = result.scalar_one_or_none()
                if ad:
                    ad.generation_error = f"Creative generation failed: {e}"
                    await db2.commit()
        except Exception:
            logger.exception("Failed to persist generation error state for ad %s", ad_id)


async def _bg_generate_shorts(ad_id: str, company_id: str) -> None:
    """Background task: generate short video creatives from existing PNG creatives."""
    from datetime import datetime

    from app.db.database import async_session_factory
    from app.services.ai.creative import CreativeService

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return
            svc = CreativeService(company_id=company_id)
            shorts = await svc.generate_shorts(ad)
            ad.shorts_files = shorts
            ad.generation_error = None
            ad.updated_at = datetime.now(UTC).replace(tzinfo=None)
            flag_modified(ad, "shorts_files")
            await db.commit()
    except Exception as e:
        logger.error("Background shorts generation failed for ad %s: %s", ad_id, e, exc_info=True)
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                result = await db2.execute(select(Advertisement).where(Advertisement.id == ad_id))
                ad = result.scalar_one_or_none()
                if ad:
                    ad.generation_error = f"Short-video generation failed: {e}"
                    await db2.commit()
        except Exception:
            logger.exception("Failed to persist generation error state for ad %s", ad_id)


async def _bg_submit_for_review(ad_id: str, company_id: str) -> None:
    """Background task: run Reviewer pre-analysis and move ad to UNDER_REVIEW."""
    from app.db.database import async_session_factory
    from app.services.ai.reviewer import ReviewerService

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return
            reviewer_svc = ReviewerService(db, company_id)
            review_output = await reviewer_svc.pre_review(ad)

            # Fail closed: a parse error means we have no trustworthy
            # website_requirements / ad_details. Storing garbage (or the raw LLM
            # text) and advancing to UNDER_REVIEW would poison ethics review.
            if review_output.get("parse_error"):
                logger.error(
                    "Reviewer returned unparseable output for ad %s — leaving in %s for retry",
                    ad_id,
                    ad.status.value,
                )
                return

            ad.website_reqs = review_output.get("website_requirements")
            ad.ad_details = review_output.get("ad_details")
            flag_modified(ad, "website_reqs")
            flag_modified(ad, "ad_details")
            if ad.status != AdStatus.STRATEGY_CREATED:
                logger.warning(
                    "Skipping UNDER_REVIEW transition for ad %s — state is '%s', expected STRATEGY_CREATED",
                    ad_id,
                    ad.status.value,
                )
                await db.commit()
                return
            ad.status = AdStatus.UNDER_REVIEW
            await db.commit()
    except Exception as e:
        logger.error("Background submit-for-review failed for ad %s: %s", ad_id, e, exc_info=True)


async def _bg_generate_website(ad_id: str, company_id: str) -> None:
    """Background task: generate landing page website."""
    from app.db.database import async_session_factory
    from app.services.ai.website_agent import WebsiteAgentService

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return
            brand_kit_result = await db.execute(select(BrandKit).where(BrandKit.company_id == company_id))
            brand_kit = brand_kit_result.scalar_one_or_none()
            company_result = await db.execute(select(Company).where(Company.id == company_id))
            company = company_result.scalar_one_or_none()
            svc = WebsiteAgentService(company_id=company_id)
            url = await svc.generate_website(ad, brand_kit, company)
            ad.output_url = url
            # Force updated_at so the frontend poll detects the task finished even
            # when the deterministic URL value hasn't changed.
            ad.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
    except Exception as e:
        logger.error("Background website generation failed for ad %s: %s", ad_id, e, exc_info=True)
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                r2 = await db2.execute(select(Advertisement).where(Advertisement.id == ad_id))
                ad2 = r2.scalar_one_or_none()
                if ad2:
                    ad2.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    await db2.commit()
        except Exception as touch_err:
            logger.error("Failed to touch updated_at after website gen failure for ad %s: %s", ad_id, touch_err)


async def _bg_rewrite_strategy(
    ad_id: str,
    company_id: str,
    reviewer_id: str,
    instructions: str,
    restore_status: AdStatus,
) -> None:
    """
    Background task: rewrite strategy with Curator, then restore the campaign to
    exactly the status it had before the rewrite.
    """
    from app.db.database import async_session_factory

    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = result.scalar_one_or_none()
            if not ad:
                return

            all_docs = await _load_all_docs(db, ad_id, company_id)
            curator = CuratorService(db, company_id)
            strategy = await curator.generate_strategy(ad, all_docs, extra_instructions=instructions)
            ad.strategy_json = strategy
            flag_modified(ad, "strategy_json")

            preview = instructions[:120] + ("…" if len(instructions) > 120 else "")
            db.add(
                Review(
                    advertisement_id=ad_id,
                    reviewer_id=reviewer_id,
                    review_type="system",
                    status="pending",
                    comments=f"AI Re-Strategy completed: '{preview}'",
                )
            )

            ad.status = restore_status
            await db.commit()
    except Exception as e:
        logger.error("Background rewrite-strategy failed for ad %s: %s", ad_id, e, exc_info=True)
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                result = await db2.execute(select(Advertisement).where(Advertisement.id == ad_id))
                ad = result.scalar_one_or_none()
                if ad and ad.status == AdStatus.OPTIMIZING:
                    ad.status = restore_status
                    await db2.commit()
        except Exception:
            logger.exception("Failed to persist generation error state for ad %s", ad_id)


# ── Questionnaire ─────────────────────────────────────────────────────────────


@router.post("/{ad_id}/generate-questionnaire", response_model=AdvertisementOut)
async def generate_questionnaire(
    ad_id: str,
    user: User = Depends(
        require_roles([UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])
    ),
    db: AsyncSession = Depends(get_db),
):
    """Use Claude to auto-generate an MCQ eligibility questionnaire from campaign context."""
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    all_docs = await _load_all_docs(db, ad_id, user.company_id)
    curator = CuratorService(db, user.company_id)
    try:
        questionnaire = await curator.generate_questionnaire(ad, all_docs)
    except AppError:
        raise
    except Exception as e:
        logger.error("Questionnaire generation failed for ad %s: %s", ad_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Questionnaire generation failed")

    ad.questionnaire = questionnaire
    flag_modified(ad, "questionnaire")
    return ad


@router.patch("/{ad_id}/questionnaire", response_model=AdvertisementOut)
async def update_questionnaire(
    ad_id: str,
    body: QuestionnaireUpdate,
    user: User = Depends(
        require_roles([UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])
    ),
    db: AsyncSession = Depends(get_db),
):
    """Save or replace the questionnaire for a campaign."""
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")
    ad.questionnaire = body.questionnaire
    flag_modified(ad, "questionnaire")
    return ad


@router.post("/{ad_id}/questionnaire/rewrite-question")
async def rewrite_question(
    ad_id: str,
    body: RewriteQuestionRequest,
    user: User = Depends(
        require_roles([UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])
    ),
    db: AsyncSession = Depends(get_db),
):
    """Rewrite a single MCQ question using Claude based on a user instruction."""
    curator = CuratorService(db, user.company_id)
    updated = await curator.rewrite_question(body.question, body.instruction)
    return {"question": updated}


# ── Strategy generation ───────────────────────────────────────────────────────


@router.post("/{ad_id}/generate-strategy", response_model=AdvertisementOut)
async def generate_strategy(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off async strategy generation. Returns immediately with status=generating.
    Frontend polls GET /{ad_id} until status != generating.
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

    if ad.status not in (AdStatus.DRAFT, AdStatus.STRATEGY_CREATED):
        raise HTTPException(
            status_code=400,
            detail=f"Strategy can only be generated from DRAFT or STRATEGY_CREATED, not '{ad.status.value}'",
        )

    ad.status = AdStatus.GENERATING
    await db.flush()
    background_tasks.add_task(_bg_generate_strategy, ad_id, user.company_id)
    return ad


# ── Creative generation ───────────────────────────────────────────────────────


@router.post("/{ad_id}/generate-creatives", response_model=AdvertisementOut)
async def generate_creatives(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(
        require_roles(
            [UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER]
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off async creative generation. Returns immediately.
    Frontend polls GET /{ad_id} for output_files to appear.
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
    if ad.status not in (AdStatus.APPROVED, AdStatus.PUBLISHED, AdStatus.UNDER_REVIEW, AdStatus.STRATEGY_CREATED):
        raise HTTPException(
            status_code=400,
            detail="Creatives can only be generated after strategy creation.",
        )

    background_tasks.add_task(_bg_generate_creatives, ad_id, user.company_id)

    if user.role == UserRole.PUBLISHER:
        db.add(
            Review(
                advertisement_id=ad_id,
                reviewer_id=user.id,
                review_type="optimizer",
                status="pending",
                comments="Optimizer regenerated ad creatives.",
                suggestions={"action": "regenerate_creative"},
            )
        )
        await db.flush()

    return ad


# ── Shorts generation ─────────────────────────────────────────────────────────


@router.post("/{ad_id}/generate-shorts", response_model=AdvertisementOut)
async def generate_shorts(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(
        require_roles(
            [UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER]
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off async short-video generation from existing PNG creatives.
    Returns immediately; frontend polls GET /{ad_id} for shorts_files.
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
    if not ad.output_files:
        raise HTTPException(
            status_code=400,
            detail="Generate ad creatives first before generating shorts.",
        )

    png_sources = [c for c in ad.output_files if c.get("image_url") and not c["image_url"].endswith(".mp4")]
    if not png_sources:
        raise HTTPException(
            status_code=400,
            detail="No ad images found. Regenerate ad creatives with image generation enabled first.",
        )

    background_tasks.add_task(_bg_generate_shorts, ad_id, user.company_id)
    return ad


# ── Website generation ────────────────────────────────────────────────────────


@router.post("/{ad_id}/generate-website", response_model=AdvertisementOut)
async def generate_website(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(
        require_roles(
            [UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER]
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off async website generation. Returns immediately.
    Frontend polls GET /{ad_id} for output_url to appear.
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
    if ad.status not in (AdStatus.APPROVED, AdStatus.PUBLISHED, AdStatus.UNDER_REVIEW, AdStatus.STRATEGY_CREATED):
        raise HTTPException(
            status_code=400,
            detail="Website can only be generated after strategy creation.",
        )

    background_tasks.add_task(_bg_generate_website, ad_id, user.company_id)

    if user.role == UserRole.PUBLISHER:
        db.add(
            Review(
                advertisement_id=ad_id,
                reviewer_id=user.id,
                review_type="optimizer",
                status="pending",
                comments="Optimizer regenerated website content.",
                suggestions={"action": "regenerate_website"},
            )
        )
        await db.flush()

    return ad


@router.get("/{ad_id}/website")
async def serve_website(
    ad_id: str,
    download: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """
    Serve the generated HTML landing page — public, no auth required.
    ?download=true → Content-Disposition: attachment (triggers browser download).
    """
    import os as _os

    from app.core.config import settings as _s

    result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")
    if not ad.output_url:
        raise HTTPException(status_code=404, detail="No website has been generated for this campaign yet")

    relative = media_to_disk(ad.output_url)
    disk_path = _os.path.join(_s.OUTPUT_DIR, relative)

    if not _os.path.exists(disk_path):
        raise HTTPException(status_code=404, detail="Website file not found on disk")

    with open(disk_path, encoding="utf-8") as f:
        html_content = f.read()

    if download:
        return Response(
            content=html_content,
            media_type="text/html",
            headers={"Content-Disposition": 'attachment; filename="landing-page.html"'},
        )
    return HTMLResponse(content=html_content)


@router.post("/{ad_id}/host-page", response_model=AdvertisementOut)
async def host_page(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish the landing page by pointing hosted_url at the API endpoint that reads
    directly from persistent storage (EFS-backed outputs/). No file copy needed.
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
    if not ad.output_url:
        raise HTTPException(
            status_code=400,
            detail="Landing page has not been generated yet. Generate the website first.",
        )

    ad.hosted_url = media_url(f"/api/advertisements/{ad_id}/website")
    await db.commit()
    await db.refresh(ad)
    return ad


# ── Strategy rewrite ──────────────────────────────────────────────────────────


@router.post("/{ad_id}/rewrite-strategy", response_model=AdvertisementOut)
async def rewrite_strategy(
    ad_id: str,
    body: RewriteStrategyRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.PROJECT_MANAGER, UserRole.ETHICS_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off async strategy rewrite. Returns immediately with status=optimizing.
    Frontend polls GET /{ad_id} until status returns to its previous value.
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

    _rewritable = (AdStatus.STRATEGY_CREATED, AdStatus.UNDER_REVIEW, AdStatus.ETHICS_REVIEW)
    if ad.status not in _rewritable:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy can only be rewritten from STRATEGY_CREATED, UNDER_REVIEW, or ETHICS_REVIEW, not '{ad.status.value}'",
        )

    background_tasks.add_task(
        _bg_rewrite_strategy,
        ad_id,
        user.company_id,
        user.id,
        body.instructions,
        ad.status,
    )
    return ad


# ── Schedule suggestions ──────────────────────────────────────────────────────


@router.get("/{ad_id}/schedule-suggestions")
async def get_schedule_suggestions(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Use Claude to suggest optimal ad scheduling based on campaign strategy,
    target audience, and available Meta performance data.
    """
    import json as _json

    from app.core.llm import get_ai_client
    from app.models.models import AdAnalytics as _AdAnalytics

    ad_result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    ad = ad_result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Advertisement not found")

    analytics_result = await db.execute(
        select(_AdAnalytics)
        .where(
            _AdAnalytics.advertisement_id == ad_id,
            _AdAnalytics.source == "meta",
        )
        .order_by(_AdAnalytics.recorded_at.desc())
        .limit(10)
    )
    analytics = analytics_result.scalars().all()

    strategy = ad.strategy_json if isinstance(ad.strategy_json, dict) else {}
    kpis = strategy.get("kpis", [])
    audience = ad.target_audience if isinstance(ad.target_audience, dict) else {}
    recent_perf = [
        {
            "date": a.date_label,
            "impressions": a.impressions,
            "clicks": a.views,
            "ctr": a.click_rate,
            "spend": a.spend,
        }
        for a in analytics
    ]

    prompt = f"""You are a Meta advertising strategy expert specialising in healthcare clinical trial recruitment.

Campaign: {ad.title}
Target Audience: {_json.dumps(audience)}
Strategy KPIs: {_json.dumps(kpis)}
Trial Duration: {ad.duration or "not set"}
Budget: ${ad.budget or "not set"}/total

Recent performance (last {len(recent_perf)} days of Meta data):
{_json.dumps(recent_perf, indent=2) if recent_perf else "No Meta data available yet — use healthcare audience best practices."}

Based on this context, provide concrete scheduling recommendations in JSON with this structure:
{{
  "best_days": ["Monday", "Tuesday", ...],
  "best_hours": ["9am-11am", "7pm-9pm", ...],
  "pause_periods": ["Saturday afternoon", "Sunday morning", ...],
  "budget_pacing": "Front-load budget in first 2 weeks to build audience momentum",
  "headline_tips": ["Use urgency language Mon-Wed", "..."],
  "reasoning": "2-3 sentence explanation",
  "confidence": "high|medium|low"
}}

Return ONLY valid JSON, no markdown or explanation."""

    client = get_ai_client()
    try:
        raw = await client.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        suggestions = _json.loads(raw.strip())
    except Exception as exc:
        logger.warning("Schedule suggestions Claude call failed: %s", exc)
        suggestions = {
            "best_days": ["Monday", "Tuesday", "Wednesday", "Thursday"],
            "best_hours": ["9am–11am", "12pm–1pm", "7pm–9pm"],
            "pause_periods": ["Saturday night", "Sunday morning"],
            "budget_pacing": "Distribute budget evenly across weekdays; reduce by 40% on weekends.",
            "headline_tips": ["Lead with patient benefit on Mon–Wed", "Use urgency copy Thu–Fri"],
            "reasoning": "Healthcare audiences engage highest on weekday mornings and evenings. Weekend engagement drops significantly for clinical trial ads.",
            "confidence": "medium",
        }

    return {"ad_id": ad_id, "suggestions": suggestions}
