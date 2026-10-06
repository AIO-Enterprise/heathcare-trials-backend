"""
Meta Ads distribution and management: distribute, list/update/delete ads,
budget updates, and insights.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.exceptions import AppError
from app.core.security import get_company_ad_or_404, require_roles
from app.db.database import get_db
from app.models.models import AdStatus, PlatformConnection, User, UserRole
from app.services.storage.extractor import BACKEND_ROOT

router = APIRouter(prefix="/advertisements", tags=["Meta Ads"])
logger = logging.getLogger(__name__)


def _get_meta_conn_and_ids(ad, conn):
    """
    Extract Meta campaign_id, ad_ids, access_token, ad_account_id, page_id from an ad.
    Raises HTTPException if anything required is missing.
    """
    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    campaign_id = bot.get("meta_campaign_id")
    ad_ids = bot.get("meta_ad_ids", [])

    if not campaign_id:
        raise HTTPException(
            status_code=400,
            detail="No Meta campaign found for this advertisement. Distribute it to Meta first.",
        )
    if not conn:
        raise HTTPException(
            status_code=400,
            detail="Meta account not connected. Connect it in Platform Settings.",
        )
    if not conn.ad_account_id or not conn.page_id:
        raise HTTPException(
            status_code=400,
            detail="Select an Ad Account and Facebook Page in Platform Settings.",
        )
    return campaign_id, ad_ids, conn.access_token, conn.ad_account_id, conn.page_id


async def _load_meta_conn(db: AsyncSession, company_id: str):
    conn_result = await db.execute(
        select(PlatformConnection).where(
            PlatformConnection.company_id == company_id,
            PlatformConnection.platform == "meta",
        )
    )
    return conn_result.scalar_one_or_none()


@router.post("/{ad_id}/distribute")
async def distribute_to_meta(
    ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish the campaign's generated ad creatives to Meta (Facebook/Instagram)
    via the Marketing API.

    Credentials (access_token, ad_account_id, page_id) are read from the stored
    PlatformConnection for this company — connect once via OAuth in Platform Settings.

    Expected body:
      platform          : "meta"
      config:
        destination_url    : URL the ad clicks lead to
        daily_budget       : daily budget in USD  (e.g. 10.0)
        targeting_countries: comma-separated ISO country codes  (e.g. "US,GB")
        selected_creatives : list of creative indexes to publish
    """
    from app.services.ads.meta_ads_service import MetaAdsService

    platform = (body.get("platform") or "").lower()
    if platform != "meta":
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")

    cfg = body.get("config") or {}
    destination_url = (cfg.get("destination_url") or "").strip()
    daily_budget_str = str(cfg.get("daily_budget") or "10").strip()
    countries_str = (cfg.get("targeting_countries") or "AU").strip()
    selected_creatives = cfg.get("selected_creatives") or []
    display_url = (cfg.get("display_url") or "").strip() or None
    addon_type = (cfg.get("addon_type") or "").strip() or None
    addon_phone = (cfg.get("addon_phone") or "").strip() or None
    start_time = (cfg.get("start_time") or "").strip() or None

    try:
        age_min = int(cfg.get("age_min") or 18)
        age_max = int(cfg.get("age_max") or 65)
    except (TypeError, ValueError):
        age_min, age_max = 18, 65

    conn = await _load_meta_conn(db, user.company_id)

    access_token = (cfg.get("access_token") or "").strip() or (conn.access_token if conn else "")
    ad_account_id = (cfg.get("ad_account_id") or "").strip() or (conn.ad_account_id if conn else "")
    page_id = (cfg.get("page_id") or "").strip() or (conn.page_id if conn else "")

    missing = [
        name
        for name, val in [
            ("access_token", access_token),
            ("ad_account_id", ad_account_id),
            ("page_id", page_id),
            ("destination_url", destination_url),
        ]
        if not val
    ]
    if missing:
        detail = f"Missing required Meta config fields: {', '.join(missing)}"
        if not conn:
            detail += ". Connect your Meta account in Platform Settings first."
        elif not conn.ad_account_id or not conn.page_id:
            detail += ". Select an Ad Account and Facebook Page in Platform Settings."
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
            detail="Campaign must be approved or published before distributing to Meta",
        )
    if not ad.output_files:
        raise HTTPException(
            status_code=400,
            detail="No ad creatives found. Generate creatives first.",
        )

    if addon_type == "phone" and not addon_phone:
        bc = ad.bot_config if isinstance(ad.bot_config, dict) else {}
        addon_phone = bc.get("voice_phone_number") or None
        if not addon_phone:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No voicebot phone number found. "
                    "Provision the voice agent first so its number can be used for the ad CTA."
                ),
            )

    existing_bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    existing_campaign_id = existing_bot.get("meta_campaign_id") or None

    async def _persist_checkpoint(partial: dict) -> None:
        """Persist publish progress so a crash mid-publish never duplicates campaigns."""
        bot = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
        bot["meta_campaign_id"] = partial["campaign_id"]
        if partial.get("adset_id"):
            bot["meta_adset_id"] = partial["adset_id"]
        bot["meta_ad_ids"] = partial.get("ad_ids", [])
        ad.bot_config = bot
        flag_modified(ad, "bot_config")
        await db.commit()

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        meta_result = await svc.publish_campaign(
            campaign_name=ad.title,
            page_id=page_id,
            creatives=ad.output_files,
            selected_indices=[int(i) for i in selected_creatives],
            daily_budget_usd=daily_budget,
            destination_url=destination_url,
            targeting_countries=targeting_countries,
            backend_root=str(BACKEND_ROOT),
            display_url=display_url,
            addon_type=addon_type,
            addon_phone=addon_phone,
            existing_campaign_id=existing_campaign_id,
            age_min=age_min,
            age_max=age_max,
            start_time=start_time,
            on_checkpoint=_persist_checkpoint,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AppError:
        raise
    except Exception as exc:
        logger.error("Meta distribute failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta publish failed — see server logs")
    finally:
        await svc.close()

    existing_meta = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
    existing_meta["meta_campaign_id"] = meta_result["campaign_id"]
    existing_meta["meta_adset_id"] = meta_result["adset_id"]
    existing_meta["meta_ad_ids"] = meta_result["ad_ids"]
    existing_meta["meta_manager_url"] = meta_result["ads_manager_url"]
    ad.bot_config = existing_meta
    flag_modified(ad, "bot_config")
    await db.commit()

    return meta_result


@router.get("/{ad_id}/meta-ads")
async def list_meta_ads(
    ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    List all Meta ads for this campaign, fetched live from the Meta API.
    Returns id, name, status (ACTIVE/PAUSED/DELETED), and creative details.
    """
    from app.services.ads.meta_ads_service import MetaAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_meta_conn(db, user.company_id)
    campaign_id, _, access_token, ad_account_id, _ = _get_meta_conn_and_ids(ad, conn)

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        ads = await svc.get_ads(campaign_id)
    except Exception as exc:
        logger.error("Meta get_ads failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta API error - see server logs")
    finally:
        await svc.close()

    bc_list = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    return {
        "campaign_id": campaign_id,
        "adset_id": bc_list.get("meta_adset_id"),
        "ads": ads,
    }


@router.patch("/{ad_id}/meta-ads/{meta_ad_id}")
async def update_meta_ad(
    ad_id: str,
    meta_ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Update a Meta ad.

    To toggle status:
      {"status": "ACTIVE" | "PAUSED"}

    To update creative copy (all fields required when editing creative):
      {"headline": "...", "body": "...", "cta_type": "LEARN_MORE",
       "link_url": "https://...", "image_hash": "...", "page_id": "..."}
    """
    from app.services.ads.meta_ads_service import MetaAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_meta_conn(db, user.company_id)
    _, _, access_token, ad_account_id, page_id_default = _get_meta_conn_and_ids(ad, conn)

    bot = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    adset_id = bot.get("meta_adset_id")
    campaign_id_stored = bot.get("meta_campaign_id")

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        status = (body.get("status") or "").upper()
        if status in ("ACTIVE", "PAUSED"):
            result = await svc.update_ad_status(meta_ad_id, status)
            if status == "ACTIVE":
                if adset_id:
                    try:
                        await svc.update_ad_status(adset_id, "ACTIVE")
                    except Exception as e:
                        logger.warning("Could not activate ad set %s: %s", adset_id, e)
                if campaign_id_stored:
                    try:
                        await svc.update_ad_status(campaign_id_stored, "ACTIVE")
                    except Exception as e:
                        logger.warning("Could not activate campaign %s: %s", campaign_id_stored, e)
        elif body.get("headline") or body.get("body"):
            image_hash = body.get("image_hash", "")
            if not image_hash:
                raise HTTPException(status_code=422, detail="image_hash is required when updating creative text.")
            result = await svc.update_ad_creative(
                meta_ad_id=meta_ad_id,
                page_id=body.get("page_id") or page_id_default,
                image_hash=image_hash,
                headline=body.get("headline", ""),
                body=body.get("body", ""),
                cta_type=(body.get("cta_type") or "BOOK_NOW").upper(),
                link_url=body.get("link_url", ""),
                ad_name=ad.title,
            )
        else:
            raise HTTPException(
                status_code=422, detail="Provide status (ACTIVE|PAUSED) or creative fields (headline, body)."
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Meta update_ad failed for meta_ad %s: %s", meta_ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta API error - see server logs")
    finally:
        await svc.close()

    return result


@router.post("/{ad_id}/meta-budget")
async def update_meta_budget(
    ad_id: str,
    body: dict,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Update the daily budget on the campaign's Meta ad set.
    Body: {"daily_budget_usd": 2.50}
    """
    from app.services.ads.meta_ads_service import MetaAdsService

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
    conn = await _load_meta_conn(db, user.company_id)
    _, _, access_token, ad_account_id, _ = _get_meta_conn_and_ids(ad, conn)

    bc_budget = ad.bot_config if isinstance(ad.bot_config, dict) else {}
    adset_id = bc_budget.get("meta_adset_id")
    if not adset_id:
        raise HTTPException(status_code=400, detail="No Meta ad set found for this campaign. Upload to Meta first.")

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        result = await svc.update_adset_budget(adset_id, daily_budget_usd)
    except Exception as exc:
        logger.error("Meta update_adset_budget failed for adset %s: %s", adset_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta API error - see server logs")
    finally:
        await svc.close()

    ad.budget = daily_budget_usd
    await db.flush()

    return {"adset_id": adset_id, "daily_budget_usd": daily_budget_usd, "meta_result": result}


@router.delete("/{ad_id}/meta-ads/{meta_ad_id}", status_code=204)
async def delete_meta_ad(
    ad_id: str,
    meta_ad_id: str,
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """Delete a Meta ad. Also removes it from the campaign's stored meta_ad_ids list."""
    from app.services.ads.meta_ads_service import MetaAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_meta_conn(db, user.company_id)
    _, _, access_token, ad_account_id, _ = _get_meta_conn_and_ids(ad, conn)

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        await svc.delete_ad(meta_ad_id)
    except Exception as exc:
        logger.error("Meta delete_ad failed for meta_ad %s: %s", meta_ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta API error - see server logs")
    finally:
        await svc.close()

    bot = dict(ad.bot_config if isinstance(ad.bot_config, dict) else {})
    existing_ids = bot.get("meta_ad_ids", [])
    bot["meta_ad_ids"] = [i for i in existing_ids if i != meta_ad_id]
    ad.bot_config = bot
    flag_modified(ad, "bot_config")
    await db.commit()


@router.get("/{ad_id}/meta-insights")
async def get_meta_insights(
    ad_id: str,
    date_preset: str = "last_30d",
    user: User = Depends(require_roles([UserRole.PUBLISHER])),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch daily performance insights from Meta and persist them in AdAnalytics.
    Each day's row is upserted by (advertisement_id, date_label, source='meta').

    date_preset: last_7d | last_14d | last_30d | last_90d
    """
    from app.models.models import AdAnalytics
    from app.services.ads.meta_ads_service import MetaAdsService

    ad = await get_company_ad_or_404(db, user, ad_id)
    conn = await _load_meta_conn(db, user.company_id)
    campaign_id, _, access_token, ad_account_id, _ = _get_meta_conn_and_ids(ad, conn)

    svc = MetaAdsService(access_token=access_token, ad_account_id=ad_account_id)
    try:
        raw_rows = await svc.get_insights(campaign_id, date_preset=date_preset)
    except Exception as exc:
        logger.error("Meta get_insights failed for ad %s: %s", ad_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Meta API error - see server logs")
    finally:
        await svc.close()

    result_rows = []
    # Batch-fetch existing rows once — avoids one SELECT per insight day.
    date_labels = [r.get("date_start", "") for r in raw_rows]
    existing_rows = await db.execute(
        select(AdAnalytics).where(
            AdAnalytics.advertisement_id == ad_id,
            AdAnalytics.date_label.in_(date_labels),
            AdAnalytics.source == "meta",
        )
    )
    existing_by_label: dict = {e.date_label: e for e in existing_rows.scalars().all()}

    for row in raw_rows:
        date_label = row.get("date_start", "")
        impressions = int(row.get("impressions", 0) or 0)
        clicks = int(row.get("clicks", 0) or 0)
        spend = float(row.get("spend", 0) or 0)
        reach = int(row.get("reach", 0) or 0)
        cpm = float(row.get("cpm", 0) or 0)
        cpc = float(row.get("cpc", 0) or 0)
        actions = row.get("actions") or []
        actions = actions if isinstance(actions, list) else []
        conversions = sum(
            int(a.get("value", 0) or 0)
            for a in actions
            if isinstance(a, dict) and a.get("action_type") in ("offsite_conversion.fb_pixel_lead", "link_click")
        )
        click_rate = round(clicks / impressions * 100, 4) if impressions else 0.0

        existing = existing_by_label.get(date_label)
        if existing:
            existing.impressions = impressions
            existing.views = impressions
            existing.click_rate = click_rate
            existing.conversions = conversions
            existing.spend = spend
            existing.reach = reach
            existing.cpm = cpm
            existing.cost_per_click = cpc
        else:
            db.add(
                AdAnalytics(
                    advertisement_id=ad_id,
                    date_label=date_label,
                    source="meta",
                    impressions=impressions,
                    views=impressions,
                    click_rate=click_rate,
                    conversions=conversions,
                    spend=spend,
                    reach=reach,
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
                "reach": reach,
                "cpm": cpm,
                "cpc": cpc,
                "conversions": conversions,
                "click_rate": click_rate,
            }
        )

    await db.commit()
    return {"date_preset": date_preset, "rows": result_rows, "campaign_id": campaign_id}
