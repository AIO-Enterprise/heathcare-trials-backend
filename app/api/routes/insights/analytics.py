"""
M7/M15: Analytics & Optimizer Routes
Owner: Backend Dev 2 / AI Dev
Dependencies: M1, M2, M7 (Optimizer Service)

GET  /analytics/{ad_id}                  — Performance data for an advertisement
POST /analytics/{ad_id}/optimize         — Trigger optimizer suggestions
POST /analytics/{ad_id}/regenerate-item  — Regenerate a single optimization item via its prompt
POST /analytics/{ad_id}/decision         — Human accepts/rejects optimizer suggestions
"""

import json
import logging
import traceback

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import extract_json, get_ai_client
from app.core.security import get_company_ad_or_404, get_current_user, require_roles
from app.db.database import async_session_factory, get_db
from app.models.models import AdAnalytics, Advertisement, OptimizerLog, Review, User, UserRole
from app.schemas.schemas import AnalyticsOut, OptimizerDecision, OptimizerSuggestion
from app.services.optimization.optimizer import OptimizerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Analytics & Optimization"])


@router.get("/{ad_id}", response_model=list[AnalyticsOut])
async def get_analytics(
    ad_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve performance metrics for an advertisement."""
    await get_company_ad_or_404(db, user, ad_id)
    result = await db.execute(
        select(AdAnalytics).where(AdAnalytics.advertisement_id == ad_id).order_by(AdAnalytics.recorded_at.desc())
    )
    return result.scalars().all()


async def _bg_run_optimizer(log_id: str, ad_id: str, company_id: str) -> None:
    """Background task: run optimizer, write results back to OptimizerLog."""
    from app.db.database import async_session_factory

    try:
        async with async_session_factory() as db:
            ad_result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
            ad = ad_result.scalar_one_or_none()
            if not ad:
                return

            analytics_result = await db.execute(select(AdAnalytics).where(AdAnalytics.advertisement_id == ad_id))
            analytics = analytics_result.scalars().all()

            review_result = await db.execute(select(Review).where(Review.advertisement_id == ad_id))
            reviews = review_result.scalars().all()

            optimizer = OptimizerService(db, company_id)
            suggestions = await optimizer.generate_suggestions(ad, analytics, reviews)

            log_result = await db.execute(select(OptimizerLog).where(OptimizerLog.id == log_id))
            log = log_result.scalar_one_or_none()
            if log:
                log.status = "done"
                log.suggestions = suggestions["suggestions"]
                log.context = suggestions.get("context")
            await db.commit()
    except Exception as exc:
        logger.error("Background optimizer failed for ad %s: %s\n%s", ad_id, exc, traceback.format_exc())
        try:
            from app.db.database import async_session_factory as _sf

            async with _sf() as db2:
                log_result = await db2.execute(select(OptimizerLog).where(OptimizerLog.id == log_id))
                log = log_result.scalar_one_or_none()
                if log:
                    log.status = "failed"
                    await db2.commit()
        except Exception:
            pass


@router.post("/{ad_id}/optimize")
async def trigger_optimization(
    ad_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER])),
):
    """
    Kick off async optimizer run. Returns immediately with {"log_id", "status": "pending"}.
    Poll GET /{ad_id}/optimize/status?log_id=<id> until status is "done" or "failed".
    """
    async with async_session_factory() as db:
        await get_company_ad_or_404(db, user, ad_id)

        log = OptimizerLog(
            advertisement_id=ad_id,
            status="pending",
            suggestions=None,
            context=None,
        )
        db.add(log)
        await db.flush()
        log_id = log.id
        await db.commit()

    background_tasks.add_task(_bg_run_optimizer, log_id, ad_id, user.company_id)
    return {"log_id": log_id, "status": "pending"}


@router.get("/{ad_id}/optimize/status", response_model=OptimizerSuggestion)
async def get_optimization_status(
    ad_id: str,
    log_id: str,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Poll for optimizer results. Returns status: pending | done | failed.
    When status is "done", suggestions and context are populated.
    """
    await get_company_ad_or_404(db, user, ad_id)
    result = await db.execute(
        select(OptimizerLog).where(
            OptimizerLog.id == log_id,
            OptimizerLog.advertisement_id == ad_id,
        )
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Optimizer log not found")

    return OptimizerSuggestion(
        advertisement_id=ad_id,
        suggestions=log.suggestions or {},
        context=log.context,
        status=log.status,
    )


class RegenerateItemRequest(BaseModel):
    prompt: str
    item_type: str | None = "general"  # "cost" | "website" | "advertisement"


@router.post("/{ad_id}/regenerate-item")
async def regenerate_optimizer_item(
    ad_id: str,
    body: RegenerateItemRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Regenerate a single optimization item using its stored AI prompt.
    Returns a new { what, why, prompt } object.
    """
    await get_company_ad_or_404(db, user, ad_id)

    ai = get_ai_client()
    if not ai.is_configured():
        return {
            "what": "Regenerated suggestion (AI not configured)",
            "why": f"Prompt used: {body.prompt[:120]}…",
            "prompt": body.prompt,
        }

    system_prompt = (
        "You are a marketing optimization AI. "
        "Given a prompt, return a single optimization suggestion as a JSON object with exactly three keys: "
        "'what' (the specific change to make), "
        "'why' (data-driven reason), and "
        "'prompt' (an improved self-contained prompt for future regeneration). "
        "Output ONLY a raw JSON object — no markdown, no code fences. "
        "Your entire response must start with { and end with }."
    )

    try:
        text = await ai.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": body.prompt}],
            max_tokens=800,
        )
    except Exception:
        logger.exception("Optimizer item regeneration failed for ad %s", ad_id)
        raise HTTPException(status_code=502, detail="AI regeneration failed — please retry")

    try:
        result = extract_json(text)
    except json.JSONDecodeError:
        logger.warning("Regenerated optimizer item returned non-JSON output")
        return {"what": text[:500], "why": "Raw AI response", "prompt": body.prompt}

    if not isinstance(result, dict):
        return {"what": text[:500], "why": "Raw AI response", "prompt": body.prompt}
    return {
        "what": result.get("what", ""),
        "why": result.get("why", ""),
        "prompt": result.get("prompt", body.prompt),
    }


@router.post("/{ad_id}/decision")
async def submit_optimizer_decision(
    ad_id: str,
    body: OptimizerDecision,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER, UserRole.PROJECT_MANAGER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Human-in-the-loop decision on optimizer suggestions.
    Accepts, rejects, or partially applies changes.
    """
    # Tenant scope — the log is only reachable through a company-owned campaign.
    await get_company_ad_or_404(db, user, ad_id)

    # Get latest optimizer log
    result = await db.execute(
        select(OptimizerLog).where(OptimizerLog.advertisement_id == ad_id).order_by(OptimizerLog.created_at.desc())
    )
    log = result.scalars().first()
    if not log:
        raise HTTPException(status_code=404, detail="No optimizer suggestions found")

    log.human_decision = body.decision
    log.applied_changes = body.applied_changes

    # If accepted, trigger reinforcement learning
    if body.decision in ("accepted", "partial"):
        from app.services.optimization.reinforcement import ReinforcementService

        rl_service = ReinforcementService(db, user.company_id)
        await rl_service.record_outcome(ad_id, log, body.decision)

    return {"detail": f"Decision '{body.decision}' recorded", "log_id": log.id}
