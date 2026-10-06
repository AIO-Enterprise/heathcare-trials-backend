"""
SMS service — sends transactional SMS (and optionally WhatsApp) via Twilio's
REST API, using the httpx client already shipped with the project (no twilio SDK).

If TWILIO_ACCOUNT_SID is not configured, the message is printed to the server
log instead — useful for local development without a Twilio account. This
mirrors the dev-mode fallback in email_service.py.
"""

import logging
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConfigurationError, ExternalServiceError
from app.models.sms import SmsMessage

logger = logging.getLogger(__name__)

_TWILIO_BASE = "https://api.twilio.com/2010-04-01"


async def send_sms(to: str, body: str, *, as_whatsapp: bool = False) -> str | None:
    """Send a single SMS/WhatsApp message.

    Returns the Twilio Message SID on success, or None when running in dev mode
    (no credentials configured). Raises on a real send failure so callers can
    decide whether to swallow the error.
    """
    if not settings.TWILIO_ACCOUNT_SID:
        logger.info("[DEV — no Twilio] SMS to %s: %s", to, body)
        return None

    if as_whatsapp:
        from_addr = settings.TWILIO_WHATSAPP_FROM
        to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    else:
        from_addr = settings.TWILIO_FROM_NUMBER
        to_addr = to

    if not from_addr:
        var = "TWILIO_WHATSAPP_FROM" if as_whatsapp else "TWILIO_FROM_NUMBER"
        raise ConfigurationError(f"{var} is not configured")

    url = f"{_TWILIO_BASE}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            data={"To": to_addr, "From": from_addr, "Body": body},
        )

    # On failure, surface Twilio's actual error (code + message) instead of the
    # opaque httpx "Client error '400'..." string. Twilio returns JSON like
    # {"code": 21211, "message": "The 'To' number ... is not valid", "more_info": "..."}.
    if resp.status_code >= 400:
        try:
            err = resp.json()
            code = err.get("code")
            message = err.get("message") or resp.text
            more = err.get("more_info")
        except ValueError:
            code, message, more = None, resp.text or f"HTTP {resp.status_code}", None
        detail = f"Twilio error {code}: {message}" if code else f"Twilio error: {message}"
        if more:
            detail += f" ({more})"
        logger.error("Twilio send failed: %s", detail)
        raise ExternalServiceError("twilio", detail)

    sid = resp.json().get("sid")
    logger.info("SMS sent to %s (sid=%s)", to, sid)
    return sid


async def send_and_log(
    db: AsyncSession,
    to: str,
    body: str,
    kind: str = "manual",
    *,
    appointment_id: str | None = None,
    company_id: str | None = None,
    as_whatsapp: bool = False,
) -> SmsMessage:
    """Send an SMS and persist an SmsMessage audit row.

    Never raises — a send failure is recorded as status='failed' so the calling
    request (e.g. booking a slot) is never blocked by SMS delivery problems.
    """
    record = SmsMessage(
        to_number=to,
        body=body,
        kind=kind,
        appointment_id=appointment_id,
        company_id=company_id,
    )
    try:
        sid = await send_sms(to, body, as_whatsapp=as_whatsapp)
        record.status = "sent"
        record.provider_sid = sid
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)[:500]
        logger.warning("SMS to %s failed: %s", to, exc)

    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


# ── Message templates ───────────────────────────────────────────────────────


def _fmt_slot(dt: datetime) -> str:
    """Render a slot datetime, e.g. 'Mon 02 Jun at 02:30 PM'."""
    return dt.strftime("%a %d %b at %I:%M %p")


def confirmation_body(patient_name: str, slot_dt: datetime, campaign_name: str = "") -> str:
    who = f" for {campaign_name}" if campaign_name else ""
    return (
        f"Hi {patient_name}, your appointment{who} is confirmed for "
        f"{_fmt_slot(slot_dt)}. Reply if you need to reschedule."
    )


def cancellation_body(patient_name: str, slot_dt: datetime) -> str:
    return f"Hi {patient_name}, your appointment on {_fmt_slot(slot_dt)} has been cancelled. Contact us to rebook."


def reminder_body(patient_name: str, slot_dt: datetime) -> str:
    return (
        f"Reminder: Hi {patient_name}, you have an appointment on {_fmt_slot(slot_dt)}. We look forward to seeing you."
    )
