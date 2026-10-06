"""
Meta Ads Pause Scheduler
Runs as an asyncio background task (started from main.py lifespan).

Instead of polling every N seconds, it:
  1. Loads all ads with a pause_schedule.
  2. Computes the exact next transition moment (pause OR resume) across all of them.
  3. Sleeps until that moment, then fires only the ads whose transition is due.
  4. Recalculates the next wake-up and repeats.

pause_schedule format (new): list of { id, days[], timeFrom, timeTo }
pause_schedule format (legacy): { pause_days[], pause_hours: "HH:MM-HH:MM" }

An ad is in a pause window if ANY of its windows is currently active.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.timeutils import ad_zone, zone_from_config
from app.db.database import async_session_factory
from app.models.models import Advertisement, PlatformConnection
from app.services.ads.meta_ads_service import MetaAdsService

logger = logging.getLogger(__name__)

_DAY_MAP = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}
_NO_SCHEDULE_POLL = 300  # seconds to sleep when no schedules configured


def _parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _normalize_windows(pause_schedule) -> list[dict]:
    """Convert any pause_schedule value to a list of window dicts."""
    if isinstance(pause_schedule, list):
        return pause_schedule
    if isinstance(pause_schedule, dict):
        # Legacy: { pause_days: [...], pause_hours: "HH:MM-HH:MM" }
        days = pause_schedule.get("pause_days") or []
        hours = pause_schedule.get("pause_hours") or ""
        time_from, time_to = "00:00", "00:00"
        if hours and "-" in hours:
            parts = hours.split("-", 1)
            time_from = parts[0].strip()
            time_to = parts[1].strip()
        return [{"id": "legacy", "days": days, "timeFrom": time_from, "timeTo": time_to}]
    return []


def _window_active(window: dict, now: datetime) -> bool:
    """Return True if `now` falls inside this pause window."""
    days = window.get("days") or []
    wanted_days = {_DAY_MAP[d] for d in days if d in _DAY_MAP}
    if not wanted_days or now.weekday() not in wanted_days:
        return False

    try:
        from_min = _parse_hhmm(window.get("timeFrom") or "00:00")
        to_min = _parse_hhmm(window.get("timeTo") or "00:00")
    except (ValueError, IndexError):
        return True  # bad format → treat as full-day pause

    now_min = now.hour * 60 + now.minute
    if from_min <= to_min:
        return from_min <= now_min <= to_min
    else:
        # Overnight wrap (e.g. 22:00 → 06:00)
        return now_min >= from_min or now_min <= to_min


def _any_window_active(windows: list[dict], now: datetime) -> bool:
    return any(_window_active(w, now) for w in windows)


def _next_window_transition_seconds(window: dict, now: datetime) -> float | None:
    """Seconds until next boundary for a single window. Returns None if unparseable."""
    days = window.get("days") or []
    wanted_weekdays = {_DAY_MAP[d] for d in days if d in _DAY_MAP}
    if not wanted_weekdays:
        return None

    try:
        from_min = _parse_hhmm(window.get("timeFrom") or "00:00")
        to_min = _parse_hhmm(window.get("timeTo") or "00:00")
    except (ValueError, IndexError):
        return None

    now_min = now.hour * 60 + now.minute

    for day_offset in range(8):
        candidate = now + timedelta(days=day_offset)
        cwd = candidate.weekday()
        if cwd not in wanted_weekdays:
            continue

        for boundary in sorted({from_min, to_min}):
            if day_offset == 0 and boundary <= now_min:
                continue
            boundary_dt = candidate.replace(hour=boundary // 60, minute=boundary % 60, second=0, microsecond=0)
            diff = (boundary_dt - now).total_seconds()
            if diff > 0:
                return diff

    return None


def _next_transition_seconds_for_schedule(pause_schedule, now: datetime) -> float | None:
    """Return seconds to the nearest upcoming boundary across all windows.

    `now` must be an aware datetime expressed in the campaign timezone.
    """
    windows = _normalize_windows(pause_schedule)
    earliest: float | None = None
    for w in windows:
        secs = _next_window_transition_seconds(w, now)
        if secs is not None and secs > 0:
            earliest = secs if earliest is None else min(earliest, secs)
    return earliest


async def _get_meta_conn(session, company_id: str):
    result = await session.execute(
        select(PlatformConnection).where(
            PlatformConnection.company_id == company_id,
            PlatformConnection.platform == "meta",
        )
    )
    return result.scalars().first()


async def _process_ad(session, ad: Advertisement, now: datetime) -> None:
    bot_config = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    sched = bot_config.get("pause_schedule")
    campaign_id = bot_config.get("meta_campaign_id") or ""
    adset_id = bot_config.get("meta_adset_id") or ""
    last_action = bot_config.get("last_scheduler_action") or ""

    if not sched or not campaign_id:
        return

    windows = _normalize_windows(sched)
    # Pause windows are wall-clock times in the campaign's timezone.
    now_local = now.astimezone(ad_zone(ad))
    should_pause = _any_window_active(windows, now_local)

    if should_pause and last_action == "paused":
        return
    if not should_pause and last_action != "paused":
        return

    conn = await _get_meta_conn(session, ad.company_id)
    if not conn or not conn.access_token or not conn.ad_account_id:
        logger.warning("Scheduler: no Meta connection for company %s (ad %s)", ad.company_id, ad.id)
        return

    meta = MetaAdsService(access_token=conn.access_token, ad_account_id=conn.ad_account_id)
    target = "PAUSED" if should_pause else "ACTIVE"
    label = "paused" if should_pause else "resumed"

    try:
        await meta.update_ad_status(campaign_id, target)
        if adset_id:
            await meta.update_ad_status(adset_id, target)

        logger.info("Scheduler: %s ad=%s campaign=%s adset=%s", label, ad.id, campaign_id, adset_id or "—")

        updated = dict(bot_config)
        updated["last_scheduler_action"] = label
        updated["last_scheduler_at"] = now.isoformat()
        ad.bot_config = updated
        flag_modified(ad, "bot_config")
        await session.commit()

    except Exception as exc:
        logger.error("Scheduler: failed to %s ad %s: %s", label, ad.id, exc)
        # A failed commit poisons the session — roll back so the remaining
        # ads in this cycle are not lost to PendingRollbackError.
        await session.rollback()


async def _run_due(now: datetime) -> None:
    """Fire all ads whose transition is due right now."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(select(Advertisement).where(Advertisement.bot_config.is_not(None)))
            for ad in result.scalars().all():
                if isinstance(ad.bot_config, dict) and "pause_schedule" in ad.bot_config:
                    try:
                        await _process_ad(session, ad, now)
                    except Exception as exc:
                        logger.error("Scheduler: unhandled error for ad %s: %s", ad.id, exc)
    except Exception as exc:
        logger.error("Scheduler: session error: %s", exc)


