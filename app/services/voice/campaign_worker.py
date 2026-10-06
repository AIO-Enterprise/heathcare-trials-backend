"""
Bulk outbound call campaign worker.

Runs as asyncio background tasks — no Celery/Redis required.
All state lives in the DB: on container restart, lifespan calls
resume_running_campaigns() to re-spawn workers for any campaigns
still in "running" status.

Concurrency model
-----------------
- asyncio.Semaphore(concurrency) caps simultaneous in-flight dials.
- min_gap = 60 / per_minute paces dial-start rate.
- _dial() is fire-and-forget — the main loop does NOT await it.

Completion model
----------------
- Worker exits once no more "pending" records remain, marks campaign "done".
- voice_webhook.py calls mark_record_outcome() as ElevenLabs posts outcomes.
- run_stale_sweeper() (started in main.py) resets records stuck in
  dialing/in_progress for >15 min to "no_answer".
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.db.database import async_session_factory
from app.models.voice import CallCampaign, CallRecord

logger = logging.getLogger(__name__)

# In-memory registry of active worker tasks (keyed by campaign_id).
# Cleared automatically when a task finishes.
_active: dict[str, asyncio.Task] = {}


# ── Public API ────────────────────────────────────────────────────────────────


def start_worker(campaign_id: str) -> None:
    """Spawn (or reuse) a background worker coroutine for this campaign."""
    if campaign_id in _active and not _active[campaign_id].done():
        return
    task = asyncio.create_task(_run(campaign_id), name=f"campaign:{campaign_id}")
    _active[campaign_id] = task
    task.add_done_callback(lambda _: _active.pop(campaign_id, None))


async def resume_running_campaigns() -> None:
    """Re-spawn workers for campaigns left in 'running' state after a restart."""
    async with async_session_factory() as db:
        result = await db.execute(select(CallCampaign.id).where(CallCampaign.status == "running"))
        for (campaign_id,) in result.all():
            logger.info("Resuming campaign worker after restart: %s", campaign_id)
            start_worker(campaign_id)


async def mark_record_outcome(conversation_id: str, call_status: str) -> None:
    """
    Called by voice_webhook.py when ElevenLabs delivers a post-call event.
    Translates the webhook status to a CallRecord terminal status and refreshes
    the campaign's completed/failed counters.
    """
    outcome = "completed" if call_status == "ended" else "failed"
    async with async_session_factory() as db:
        result = await db.execute(select(CallRecord).where(CallRecord.conversation_id == conversation_id))
        record = result.scalar_one_or_none()
        if not record or record.status not in ("dialing", "in_progress", "no_answer"):
            return
        record.status = outcome
        await db.commit()
        await _refresh_counters(db, record.campaign_id)


async def run_stale_sweeper() -> None:
    """
    Background loop that resets records stuck in dialing/in_progress for
    more than 15 minutes to 'no_answer' (covers webhook delivery failures).
    Runs every 5 minutes.
    """
    while True:
        try:
            await asyncio.sleep(300)
            cutoff = datetime.utcnow() - timedelta(minutes=15)
            async with async_session_factory() as db:
                result = await db.execute(
                    select(CallRecord).where(
                        CallRecord.status.in_(["dialing", "in_progress"]),
                        CallRecord.called_at < cutoff,
                    )
                )
                stale = result.scalars().all()
                if stale:
                    for record in stale:
                        record.status = "no_answer"
                    await db.commit()
                    # Refresh counters per campaign
                    campaign_ids = {r.campaign_id for r in stale}
                    for cid in campaign_ids:
                        await _refresh_counters(db, cid)
                    logger.info("Stale sweeper: reset %d records to no_answer", len(stale))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale sweeper error")


# ── Internal worker ───────────────────────────────────────────────────────────


async def _run(campaign_id: str) -> None:
    try:
        await _loop(campaign_id)
    except asyncio.CancelledError:
        logger.info("Campaign worker cancelled: %s", campaign_id)
        raise
    except Exception:
        logger.exception("Campaign worker crashed: %s", campaign_id)
        async with async_session_factory() as db:
            await db.execute(
                update(CallCampaign)
                .where(CallCampaign.id == campaign_id)
                .values(status="failed", finished_at=datetime.utcnow())
            )
            await db.commit()


async def _loop(campaign_id: str) -> None:
    sem: asyncio.Semaphore | None = None
    current_concurrency: int = 0

    while True:
        async with async_session_factory() as db:
            campaign = await db.get(CallCampaign, campaign_id)
            if campaign is None:
                return
            if campaign.status in ("paused", "cancelled", "done", "failed"):
                logger.info("Campaign %s exiting: status=%s", campaign_id, campaign.status)
                return

            # Recreate semaphore only when concurrency setting changes
            if sem is None or current_concurrency != campaign.concurrency:
                sem = asyncio.Semaphore(campaign.concurrency)
                current_concurrency = campaign.concurrency

            min_gap = 60.0 / max(campaign.per_minute, 1)

            record = await _claim_next(db, campaign_id)
            if record is None:
                inflight = await db.scalar(
                    select(func.count(CallRecord.id)).where(
                        CallRecord.campaign_id == campaign_id,
                        CallRecord.status.in_(["dialing", "in_progress"]),
                    )
                )
                if inflight:
                    await asyncio.sleep(10)
                    continue
                await db.execute(
                    update(CallCampaign)
                    .where(CallCampaign.id == campaign_id)
                    .values(status="done", finished_at=datetime.utcnow())
                )
                await db.commit()
                logger.info("Campaign %s: all records settled — marking done", campaign_id)
                return

            record_id = record.id
            advertisement_id = record.advertisement_id
            phone = record.phone_e164

        # Fire-and-forget: do NOT await — keeps the loop pacing independently
        asyncio.create_task(
            _dial(record_id, advertisement_id, phone, sem),
            name=f"dial:{record_id}",
        )
        await asyncio.sleep(min_gap)


async def _claim_next(db, campaign_id: str) -> CallRecord | None:
    """Atomically claim the next pending record by transitioning it to 'dialing'."""
    result = await db.execute(
        select(CallRecord)
        .where(
            CallRecord.campaign_id == campaign_id,
            CallRecord.status == "pending",
        )
        .order_by(CallRecord.created_at)
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if record:
        record.status = "dialing"
        record.attempts = (record.attempts or 0) + 1
        record.called_at = datetime.utcnow()
        await db.commit()
    return record


async def _dial(
    record_id: str,
    advertisement_id: str,
    phone: str,
    sem: asyncio.Semaphore,
) -> None:
    # Import here to avoid circular imports at module load time
    from app.services.ai.voicebot_agent import VoicebotAgentService

    async with sem:
        async with async_session_factory() as db:
            record = await db.get(CallRecord, record_id)
            if not record:
                return
            svc = VoicebotAgentService(db)
            try:
                result = await svc.outbound_call(advertisement_id, phone)
                record.conversation_id = result.get("conversation_id")
                record.status = "in_progress"
                logger.info("Dialled %s → conversation_id=%s", phone, record.conversation_id)
            except Exception as exc:
                logger.warning("Dial failed record=%s phone=%s: %s", record_id, phone, exc)
                if (record.attempts or 0) >= (record.max_attempts or 2):
                    record.status = "failed"
                    record.last_error = str(exc)[:500]
                    await db.commit()
                    await _refresh_counters(db, record.campaign_id)
                else:
                    record.status = "pending"  # will be retried on next worker pass
                    await db.commit()
                return
            await db.commit()


async def _refresh_counters(db, campaign_id: str) -> None:
    """Recount completed/failed records and update the campaign row."""
    result = await db.execute(
        select(CallRecord.status, func.count(CallRecord.id).label("n"))
        .where(CallRecord.campaign_id == campaign_id)
        .group_by(CallRecord.status)
    )
    counts = {row.status: row.n for row in result.all()}
    campaign = await db.get(CallCampaign, campaign_id)
    if campaign:
        campaign.completed = counts.get("completed", 0)
        campaign.failed_count = counts.get("failed", 0) + counts.get("no_answer", 0)
        await db.commit()
