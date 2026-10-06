"""
Survey Responses Routes
Handles submission (public, no auth) and retrieval (study coordinator only).

POST /api/advertisements/{ad_id}/survey-responses
  — Called by the public landing page / chatbot after questionnaire completion.
  — No auth required.

GET /api/advertisements/{ad_id}/survey-responses
  — Returns all responses for a campaign.
  — Study Coordinator (company-scoped) only.

GET /api/advertisements/{ad_id}/survey-responses/{response_id}
  — Returns a single response with full details including voice call transcript.
  — Study Coordinator (company-scoped) only.
"""

import logging
import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.core.timeutils import ad_zone, today_in
from app.db.database import get_db
from app.models.models import Advertisement, SurveyResponse, User, VoiceSession
from app.models.survey import Appointment
from app.models.voice import ChatSession
from app.schemas.schemas import SurveyResponseCreate, SurveyResponseOut

router = APIRouter(tags=["Survey Responses"])
logger = logging.getLogger(__name__)

_PHONE_LIKE_RE = re.compile(r"^[\d\s\+\-\(\)]+$")


# ── Public campaign info (title + questionnaire + booking window — no auth) ────
class PublicCampaignOut(BaseModel):
    id: str
    title: str
    questionnaire: dict | None = None
    booking_window_start: str | None = None  # "YYYY-MM-DD"
    booking_window_end: str | None = None  # "YYYY-MM-DD"
    slot_duration_minutes: int = 30
    max_per_slot: int = 3

    class Config:
        from_attributes = True


_DEFAULT_BOOKING_DAYS = 30


@router.get("/advertisements/{ad_id}/public", response_model=PublicCampaignOut)
async def get_public_campaign(
    ad_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
    ad = result.scalar_one_or_none()
    if ad is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Booking window resolves "today" in the campaign's timezone.
    today = today_in(ad_zone(ad))
    win_start = max(ad.trial_start_date, today) if ad.trial_start_date else today
    win_end = ad.trial_end_date if ad.trial_end_date else today + timedelta(days=_DEFAULT_BOOKING_DAYS)

    bc = ad.booking_config if isinstance(ad.booking_config, dict) else {}
    return PublicCampaignOut(
        id=ad.id,
        title=ad.title,
        questionnaire=ad.questionnaire,
        booking_window_start=str(win_start),
        booking_window_end=str(win_end),
        slot_duration_minutes=int(bc.get("slot_duration_minutes") or 30),
        max_per_slot=int(bc.get("max_per_slot") or 3),
    )


# ── Submit survey response (public — no auth) ──────────────────────────────────
@router.post(
    "/advertisements/{ad_id}/survey-responses",
    response_model=SurveyResponseOut,
    status_code=201,
)
async def submit_survey_response(
    ad_id: str,
    body: SurveyResponseCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id))
    ad = result.scalar_one_or_none()
    if ad is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    response = SurveyResponse(
        advertisement_id=ad_id,
        full_name=body.full_name,
        age=body.age,
        sex=body.sex,
        phone=body.phone,
        answers=[a.model_dump() for a in body.answers],
        is_eligible=body.is_eligible,
    )
    db.add(response)
    await db.commit()
    await db.refresh(response)

    refreshed = await db.execute(
        select(SurveyResponse)
        .where(SurveyResponse.id == response.id)
        .options(selectinload(SurveyResponse.voice_sessions))
    )
    return refreshed.scalar_one()


