"""
Google Ads management and analytics: list/pause/resume/delete ads,
budget updates, and insights — mirrors ads_meta.py for Google Ads.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.security import get_company_ad_or_404, require_roles
from app.db.database import get_db
from app.models.models import AdAnalytics, PlatformConnection, User, UserRole

router = APIRouter(prefix="/advertisements", tags=["Google Ads Management"])
logger = logging.getLogger(__name__)


def _get_google_conn_and_ids(ad, conn):
    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    campaign_id = bot.get("google_campaign_id")
    ad_group_id = bot.get("google_ad_group_id")
    ad_ids = bot.get("google_ad_ids", [])

    if not campaign_id:
        raise HTTPException(
            status_code=400,
            detail="No Google Ads campaign found for this advertisement. Distribute it to Google Ads first.",
        )
    if not conn:
        raise HTTPException(
            status_code=400,
            detail="Google Ads account not connected. Connect it in Platform Settings.",
        )
    if not conn.ad_account_id:
        raise HTTPException(
            status_code=400,
            detail="Select a Google Ads customer account in Platform Settings.",
        )
    return campaign_id, ad_group_id, ad_ids


async def _load_google_conn(db: AsyncSession, company_id: str):
    result = await db.execute(
        select(PlatformConnection).where(
            PlatformConnection.company_id == company_id,
            PlatformConnection.platform == "google_ads",
        )
    )
    return result.scalar_one_or_none()


async def _fresh_access_token(conn) -> str:
    from app.services.ads.google_ads_service import GoogleAdsService

    try:
        return await asyncio.to_thread(
            GoogleAdsService.refresh_access_token,
            refresh_token=conn.access_token,
            client_id=settings.GOOGLE_ADS_CLIENT_ID,
            client_secret=settings.GOOGLE_ADS_CLIENT_SECRET,
        )
    except Exception as exc:
        logger.error("Failed to refresh Google access token: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to refresh Google access token. Try reconnecting in Platform Settings.",
        )


@router.get("/{ad_id}/google-ads")
async def list_google_ads(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    List all Google Ads for this campaign, fetched live from the Google Ads API.
    Returns id, name, status (ENABLED/PAUSED/REMOVED), and creative details.
    """
    from app.services.ads.google_ads_service import GoogleAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_google_conn(db, user.company_id)
    campaign_id, ad_group_id, _ = _get_google_conn_and_ids(ad, conn)

    access_token = await _fresh_access_token(conn)
    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        ads = await asyncio.to_thread(svc.list_ads, campaign_id)
    except Exception as exc:
        logger.error("Google list_ads failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not list Google Ads — see server logs")

    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    return {
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "ads_manager_url": bot.get("google_ads_manager_url"),
        "ads": ads,
    }


