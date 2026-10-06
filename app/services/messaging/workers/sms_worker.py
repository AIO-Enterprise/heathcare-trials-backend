"""
SMS background workers — appointment reminders + bulk SMS campaigns.

Mirrors the asyncio model in services/voice/campaign_worker.py: no Celery/Redis,
all state in the DB, workers re-spawned after a restart via resume_running_campaigns().

Reminder sweeper
----------------
Every SMS_REMINDER_SWEEP_SECONDS it finds confirmed appointments whose slot is
within the next SMS_REMINDER_HOURS_BEFORE hours and which have not yet received a
reminder (no SmsMessage row with kind='reminder' for that appointment), and sends
one. Dedup lives in sms_messages, so no new column on appointments is needed.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.timeutils import MAX_TZ_SPAN_HOURS, zone_from_config
from app.db.database import async_session_factory
from app.models.models import Advertisement
from app.models.sms import SmsCampaign, SmsMessage, SmsRecord
from app.models.survey import Appointment
from app.services.messaging import sms_service

logger = logging.getLogger(__name__)

_active: dict[str, asyncio.Task] = {}


# ── Reminder sweeper ────────────────────────────────────────────────────────


async def run_reminder_sweeper() -> None:
    """Periodically send reminders for upcoming confirmed appointments."""
    interval = max(settings.SMS_REMINDER_SWEEP_SECONDS, 30)
    while True:
        try:
            await asyncio.sleep(interval)
            await _send_due_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SMS reminder sweeper error")


async def _send_due_reminders() -> None:
    """
    Send reminders for confirmed appointments starting within the reminder
    horizon. slot_datetime is stored as naive *campaign-local* wall time, so
    the SQL window is widened by the maximum timezone span and the precise
    due-check happens per-appointment in the campaign's timezone.
    """
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    lead = timedelta(hours=settings.SMS_REMINDER_HOURS_BEFORE)
    wide_lo = now_utc - timedelta(hours=MAX_TZ_SPAN_HOURS)
    wide_hi = now_utc + lead + timedelta(hours=MAX_TZ_SPAN_HOURS)

    async with async_session_factory() as db:
        # Appointments already reminded (subquery of appointment_ids).
        reminded = select(SmsMessage.appointment_id).where(
            SmsMessage.kind == "reminder",
            SmsMessage.appointment_id.isnot(None),
        )
        result = await db.execute(
            select(Appointment, Advertisement.bot_config, Advertisement.company_id)
            .join(Advertisement, Appointment.advertisement_id == Advertisement.id)
            .where(
                Appointment.status == "confirmed",
                Appointment.slot_datetime > wide_lo,
                Appointment.slot_datetime <= wide_hi,
                Appointment.patient_phone.isnot(None),
                Appointment.id.notin_(reminded),
            )
        )
        candidates = result.all()

    zone_by_cfg = {}
    due = []  # (appointment, company_id)
    for appt, bot_config, company_id in candidates:
        zone = zone_by_cfg.get(id(bot_config))
        if zone is None:
            zone = zone_from_config(bot_config)
            zone_by_cfg[id(bot_config)] = zone
        local_now = datetime.now(zone).replace(tzinfo=None)
        if local_now < appt.slot_datetime <= local_now + lead:
            due.append((appt, company_id))

    for appt, company_id in due:
        async with async_session_factory() as db:
            await sms_service.send_and_log(
                db,
                appt.patient_phone,
                sms_service.reminder_body(appt.patient_name, appt.slot_datetime),
                kind="reminder",
                appointment_id=appt.id,
                company_id=company_id,
            )
    if due:
        logger.info("SMS reminder sweeper: sent %d reminders", len(due))


# ── Bulk campaign worker ────────────────────────────────────────────────────


def start_campaign(campaign_id: str) -> None:
    """Spawn (or reuse) a background worker for a bulk SMS campaign."""
    if campaign_id in _active and not _active[campaign_id].done():
        return
    task = asyncio.create_task(_run(campaign_id), name=f"sms_campaign:{campaign_id}")
    _active[campaign_id] = task
    task.add_done_callback(lambda _: _active.pop(campaign_id, None))


async def resume_running_campaigns() -> None:
    """Re-spawn workers for campaigns left running after a restart."""
    async with async_session_factory() as db:
        result = await db.execute(select(SmsCampaign.id).where(SmsCampaign.status == "running"))
        for (campaign_id,) in result.all():
            logger.info("Resuming SMS campaign after restart: %s", campaign_id)
            start_campaign(campaign_id)


async def _run(campaign_id: str) -> None:
    try:
        # Recover any records left mid-send by a previous crash/restart.
        async with async_session_factory() as db:
            await db.execute(
                update(SmsRecord)
                .where(SmsRecord.campaign_id == campaign_id, SmsRecord.status == "sending")
                .values(status="pending")
            )
            await db.commit()
        await _loop(campaign_id)
    except asyncio.CancelledError:
        logger.info("SMS campaign worker cancelled: %s", campaign_id)
        raise
    except Exception:
        logger.exception("SMS campaign worker crashed: %s", campaign_id)
        async with async_session_factory() as db:
            await db.execute(
                update(SmsCampaign)
                .where(SmsCampaign.id == campaign_id)
                .values(status="failed", finished_at=datetime.utcnow())
            )
            await db.commit()


async def _loop(campaign_id: str) -> None:
    while True:
        async with async_session_factory() as db:
            campaign = await db.get(SmsCampaign, campaign_id)
            if campaign is None:
                return
            if campaign.status in ("paused", "cancelled", "done", "failed"):
                logger.info("SMS campaign %s exiting: status=%s", campaign_id, campaign.status)
                return

            min_gap = 60.0 / max(campaign.per_minute, 1)
            body_template = campaign.body_template

            record = await _claim_next(db, campaign_id)
            if record is None:
                await db.execute(
                    update(SmsCampaign)
                    .where(SmsCampaign.id == campaign_id)
                    .values(status="done", finished_at=datetime.utcnow())
                )
                await db.commit()
                logger.info("SMS campaign %s: all records sent — marking done", campaign_id)
                return

            record_id = record.id
            phone = record.phone_e164
            name = record.contact_name or "there"

        body = body_template.replace("{name}", name)
        try:
            sid = await sms_service.send_sms(phone, body)
            outcome, err = "sent", None
        except Exception as exc:
            sid, outcome, err = None, "failed", str(exc)[:500]
            logger.warning("SMS campaign send failed phone=%s: %s", phone, exc)

        async with async_session_factory() as db:
            rec = await db.get(SmsRecord, record_id)
            if rec:
                rec.status = outcome
                rec.provider_sid = sid
                rec.last_error = err
                rec.sent_at = datetime.utcnow()
                await db.commit()
            await _refresh_counters(db, campaign_id)

        await asyncio.sleep(min_gap)


async def _claim_next(db, campaign_id: str):
    """Atomically claim the next pending record by flipping it out of 'pending'."""
    result = await db.execute(
        select(SmsRecord)
        .where(SmsRecord.campaign_id == campaign_id, SmsRecord.status == "pending")
        .order_by(SmsRecord.created_at)
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if record:
        record.status = "sending"
        await db.commit()
    return record


async def _refresh_counters(db, campaign_id: str) -> None:
    result = await db.execute(
        select(SmsRecord.status, func.count(SmsRecord.id).label("n"))
        .where(SmsRecord.campaign_id == campaign_id)
        .group_by(SmsRecord.status)
    )
    counts = {row.status: row.n for row in result.all()}
    campaign = await db.get(SmsCampaign, campaign_id)
    if campaign:
        campaign.completed = counts.get("sent", 0)
        campaign.failed_count = counts.get("failed", 0)
        await db.commit()