# ── List responses (study coordinator, company-scoped) ─────────────────────────
@router.get(
    "/advertisements/{ad_id}/survey-responses",
    response_model=list[SurveyResponseOut],
)
async def list_survey_responses(
    ad_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ad = await _get_campaign_for_user(ad_id, current_user, db)
    if ad is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    result = await db.execute(
        select(SurveyResponse)
        .where(SurveyResponse.advertisement_id == ad_id)
        .options(selectinload(SurveyResponse.voice_sessions).selectinload(VoiceSession.transcripts))
        .order_by(SurveyResponse.created_at.desc())
    )
    return result.scalars().all()


# ── Get single response (with voice transcript) ────────────────────────────────
@router.get(
    "/advertisements/{ad_id}/survey-responses/{response_id}",
    response_model=SurveyResponseOut,
)
async def get_survey_response(
    ad_id: str,
    response_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ad = await _get_campaign_for_user(ad_id, current_user, db)
    if ad is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    result = await db.execute(
        select(SurveyResponse)
        .where(
            SurveyResponse.id == response_id,
            SurveyResponse.advertisement_id == ad_id,
        )
        .options(selectinload(SurveyResponse.voice_sessions).selectinload(VoiceSession.transcripts))
    )
    response = result.scalar_one_or_none()
    if response is None:
        raise HTTPException(status_code=404, detail="Response not found")
    return response


# ── Unified participants list (all channels) ───────────────────────────────────
@router.get("/advertisements/{ad_id}/participants")
async def list_participants(
    ad_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Unified view of everyone who engaged with this campaign across all channels
    and left at least a name or phone number.

    Returns a flat list where each entry has:
      id, name, phone, age, sex, source, eligibility,
      interaction_id (chat/voice session id for drill-down),
      appointment (slot info if booked),
      analysis (AI analysis dict if available),
      created_at
    """
    ad = await _get_campaign_for_user(ad_id, current_user, db)
    if ad is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    participants: list[dict[str, Any]] = []

    # ── 1. Survey responses ───────────────────────────────────────────────────
    sr_result = await db.execute(
        select(SurveyResponse)
        .where(SurveyResponse.advertisement_id == ad_id)
        .options(
            selectinload(SurveyResponse.voice_sessions).selectinload(VoiceSession.transcripts),
            selectinload(SurveyResponse.appointments),
        )
        .order_by(SurveyResponse.created_at.desc())
    )
    for sr in sr_result.scalars().all():
        # Derive eligibility from voice session analysis, fall back to survey flag
        outcomes = [vs.call_analysis.get("eligibility_outcome") for vs in sr.voice_sessions if vs.call_analysis]
        if "not_eligible" in outcomes:
            eligibility = "not_eligible"
        elif "eligible" in outcomes:
            eligibility = "eligible"
        elif "review_needed" in outcomes:
            eligibility = "review_needed"
        elif sr.is_eligible is True:
            eligibility = "eligible"
        elif sr.is_eligible is False:
            eligibility = "not_eligible"
        else:
            eligibility = "unknown"

        appt = sr.appointments[0] if sr.appointments else None
        participants.append(
            {
                "id": sr.id,
                "name": sr.full_name,
                "phone": sr.phone,
                "age": sr.age,
                "sex": sr.sex,
                "source": "survey",
                "eligibility": eligibility,
                "interaction_id": sr.voice_sessions[0].id if sr.voice_sessions else None,
                "voice_sessions": [
                    {
                        "id": vs.id,
                        "elevenlabs_conversation_id": vs.elevenlabs_conversation_id,
                        "status": vs.status,
                        "phone": vs.phone,
                        "started_at": vs.started_at.isoformat() if vs.started_at else None,
                        "ended_at": vs.ended_at.isoformat() if vs.ended_at else None,
                        "duration_seconds": vs.duration_seconds,
                        "call_analysis": vs.call_analysis,
                        "transcripts": [
                            {"speaker": t.speaker, "text": t.text, "turn_index": t.turn_index}
                            for t in sorted(vs.transcripts, key=lambda x: x.turn_index or 0)
                        ],
                    }
                    for vs in sr.voice_sessions
                ],
                "answers": sr.answers or [],
                "appointment": {
                    "id": appt.id,
                    "slot_datetime": appt.slot_datetime.isoformat(),
                    "duration_minutes": appt.duration_minutes,
                    "status": appt.status,
                    "notes": appt.notes,
                }
                if appt
                else None,
                "analysis": None,
                "created_at": sr.created_at.isoformat() if sr.created_at else None,
            }
        )

    # ── 2. Chatbot appointments (no survey, no voice — regardless of chat_session_id) ─
    # Includes legacy rows where chat_session_id was NULL before the column was added.
    chat_appts_result = await db.execute(
        select(Appointment)
        .where(
            Appointment.advertisement_id == ad_id,
            Appointment.survey_response_id.is_(None),
            Appointment.voice_session_id.is_(None),
        )
        .order_by(Appointment.created_at.desc())
    )

    # Pre-load all analyzed chat sessions for this campaign so we can match
    # legacy appointments (no chat_session_id) by name from the analysis.
    all_cs_result = await db.execute(
        select(ChatSession).where(
            ChatSession.campaign_id == ad_id,
            ChatSession.chat_analysis.isnot(None),
        )
    )
    all_chat_sessions = all_cs_result.scalars().all()

    # Index analyzed sessions once — O(1) lookups instead of linear scans
    # per appointment.
    cs_by_id = {cs.id: cs for cs in all_chat_sessions}
    cs_by_name: dict = {}
    for cs in all_chat_sessions:
        retrieved = (
            (cs.chat_analysis or {}).get("information_retrieved", {}) if isinstance(cs.chat_analysis, dict) else {}
        )
        name_key = (retrieved.get("name") or "").lower()
        if name_key and name_key not in cs_by_name:
            cs_by_name[name_key] = cs

    def _find_chat_session(appt: Appointment):
        """Return matching ChatSession: by id first, then by name in analysis."""
        if appt.chat_session_id:
            return cs_by_id.get(appt.chat_session_id)
        # Legacy fallback: match by patient name stored in analysis info_retrieved
        return cs_by_name.get((appt.patient_name or "").lower())

    for appt in chat_appts_result.scalars().all():
        cs = _find_chat_session(appt)
        participants.append(
            {
                "id": appt.id,
                "name": appt.patient_name,
                "phone": appt.patient_phone,
                "age": None,
                "sex": None,
                "source": "chatbot",
                "eligibility": cs.chat_analysis.get("eligibility_outcome", "review_needed")
                if cs and cs.chat_analysis
                else "review_needed",
                "interaction_id": cs.id if cs else appt.chat_session_id,
                "voice_sessions": [],
                "answers": [],
                "chat_messages": cs.messages if cs else [],
                "appointment": {
                    "id": appt.id,
                    "slot_datetime": appt.slot_datetime.isoformat(),
                    "duration_minutes": appt.duration_minutes,
                    "status": appt.status,
                    "notes": appt.notes,
                },
                "analysis": cs.chat_analysis if cs else None,
                "created_at": appt.created_at.isoformat() if appt.created_at else None,
            }
        )

    # ── 3. Voicebot sessions — ALL ended/failed voice sessions not linked to a survey ──
    # Covers callers who were screened but did NOT book an appointment, as well as
    # those who did (appointment linked via voice_session_id on the Appointment row).
    # IDs already covered by survey section (survey_response_id set) are excluded.
    survey_vs_ids = {vs["id"] for p in participants if p["source"] == "survey" for vs in p["voice_sessions"]}

    voice_sessions_result = await db.execute(
        select(VoiceSession)
        .where(
            VoiceSession.advertisement_id == ad_id,
            VoiceSession.survey_response_id.is_(None),
            VoiceSession.status.in_(["ended", "failed"]),
        )
        .options(selectinload(VoiceSession.transcripts))
        .order_by(VoiceSession.started_at.desc())
    )
    voice_sessions = voice_sessions_result.scalars().all()

    # Batch-fetch linked appointments for all sessions in one query
    appt_by_vs: dict = {}
    if voice_sessions:
        vs_appts_result = await db.execute(
            select(Appointment).where(Appointment.voice_session_id.in_([vs.id for vs in voice_sessions]))
        )
        for appt in vs_appts_result.scalars().all():
            appt_by_vs[appt.voice_session_id] = appt

    for vs in voice_sessions:
        if vs.id in survey_vs_ids:
            continue

        eligibility = "unknown"
        if vs.call_analysis:
            eligibility = vs.call_analysis.get("eligibility_outcome", "unknown")

        # Derive name from analysis info_retrieved only — must be a real name, not a phone/description
        analysis = vs.call_analysis or {}
        retrieved = analysis.get("information_retrieved", {}) if isinstance(analysis, dict) else {}
        raw_name = retrieved.get("name") or ""
        name = raw_name if raw_name and not _PHONE_LIKE_RE.match(raw_name.strip()) else None

        # Phone: always use the real call number, never AI-analysis text
        phone = vs.phone or ""

        # Check if there's a linked appointment (may carry name/phone too)
        appt = appt_by_vs.get(vs.id)
        if appt:
            if not name:
                name = appt.patient_name
            if not phone:
                phone = appt.patient_phone

        # Skip sessions with no fruitful result: no real name AND eligibility unknown
        if not name and eligibility == "unknown":
            continue

        participants.append(
            {
                "id": vs.id,
                "name": name or phone or "Unknown",
                "phone": phone,
                "age": None,
                "sex": None,
                "source": "voicebot",
                "eligibility": eligibility,
                "interaction_id": vs.id,
                "voice_sessions": [
                    {
                        "id": vs.id,
                        "elevenlabs_conversation_id": vs.elevenlabs_conversation_id,
                        "status": vs.status,
                        "phone": vs.phone,
                        "started_at": vs.started_at.isoformat() if vs.started_at else None,
                        "ended_at": vs.ended_at.isoformat() if vs.ended_at else None,
                        "duration_seconds": vs.duration_seconds,
                        "call_analysis": vs.call_analysis,
                        "transcripts": [
                            {"speaker": t.speaker, "text": t.text, "turn_index": t.turn_index}
                            for t in sorted(vs.transcripts, key=lambda x: x.turn_index or 0)
                        ],
                    }
                ],
                "answers": [],
                "appointment": {
                    "id": appt.id,
                    "slot_datetime": appt.slot_datetime.isoformat(),
                    "duration_minutes": appt.duration_minutes,
                    "status": appt.status,
                    "notes": appt.notes,
                }
                if appt
                else None,
                "analysis": vs.call_analysis,
                "created_at": vs.started_at.isoformat() if vs.started_at else None,
            }
        )

    # Sort all participants newest first
    participants.sort(key=lambda p: p["created_at"] or "", reverse=True)
    return participants


# ── Helper ─────────────────────────────────────────────────────────────────────
async def _get_campaign_for_user(ad_id: str, user: User, db: AsyncSession):
    """Return the Advertisement if it belongs to the user's company, else None."""
    result = await db.execute(
        select(Advertisement).where(
            Advertisement.id == ad_id,
            Advertisement.company_id == user.company_id,
        )
    )
    return result.scalar_one_or_none()
