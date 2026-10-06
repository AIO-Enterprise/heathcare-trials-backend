"""
Stale-`generating` sweeper.

Background AI tasks (strategy / creatives / shorts / website) normally reset
the advertisement themselves — either to the next status or back to DRAFT with
`generation_error` set. But if a task dies between status flips, or a worker
is OOM-killed mid-run, the ad stays in GENERATING forever and the UI polls
infinitely. This sweeper reaps those rows.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.database import async_session_factory
from app.models.base import AdStatus
from app.models.models import Advertisement

logger = logging.getLogger(__name__)

# How long an ad may sit in GENERATING before it is considered stuck.
STALE_AFTER = timedelta(minutes=30)
SCAN_INTERVAL = 300  # seconds


async def run_stale_generating_sweeper() -> None:
    while True:
        try:
            cutoff = datetime.utcnow() - STALE_AFTER
            async with async_session_factory() as db:
                result = await db.execute(
                    select(Advertisement).where(
                        Advertisement.status == AdStatus.GENERATING,
                        Advertisement.updated_at < cutoff,
                    )
                )
                stale = result.scalars().all()
                for ad in stale:
                    ad.status = AdStatus.DRAFT
                    ad.generation_error = "Generation timed out after 30 minutes — please retry."
                if stale:
                    await db.commit()
                    logger.warning("Stale-generating sweeper: reaped %d stuck ads", len(stale))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale-generating sweeper error")

        await asyncio.sleep(SCAN_INTERVAL)
