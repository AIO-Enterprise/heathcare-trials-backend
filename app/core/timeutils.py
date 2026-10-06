"""
Campaign-timezone helpers.

Convention used across the backend: booking slots, pause-schedule windows and
reminder horizons are stored as *naive datetimes in campaign-local wall time*.

Every comparison against "now" must therefore be done in the campaign's
timezone — never against server-local time or UTC. The helpers below are the
single source of truth for that conversion.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings

# Widest real-world UTC offsets span -12:00 .. +14:00. Used to widen SQL
# windows before per-row timezone filtering.
MAX_TZ_SPAN_HOURS = 15


def get_zone(tz_name: str | None) -> ZoneInfo:
    """Resolve an IANA tz name, falling back to DEFAULT_CAMPAIGN_TIMEZONE."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    return ZoneInfo(settings.DEFAULT_CAMPAIGN_TIMEZONE)


def zone_from_config(bot_config: dict | None) -> ZoneInfo:
    """Campaign timezone from a raw bot_config mapping (or None)."""
    cfg = bot_config if isinstance(bot_config, dict) else {}
    return get_zone(cfg.get("timezone"))


def ad_zone(ad) -> ZoneInfo:
    """Campaign timezone for an Advertisement (bot_config.timezone or default)."""
    return zone_from_config(getattr(ad, "bot_config", None))


def naive_now(zone: ZoneInfo) -> datetime:
    """Current wall-clock time as a naive datetime in `zone`.

    Directly comparable with stored slot_datetime values.
    """
    return datetime.now(zone).replace(tzinfo=None)


def today_in(zone: ZoneInfo) -> date:
    """Today's date in `zone`."""
    return datetime.now(zone).date()


def to_naive_local(dt: datetime, zone: ZoneInfo) -> datetime:
    """Normalize a client-supplied datetime to naive campaign-local wall time.

    Aware input is converted into `zone` then stripped; naive input is taken
    to already be campaign-local and passes through unchanged.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(zone).replace(tzinfo=None)