async def _next_sleep(now: datetime) -> float:
    """
    Compute seconds until the nearest upcoming transition across all scheduled ads.
    Falls back to _NO_SCHEDULE_POLL if nothing is configured.
    """
    earliest: float | None = None
    try:
        async with async_session_factory() as session:
            # Only id + bot_config are needed — avoids dragging strategy_json /
            # output_files blobs for every ad on every cycle.
            result = await session.execute(
                select(Advertisement.id, Advertisement.bot_config).where(Advertisement.bot_config.is_not(None))
            )
            for ad_id, bot_config in result.all():
                try:
                    bc = bot_config if isinstance(bot_config, dict) else {}
                    sched = bc.get("pause_schedule")
                    if not sched:
                        continue
                    zone = zone_from_config(bc)
                    secs = _next_transition_seconds_for_schedule(sched, now.astimezone(zone))
                    if secs is not None and secs > 0:
                        earliest = secs if earliest is None else min(earliest, secs)
                except Exception as inner_exc:
                    # Skip ads with malformed bot_config
                    logger.debug("Scheduler: skipping ad %s due to error: %s", ad_id, inner_exc)
                    continue
    except Exception as exc:
        import traceback

        logger.error("Scheduler: error computing next sleep: %s\n%s", exc, traceback.format_exc())

    if earliest is None:
        return float(_NO_SCHEDULE_POLL)

    # 5-second buffer so we land just after the boundary minute ticks over
    return max(1.0, earliest + 5)


async def run_pause_scheduler() -> None:
    """
    Event-driven scheduler: sleeps until the next pause/resume boundary,
    fires exactly then, then recalculates the next wake-up.
    Only calls Meta API when a state change is actually due.
    """
    logger.info("Pause scheduler started (event-driven)")
    while True:
        now = datetime.now(UTC)
        sleep_secs = await _next_sleep(now)
        logger.debug("Scheduler: next check in %.0fs (%.1f min)", sleep_secs, sleep_secs / 60)
        await asyncio.sleep(sleep_secs)
        await _run_due(datetime.now(UTC))
