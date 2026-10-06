"""
Google Ads Pause Scheduler
Mirrors meta_scheduler.py — runs as an asyncio background task.

Reads pause_schedule from ad.bot_config (same format as Meta):
  New: list of { id, days[], timeFrom, timeTo }
  Legacy: { pause_days[], pause_hours: "HH:MM-HH:MM" }

Pauses/resumes Google Ads campaigns + ad groups on schedule.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.timeutils import ad_zone, zone_from_config
from app.db.database import async_session_factory
from app.models.models import Advertisement, PlatformConnection

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
_NO_SCHEDULE_POLL = 300


def _parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _normalize_windows(pause_schedule) -> list[dict]:
    if isinstance(pause_schedule, list):
        return pause_schedule
    if isinstance(pause_schedule, dict):
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
    days = window.get("days") or []
    wanted_days = {_DAY_MAP[d] for d in days if d in _DAY_MAP}
    if not wanted_days or now.weekday() not in wanted_days:
        return False
    try:
        from_min = _parse_hhmm(window.get("timeFrom") or "00:00")
        to_min = _parse_hhmm(window.get("timeTo") or "00:00")
    except (ValueError, IndexError):
        return True
    now_min = now.hour * 60 + now.minute
    if from_min <= to_min:
        return from_min <= now_min <= to_min
    return now_min >= from_min or now_min <= to_min


def _any_window_active(windows: list[dict], now: datetime) -> bool:
    return any(_window_active(w, now) for w in windows)


def _next_window_transition_seconds(window: dict, now: datetime) -> float | None:
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
        if candidate.weekday() not in wanted_weekdays:
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
    windows = _normalize_windows(pause_schedule)
    earliest: float | None = None
    for w in windows:
        secs = _next_window_transition_seconds(w, now)
        if secs is not None and secs > 0:
            earliest = secs if earliest is None else min(earliest, secs)
    return earliest


async def _get_google_conn(session, company_id: str):
    result = await session.execute(
        select(PlatformConnection).where(
            PlatformConnection.company_id == company_id,
            PlatformConnection.platform == "google_ads",
        )
    )
    return result.scalars().first()


async def _process_ad(session, ad: Advertisement, now: datetime) -> None:
    if (
        not settings.GOOGLE_ADS_CLIENT_ID
        or not settings.GOOGLE_ADS_CLIENT_SECRET
        or not settings.GOOGLE_ADS_DEVELOPER_TOKEN
    ):
        return

    bot_config = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    sched = bot_config.get("pause_schedule")
    campaign_id = bot_config.get("google_campaign_id") or ""
    ad_group_id = bot_config.get("google_ad_group_id") or ""
    last_action = bot_config.get("google_last_scheduler_action") or ""

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

    conn = await _get_google_conn(session, ad.company_id)
    if not conn or not conn.access_token or not conn.ad_account_id:
        logger.warning("Google Scheduler: no connection for company %s (ad %s)", ad.company_id, ad.id)
        return

    from app.services.ads.google_ads_service import GoogleAdsService

    try:
        access_token = await asyncio.to_thread(
            GoogleAdsService.refresh_access_token,
            refresh_token=conn.access_token,
            client_id=settings.GOOGLE_ADS_CLIENT_ID,
            client_secret=settings.GOOGLE_ADS_CLIENT_SECRET,
        )
    except Exception as exc:
        logger.error("Google Scheduler: token refresh failed for company %s: %s", ad.company_id, exc)
        return

    svc = GoogleAdsService(
        access_token=access_token, customer_id=conn.ad_account_id, developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN
    )
    target = "PAUSED" if should_pause else "ENABLED"
    label = "paused" if should_pause else "resumed"

    try:
        await asyncio.to_thread(svc.update_campaign_status, campaign_id, target)
        if ad_group_id:
            await asyncio.to_thread(svc.update_ad_group_status, ad_group_id, target)

        logger.info("Google Scheduler: %s ad=%s campaign=%s", label, ad.id, campaign_id)

        updated = dict(bot_config)
        updated["google_last_scheduler_action"] = label
        updated["google_last_scheduler_at"] = now.isoformat()
        ad.bot_config = updated
        flag_modified(ad, "bot_config")
        await session.commit()

    except Exception as exc:
        logger.error("Google Scheduler: failed to %s ad %s: %s", label, ad.id, exc)
        # A failed commit poisons the session — roll back so the remaining
        # ads in this cycle are not lost to PendingRollbackError.
        await session.rollback()


async def _run_due(now: datetime) -> None:
    try:
        async with async_session_factory() as session:
            result = await session.execute(select(Advertisement).where(Advertisement.bot_config.is_not(None)))
            for ad in result.scalars().all():
                bc = ad.bot_config if isinstance(ad.bot_config, dict) else {}
                if "pause_schedule" in bc and bc.get("google_campaign_id"):
                    try:
                        await _process_ad(session, ad, now)
                    except Exception as exc:
                        logger.error("Google Scheduler: unhandled error for ad %s: %s", ad.id, exc)
    except Exception as exc:
        logger.error("Google Scheduler: session error: %s", exc)


async def _next_sleep(now: datetime) -> float:
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
                    if not sched or not bc.get("google_campaign_id"):
                        continue
                    secs = _next_transition_seconds_for_schedule(sched, now.astimezone(zone_from_config(bc)))
                    if secs is not None and secs > 0:
                        earliest = secs if earliest is None else min(earliest, secs)
                except Exception as inner_exc:
                    logger.debug("Google Scheduler: skipping ad %s: %s", ad_id, inner_exc)
    except Exception as exc:
        logger.error("Google Scheduler: error computing next sleep: %s", exc)

    return max(1.0, (earliest + 5)) if earliest is not None else float(_NO_SCHEDULE_POLL)


async def run_google_pause_scheduler() -> None:
    """Event-driven scheduler for Google Ads pause windows."""
    logger.info("Google Ads pause scheduler started (event-driven)")
    while True:
        now = datetime.now(UTC)
        sleep_secs = await _next_sleep(now)
        logger.debug("Google Scheduler: next check in %.0fs", sleep_secs)
        await asyncio.sleep(sleep_secs)
        await _run_due(datetime.now(UTC))
