"""
Conversation Analysis Routes
POST /advertisements/{ad_id}/survey-responses/{response_id}/analyze
  — Analyzes all voice sessions linked to a participant and returns structured insights.

POST /advertisements/{ad_id}/voice-sessions/{session_id}/analyze
  — Analyzes a single voice session.

POST /advertisements/{ad_id}/chat-sessions/{session_id}/analyze
  — Analyzes a single chatbot chat session.

GET  /advertisements/{ad_id}/chat-sessions
  — Lists all chat sessions for a campaign (study coordinator).

All write endpoints require STUDY_COORDINATOR or PROJECT_MANAGER role.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.security import require_roles
from app.db.database import get_db
from app.models.models import Advertisement, ChatSession, SurveyResponse, User, UserRole, VoiceSession
from app.services.ai.conversation_analyzer import ConversationAnalysisService

router = APIRouter(prefix="/advertisements", tags=["Conversation Analysis"])
logger = logging.getLogger(__name__)

_COORDINATOR_ROLES = [UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER]


# ── Analyze all voice sessions for a participant ───────────────────────────────


@router.post("/{ad_id}/survey-responses/{response_id}/analyze")
async def analyze_participant_conversations(
    ad_id: str,
    response_id: str,
    user: User = Depends(require_roles(_COORDINATOR_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await _assert_campaign_ownership(ad_id, user, db)

    result = await db.execute(
        select(SurveyResponse).where(
            SurveyResponse.id == response_id,
            SurveyResponse.advertisement_id == ad_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Participant not found")

    svc = ConversationAnalysisService(db)
    try:
        analyses = await svc.analyze_participant(response_id)
    except AppError:
        raise
    except Exception:
        logger.exception("Participant analysis failed for %s", response_id)
        raise HTTPException(status_code=500, detail="Analysis failed")

    return {"analyses": analyses, "count": len(analyses)}


# ── Analyze a single voice session ────────────────────────────────────────────


@router.post("/{ad_id}/voice-sessions/{session_id}/analyze")
async def analyze_voice_session(
    ad_id: str,
    session_id: str,
    user: User = Depends(require_roles(_COORDINATOR_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await _assert_campaign_ownership(ad_id, user, db)

    result = await db.execute(
        select(VoiceSession).where(
            VoiceSession.id == session_id,
            VoiceSession.advertisement_id == ad_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Voice session not found")

    svc = ConversationAnalysisService(db)
    try:
        analysis = await svc.analyze_voice_session(session_id)
    except AppError:
        raise
    except Exception:
        logger.exception("Voice session analysis failed for %s", session_id)
        raise HTTPException(status_code=500, detail="Analysis failed")

    return analysis


# ── Analyze a single chat session ─────────────────────────────────────────────


@router.post("/{ad_id}/chat-sessions/{session_id}/analyze")
async def analyze_chat_session(
    ad_id: str,
    session_id: str,
    user: User = Depends(require_roles(_COORDINATOR_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await _assert_campaign_ownership(ad_id, user, db)

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.campaign_id == ad_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Chat session not found")

    svc = ConversationAnalysisService(db)
    try:
        analysis = await svc.analyze_chat_session(session_id)
    except AppError:
        raise
    except Exception:
        logger.exception("Chat session analysis failed for %s", session_id)
        raise HTTPException(status_code=500, detail="Analysis failed")

    return analysis


# ── Auto-analyze all sessions without analysis ────────────────────────────────


@router.post("/{ad_id}/auto-analyze")
async def auto_analyze_campaign(
    ad_id: str,
    user: User = Depends(require_roles(_COORDINATOR_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """
    Background analysis pass: run Claude on every voice session and chat session
    that doesn't already have analysis. Skips sessions with no transcript/messages.
    Returns counts of newly analyzed vs skipped sessions.
    """
    await _assert_campaign_ownership(ad_id, user, db)
    svc = ConversationAnalysisService(db)

    # Voice sessions without analysis
    voice_result = await db.execute(
        select(VoiceSession)
        .join(VoiceSession.transcripts)
        .where(
            VoiceSession.advertisement_id == ad_id,
            VoiceSession.call_analysis == None,  # noqa: E711
        )
        .distinct()
    )
    voice_sessions = voice_result.scalars().all()

    # Chat sessions without analysis
    chat_result = await db.execute(
        select(ChatSession).where(
            ChatSession.campaign_id == ad_id,
            ChatSession.chat_analysis == None,  # noqa: E711
        )
    )
    chat_sessions = chat_result.scalars().all()

    analyzed = 0
    skipped = 0

    for vs in voice_sessions:
        try:
            await svc.analyze_voice_session(vs.id)
            analyzed += 1
        except Exception as e:
            logger.warning("Auto-analyze skipped voice session %s: %s", vs.id, e)
            skipped += 1

    for cs in chat_sessions:
        if not cs.messages:
            skipped += 1
            continue
        try:
            await svc.analyze_chat_session(cs.id)
            analyzed += 1
        except Exception as e:
            logger.warning("Auto-analyze skipped chat session %s: %s", cs.id, e)
            skipped += 1

    return {"analyzed": analyzed, "skipped": skipped}


# ── List chat sessions for a campaign ────────────────────────────────────────


@router.get("/{ad_id}/chat-sessions")
async def list_chat_sessions(
    ad_id: str,
    user: User = Depends(require_roles(_COORDINATOR_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await _assert_campaign_ownership(ad_id, user, db)

    result = await db.execute(
        select(ChatSession).where(ChatSession.campaign_id == ad_id).order_by(ChatSession.created_at.desc())
    )
    sessions = result.scalars().all()

    return {
        "sessions": [
            {
                "id": s.id,
                "session_id": s.session_id,
                "message_count": len(s.messages or []),
                "has_analysis": s.chat_analysis is not None,
                "chat_analysis": s.chat_analysis,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in sessions
        ],
        "count": len(sessions),
    }


# ── Helper ────────────────────────────────────────────────────────────────────


async def _assert_campaign_ownership(ad_id: str, user: User, db: AsyncSession):
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Campaign not found")
