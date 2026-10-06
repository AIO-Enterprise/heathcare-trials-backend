"""
Conversation Analysis Service
Analyzes voice call and chatbot transcripts using Claude to produce structured insights:
- Plain-language summary
- Questions asked by the agent and answers given by the user
- Questionnaire section: each question, the user's answer, and per-answer eligibility
- Drop-off point: the exact turn where the user became ineligible (if applicable)
- Information retrieved (name, phone, age, sex — or "not provided")
"""

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.core.llm import extract_json, get_ai_client
from app.models.models import CallTranscript, ChatSession, SurveyResponse, VoiceSession

logger = logging.getLogger(__name__)

# ── Prompt template ────────────────────────────────────────────────────────────

_SYSTEM = """You are an expert conversation analyst for healthcare clinical trial recruitment.
You receive a raw conversation transcript (voice call or chatbot) between an AI recruitment agent and a potential trial participant.

Your job is to extract structured insight from the conversation. Respond ONLY with valid JSON — no preamble, no markdown fences.

Output this exact JSON structure:
{
  "summary": "2-3 sentence plain-language summary of the conversation",
  "channel": "voice" or "chat",
  "duration_label": "e.g. '3 min 42 sec' for voice, 'N/A' for chat",
  "information_retrieved": {
    "name": "value or null",
    "phone": "value or null",
    "age": "value or null",
    "sex": "value or null",
    "email": "value or null"
  },
  "questions_asked": [
    {
      "turn": <integer — 1-based turn number when agent asked>,
      "question": "exact or paraphrased agent question",
      "user_answer": "exact or paraphrased user response, or null if no answer"
    }
  ],
  "questionnaire_responses": [
    {
      "question_text": "eligibility screening question text",
      "selected_option": "what the user answered",
      "is_eligible": true/false/null
    }
  ],
  "eligibility_outcome": "eligible" or "not_eligible" or "review_needed" or "unknown",
  "drop_off_turn": <integer turn number where user first became ineligible, or null>,
  "drop_off_reason": "brief explanation of why user became ineligible, or null",
  "booking_attempted": true/false,
  "booking_outcome": "booked" or "declined" or "not_reached" or null
}

Rules:
- "questions_asked" should list every distinct question the agent posed, in order
- "questionnaire_responses" is only the formal screening/eligibility questions
- eligibility_outcome rules (in priority order):
  - "not_eligible": participant gave at least one clearly disqualifying answer
  - "eligible": participant answered all screening questions and all answers are qualifying
  - "review_needed": participant answered most but not all screening questions, OR gave an ambiguous answer that could go either way — a human should review
  - "unknown": the conversation ended before any screening questions were reached
- drop_off_turn is the turn where the disqualifying answer was given (not when agent acknowledged it)
- If the user did not provide their name/phone/email, set those fields to null
- Keep summary factual and concise — no opinions
"""


def _format_voice_transcript(transcripts: list[CallTranscript], duration_seconds: int | None) -> str:
    sorted_turns = sorted(transcripts, key=lambda t: t.turn_index or 0)
    lines = []
    for t in sorted_turns:
        speaker = "AGENT" if t.speaker == "agent" else "USER"
        lines.append(f"[Turn {(t.turn_index or 0) + 1}] {speaker}: {t.text}")
    duration = f"{duration_seconds // 60}m {duration_seconds % 60}s" if duration_seconds else "unknown"
    return f"CHANNEL: voice\nDURATION: {duration}\n\n" + "\n".join(lines)


def _format_chat_messages(messages: list[dict]) -> str:
    lines = []
    for i, msg in enumerate(messages, 1):
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        lines.append(f"[Turn {i}] {role}: {content}")
    return "CHANNEL: chat\nDURATION: N/A\n\n" + "\n".join(lines)


class ConversationAnalysisService:
    def __init__(self, db: AsyncSession, ai=None):
        self.db = db
        # Injectable AI client (DIP) — defaults to the shared provider adapter.
        self.ai = ai or get_ai_client()

    # ── Voice session analysis ─────────────────────────────────────────────────

    async def analyze_voice_session(self, session_id: str) -> dict[str, Any]:
        result = await self.db.execute(
            select(VoiceSession).where(VoiceSession.id == session_id).options(selectinload(VoiceSession.transcripts))
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise NotFoundError(f"VoiceSession {session_id} not found")

        if not session.transcripts:
            raise ValidationError("No transcript available for this session")

        transcript_text = _format_voice_transcript(session.transcripts, session.duration_seconds)
        analysis = await self._call_claude(transcript_text)

        # Persist analysis back to the session
        session.call_analysis = analysis
        await self.db.commit()
        return analysis

    # ── Chat session analysis ──────────────────────────────────────────────────

    async def analyze_chat_session(self, session_db_id: str) -> dict[str, Any]:
        result = await self.db.execute(select(ChatSession).where(ChatSession.id == session_db_id))
        chat = result.scalar_one_or_none()
        if chat is None:
            raise NotFoundError(f"ChatSession {session_db_id} not found")

        messages = chat.messages or []
        if not messages:
            raise ValidationError("No messages in this chat session")

        transcript_text = _format_chat_messages(messages)
        analysis = await self._call_claude(transcript_text)

        chat.chat_analysis = analysis
        await self.db.commit()
        return analysis

    # ── Analyze all voice sessions for a survey response ──────────────────────

    async def analyze_participant(self, survey_response_id: str) -> list[dict[str, Any]]:
        result = await self.db.execute(
            select(SurveyResponse)
            .where(SurveyResponse.id == survey_response_id)
            .options(selectinload(SurveyResponse.voice_sessions).selectinload(VoiceSession.transcripts))
        )
        participant = result.scalar_one_or_none()
        if participant is None:
            raise NotFoundError("Participant not found")

        results = []
        for vs in participant.voice_sessions:
            if not vs.transcripts:
                continue
            try:
                analysis = await self.analyze_voice_session(vs.id)
                results.append({"session_id": vs.id, "analysis": analysis})
            except Exception as e:
                logger.warning("Failed to analyze session %s: %s", vs.id, e)
                results.append({"session_id": vs.id, "error": str(e)})
        return results

    # ── Analyze all chat sessions for a campaign ──────────────────────────────

    async def analyze_chat_sessions_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        result = await self.db.execute(select(ChatSession).where(ChatSession.campaign_id == campaign_id))
        chats = result.scalars().all()

        results = []
        for chat in chats:
            if not chat.messages:
                continue
            try:
                analysis = await self.analyze_chat_session(chat.id)
                results.append({"chat_session_id": chat.id, "analysis": analysis})
            except Exception as e:
                logger.warning("Failed to analyze chat session %s: %s", chat.id, e)
                results.append({"chat_session_id": chat.id, "error": str(e)})
        return results

    # ── Claude call ───────────────────────────────────────────────────────────

    async def _call_claude(self, transcript_text: str) -> dict[str, Any]:
        raw = await self.ai.complete(
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyze this conversation transcript:\n\n{transcript_text}",
                }
            ],
            max_tokens=2048,
        )

        try:
            return extract_json(raw)
        except json.JSONDecodeError:
            logger.error("Claude returned non-JSON analysis: %s", raw[:300])
            raise ExternalServiceError("claude", "analysis returned invalid JSON")
