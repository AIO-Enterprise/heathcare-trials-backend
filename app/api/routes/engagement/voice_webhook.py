"""
ElevenLabs Post-Call Webhook
Owner: Backend Dev

ElevenLabs POSTs a JSON payload to this endpoint when a conversation ends.
We extract the transcript and caller phone number, store everything in the DB,
and link the session to the matching SurveyResponse (matched by phone number).

Webhook signature verification:
  ElevenLabs sends: ElevenLabs-Signature: t=<unix_ts>,v0=<hmac_hex>
  HMAC = HMAC-SHA256(key=ELEVENLABS_WEBHOOK_SECRET, message=f"{t}.{raw_body}")
  Set ELEVENLABS_WEBHOOK_SECRET in .env to enable verification.
  If the secret is not set, the signature header is ignored (dev-only).
"""

import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.services.ai.voicebot_agent import VoicebotAgentService

router = APIRouter(tags=["Voice Webhook"])
logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, header: str) -> bool:
    """
    Verify the ElevenLabs-Signature header.
    Header format: t=<unix_timestamp>,v0=<hmac_hex>

    Fail-closed: when ELEVENLABS_WEBHOOK_SECRET is not configured the webhook
    is rejected in production (DEBUG off) and accepted only in local dev.
    """
    secret = settings.ELEVENLABS_WEBHOOK_SECRET
    if not secret:
        if settings.DEBUG:
            logger.warning("ELEVENLABS_WEBHOOK_SECRET not set — accepting unverified webhook (dev only)")
            return True
        logger.error("ELEVENLABS_WEBHOOK_SECRET not set in production — rejecting webhook")
        return False

    try:
        parts = dict(chunk.split("=", 1) for chunk in header.split(","))
        ts = parts["t"]
        sig = parts["v0"]
    except Exception:
        return False

    # Reject stale webhooks (> 5 minutes old)
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except Exception:
        return False

    expected = hmac.new(
        secret.encode(),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def _extract_phone_to(data: dict[str, Any]) -> str | None:
    """
    Pull the called party's phone number from the webhook payload.
    ElevenLabs nests this differently depending on API version — try all known paths.
    """
    metadata = data.get("metadata") or {}

    # v1 Conversational AI phone calls
    phone_call = metadata.get("phone_call") or {}
    if phone_call.get("called_number"):
        return phone_call["called_number"]
    if phone_call.get("to"):
        return phone_call["to"]

    # Alternative field names seen in practice
    for key in ("phone_number_to", "to_number", "called_id"):
        if metadata.get(key):
            return metadata[key]

    # Twilio-wrapped outbound: top-level "to" field
    if data.get("to"):
        return data["to"]

    return None


@router.post("/voice/webhook", status_code=204)
async def elevenlabs_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive a post-call webhook from ElevenLabs.
    Stores the transcript and links it to the matching participant.
    Always returns 204 so ElevenLabs doesn't retry on our processing errors.
    """
    body = await request.body()

    # Signature verification
    sig_header = request.headers.get("ElevenLabs-Signature", "")
    if not _verify_signature(body, sig_header):
        logger.warning("ElevenLabs webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        import json

        payload = json.loads(body)
    except Exception:
        logger.warning("ElevenLabs webhook: could not parse JSON body")
        return  # 204

    # Payload can be top-level or wrapped under {"data": {...}}
    data: dict[str, Any] = payload.get("data") or payload

    conversation_id: str | None = data.get("conversation_id") or payload.get("conversation_id")
    if not conversation_id:
        logger.warning("ElevenLabs webhook missing conversation_id — ignoring")
        return  # 204

    transcript_raw: list = data.get("transcript") or []
    phone_to = _extract_phone_to(data)

    # duration — prefer analysis/metadata fields
    metadata = data.get("metadata") or {}
    duration = metadata.get("call_duration_secs") or metadata.get("duration_seconds") or data.get("call_duration_secs")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    call_status = "ended"
    status_raw = (data.get("status") or "").lower()
    if status_raw in ("failed", "error"):
        call_status = "failed"

    try:
        svc = VoicebotAgentService(db)
        session = await svc.store_transcript_from_webhook(
            conversation_id=conversation_id,
            transcript_turns=transcript_raw,
            phone_to=phone_to,
            duration_seconds=duration,
            status=call_status,
        )
        # Auto-analyze immediately after storing — no manual trigger needed
        if session and transcript_raw and not session.call_analysis:
            try:
                await svc.get_conversation_analysis(conversation_id)
            except Exception as analysis_exc:
                logger.warning("Auto-analysis failed for conv %s: %s", conversation_id, analysis_exc)
    except Exception as exc:
        # Log but don't raise — always return 204 to prevent ElevenLabs retries
        logger.error("Failed to store ElevenLabs webhook for conv %s: %s", conversation_id, exc, exc_info=True)

    # Link outcome to bulk campaign CallRecord if this was a campaign-initiated call
    try:
        from app.services.voice.campaign_worker import mark_record_outcome

        await mark_record_outcome(conversation_id, call_status)
    except Exception as exc:
        logger.warning("Failed to mark campaign record outcome for conv %s: %s", conversation_id, exc)

    return  # 204