@router.patch("/{ad_id}/google-ads/{google_ad_id}")
async def update_google_ad(
    ad_id: str,
    google_ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Update a Google ad.

    Toggle status:
      {"status": "ENABLED" | "PAUSED"}

    Update creative copy (all fields required):
      {"headline": "...", "description": "...", "final_url": "https://..."}
    """
    from app.services.ads.google_ads_service import GoogleAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_google_conn(db, user.company_id)
    campaign_id, ad_group_id, _ = _get_google_conn_and_ids(ad, conn)

    access_token = await _fresh_access_token(conn)
    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        status = (body.get("status") or "").upper()
        if status in ("ENABLED", "PAUSED"):
            result = await asyncio.to_thread(svc.update_ad_status, google_ad_id, status)
            if status == "ENABLED" and campaign_id:
                try:
                    await asyncio.to_thread(svc.update_campaign_status, campaign_id, "ENABLED")
                except Exception as e:
                    logger.warning("Could not activate campaign %s: %s", campaign_id, e)
                if ad_group_id:
                    try:
                        await asyncio.to_thread(svc.update_ad_group_status, ad_group_id, "ENABLED")
                    except Exception as e:
                        logger.warning("Could not activate ad group %s: %s", ad_group_id, e)
        elif body.get("headline") or body.get("description"):
            result = await asyncio.to_thread(
                svc.update_ad_copy,
                ad_group_ad_resource=f"customers/{conn.ad_account_id.replace('-', '')}/adGroupAds/{ad_group_id}~{google_ad_id}",
                headline=body.get("headline", ""),
                description=body.get("description", ""),
                final_url=body.get("final_url", ""),
            )
        else:
            raise HTTPException(
                status_code=422, detail="Provide status (ENABLED|PAUSED) or creative fields (headline, description)."
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Google update_ad failed for ad %s: %s", google_ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not update Google Ad — see server logs")

    return result


@router.post("/{ad_id}/google-budget")
async def update_google_budget(
    ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Update the daily budget on the Google Ads campaign.
    Body: {"daily_budget_usd": 2.50}
    """
    from app.services.ads.google_ads_service import GoogleAdsService

    daily_budget_usd = body.get("daily_budget_usd")
    if daily_budget_usd is None:
        raise HTTPException(status_code=422, detail="daily_budget_usd is required")
    try:
        daily_budget_usd = float(daily_budget_usd)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="daily_budget_usd must be a number")
    if daily_budget_usd <= 0:
        raise HTTPException(status_code=422, detail="daily_budget_usd must be positive")

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_google_conn(db, user.company_id)
    _get_google_conn_and_ids(ad, conn)

    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    campaign_id = bot.get("google_campaign_id")
    if not campaign_id:
        raise HTTPException(status_code=400, detail="No Google Ads campaign found. Upload to Google Ads first.")

    access_token = await _fresh_access_token(conn)
    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        result = await asyncio.to_thread(svc.update_campaign_budget, campaign_id, daily_budget_usd)
    except Exception as exc:
        logger.error("Google update_budget failed for campaign %s: %s", campaign_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not update Google budget — see server logs")

    ad.budget = daily_budget_usd
    await db.flush()

    return {"campaign_id": campaign_id, "daily_budget_usd": daily_budget_usd, "result": result}


@router.delete("/{ad_id}/google-ads/{google_ad_id}", status_code=204)
async def delete_google_ad(
    ad_id: str,
    google_ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """Remove a Google ad (sets status to REMOVED). Also removes from stored google_ad_ids."""
    from app.services.ads.google_ads_service import GoogleAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_google_conn(db, user.company_id)
    _get_google_conn_and_ids(ad, conn)

    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    ad_group_id = bot.get("google_ad_group_id", "")

    access_token = await _fresh_access_token(conn)
    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        await asyncio.to_thread(svc.remove_ad, ad_group_id=ad_group_id, ad_id=google_ad_id)
    except Exception as exc:
        logger.error("Google remove_ad failed for ad %s: %s", google_ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not remove Google Ad — see server logs")

    updated_bot = dict(bot)
    updated_bot["google_ad_ids"] = [i for i in bot.get("google_ad_ids", []) if i != google_ad_id]
    ad.bot_config = updated_bot
    flag_modified(ad, "bot_config")
    await db.commit()


@router.get("/{ad_id}/google-insights")
async def get_google_insights(
    ad_id: str,
    date_range: str = "LAST_30_DAYS",
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch daily performance insights from Google Ads Reporting API and persist in AdAnalytics.
    Each day's row is upserted by (advertisement_id, date_label, source='google_ads').

    date_range: LAST_7_DAYS | LAST_14_DAYS | LAST_30_DAYS | LAST_90_DAYS
    """
    from app.services.ads.google_ads_service import GoogleAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_google_conn(db, user.company_id)
    campaign_id, _, _ = _get_google_conn_and_ids(ad, conn)

    access_token = await _fresh_access_token(conn)
    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        raw_rows = await asyncio.to_thread(svc.get_campaign_insights, campaign_id, date_range=date_range)
    except Exception as exc:
        logger.error("Google get_insights failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not fetch Google insights — see server logs")

    result_rows = []
    # Batch-fetch existing rows once — avoids one SELECT per insight day.
    date_labels = [r.get("date", "") for r in raw_rows]
    existing_rows_result = await db.execute(
        select(AdAnalytics).where(
            AdAnalytics.advertisement_id == ad_id,
            AdAnalytics.date_label.in_(date_labels),
            AdAnalytics.source == "google_ads",
        )
    )
    existing_by_label: dict = {e.date_label: e for e in existing_rows_result.scalars().all()}

    for row in raw_rows:
        date_label = row.get("date", "")
        impressions = int(row.get("impressions", 0) or 0)
        clicks = int(row.get("clicks", 0) or 0)
        spend = float(row.get("cost_micros", 0) or 0) / 1_000_000
        conversions = int(row.get("conversions", 0) or 0)
        ctr = float(row.get("ctr", 0) or 0) * 100  # Google returns 0–1 fraction
        cpc_micros = float(row.get("average_cpc", 0) or 0)
        cpc = cpc_micros / 1_000_000
        cpm_micros = float(row.get("average_cpm", 0) or 0)
        cpm = cpm_micros / 1_000_000
        click_rate = round(ctr, 4)

        existing = existing_by_label.get(date_label)
        if existing:
            existing.impressions = impressions
            existing.clicks = clicks
            existing.views = impressions
            existing.click_rate = click_rate
            existing.conversions = conversions
            existing.spend = spend
            existing.cpm = cpm
            existing.cost_per_click = cpc
        else:
            db.add(
                AdAnalytics(
                    advertisement_id=ad_id,
                    date_label=date_label,
                    source="google_ads",
                    impressions=impressions,
                    clicks=clicks,
                    views=impressions,
                    click_rate=click_rate,
                    conversions=conversions,
                    spend=spend,
                    cpm=cpm,
                    cost_per_click=cpc,
                )
            )

        result_rows.append(
            {
                "date": date_label,
                "impressions": impressions,
                "clicks": clicks,
                "spend": spend,
                "cpm": cpm,
                "cpc": cpc,
                "conversions": conversions,
                "click_rate": click_rate,
            }
        )

    await db.commit()
    return {"date_range": date_range, "rows": result_rows, "campaign_id": campaign_id}
