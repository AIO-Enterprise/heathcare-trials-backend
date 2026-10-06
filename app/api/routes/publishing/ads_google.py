"""
Google Ads distribution: publish campaign creatives to Google Ads (Display Network).

Credentials (refresh_token, customer_id) are read from the stored PlatformConnection
for this company — connect once via OAuth in Platform Settings.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.security import get_company_ad_or_404, require_roles
from app.db.database import get_db
from app.models.models import AdStatus, PlatformConnection, User, UserRole
from app.services.storage.extractor import BACKEND_ROOT

router = APIRouter(prefix="/advertisements", tags=["Google Ads"])
logger = logging.getLogger(__name__)


async def _load_google_conn(db: AsyncSession, company_id: str):
    result = await db.execute(
        select(PlatformConnection).where(
            PlatformConnection.company_id == company_id,
            PlatformConnection.platform == "google_ads",
        )
    )
    return result.scalar_one_or_none()


@router.post("/{ad_id}/distribute-google")
async def distribute_to_google(
    ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish the campaign's generated ad creatives to Google Ads (Display Network).

    Credentials (refresh_token, customer_id) are read from the stored
    PlatformConnection — connect once via OAuth in Platform Settings.

    Expected body:
      config:
        destination_url    : URL the ad clicks lead to (required)
        daily_budget       : daily budget in USD  (e.g. 10.0)
        targeting_countries: comma-separated ISO country codes  (e.g. "US,AU")
        selected_creatives : list of creative indexes to publish
    """
    from app.services.ads.google_ads_service import GoogleAdsService

    if not settings.GOOGLE_ADS_CLIENT_ID or not settings.GOOGLE_ADS_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Ads credentials are not configured. "
                "Set GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET in your .env file."
            ),
        )
    if not settings.GOOGLE_ADS_DEVELOPER_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_ADS_DEVELOPER_TOKEN is not configured in your .env file.",
        )

    cfg = body.get("config") or {}
    destination_url = (cfg.get("destination_url") or "").strip()
    daily_budget_str = str(cfg.get("daily_budget") or "10").strip()
    countries_str = (cfg.get("targeting_countries") or "AU").strip()
    selected_creatives = cfg.get("selected_creatives") or []

    conn = await _load_google_conn(db, user.company_id)

    missing = []
    if not conn:
        missing.append("Google Ads connection (connect in Platform Settings)")
    elif not conn.ad_account_id:
        missing.append("customer_id (select a customer account in Platform Settings)")
    if not destination_url:
        missing.append("destination_url")

    if missing:
        detail = f"Missing required fields: {', '.join(missing)}."
        if not conn:
            detail += " Connect your Google Ads account in Platform Settings first."
        raise HTTPException(status_code=422, detail=detail)

    try:
        daily_budget = float(daily_budget_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="daily_budget must be a number")

    targeting_countries = [c.strip().upper() for c in countries_str.split(",") if c.strip()]

    ad = await get_company_ad_or_404(db, user, ad_id)
    if ad.status not in (AdStatus.APPROVED, AdStatus.PUBLISHED):
        raise HTTPException(
            status_code=400,
            detail="Campaign must be approved or published before distributing to Google Ads",
        )
    if not ad.output_files:
        raise HTTPException(
            status_code=400,
            detail="No ad creatives found. Generate creatives first.",
        )

    # Refresh access_token from stored refresh_token
    try:
        access_token = await asyncio.to_thread(
            GoogleAdsService.refresh_access_token,
            refresh_token=conn.access_token,
            client_id=settings.GOOGLE_ADS_CLIENT_ID,
            client_secret=settings.GOOGLE_ADS_CLIENT_SECRET,
        )
    except AppError:
        raise
    except Exception:
        logger.exception("Google token refresh failed for company %s", user.company_id)
        raise HTTPException(
            status_code=502,
            detail="Failed to refresh Google access token. Try reconnecting in Platform Settings.",
        )

    svc = GoogleAdsService(
        access_token=access_token,
        customer_id=conn.ad_account_id,
        developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
    )

    try:
        result = await svc.publish_campaign(
            campaign_name=ad.title,
            creatives=ad.output_files,
            selected_indices=[int(i) for i in selected_creatives],
            daily_budget_usd=daily_budget,
            destination_url=destination_url,
            targeting_countries=targeting_countries,
            backend_root=str(BACKEND_ROOT),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AppError:
        raise
    except Exception as exc:
        logger.error("Google Ads distribute failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Google Ads publish failed — see server logs")

    # Persist Google campaign ID in bot_config
    existing = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
    existing["google_campaign_id"] = result["campaign_id"]
    existing["google_ad_group_id"] = result["ad_group_id"]
    existing["google_ad_ids"] = result["ad_ids"]
    ad.bot_config = existing
    flag_modified(ad, "bot_config")

    await db.commit()
    await db.refresh(ad)

    return result
