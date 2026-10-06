from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.media import media_url
from app.schemas.enums import AdStatusEnum, AdTypeEnum

QUESTIONNAIRE_CAMPAIGN_CATEGORIES = {"recruitment", "survey", "hiring", "clinical_trial", "research"}


class BookingConfig(BaseModel):
    slot_duration_minutes: int = Field(default=30, ge=5, le=480)
    max_per_slot: int = Field(default=3, ge=1, le=500)


class AdvertisementCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    ad_type: list[AdTypeEnum]
    campaign_category: str | None = None
    budget: float | None = None
    duration: str | None = None
    trial_start_date: date | None = None
    trial_end_date: date | None = None
    platforms: list[str] | None = None
    target_audience: dict[str, Any] | None = None
    trial_location: list[dict[str, Any]] | None = None
    patients_required: int | None = None
    special_instructions: str | None = None
    booking_config: BookingConfig | None = None


class AdvertisementOut(BaseModel):
    id: str
    title: str
    ad_type: list[str]
    campaign_category: str | None = None
    status: AdStatusEnum
    budget: float | None = None
    duration: str | None = None
    platforms: list[str] | None = None
    strategy_json: dict[str, Any] | None = None
    review_notes: str | None = None
    website_reqs: dict[str, Any] | None = None
    ad_details: dict[str, Any] | None = None
    output_url: str | None = None
    hosted_url: str | None = None
    output_files: list[dict[str, Any]] | None = None
    shorts_files: list[dict[str, Any]] | None = None
    generation_error: str | None = None
    bot_config: dict[str, Any] | None = None
    booking_config: dict[str, Any] | None = None
    questionnaire: dict[str, Any] | None = None
    trial_location: list[dict[str, Any]] | None = None
    patients_required: int | None = None
    trial_start_date: date | None = None
    trial_end_date: date | None = None
    special_instructions: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @field_validator("hosted_url", "output_url", mode="before")
    @classmethod
    def resolve_media_urls(cls, v: str | None) -> str | None:
        if v and not v.startswith("http"):
            return media_url(v)
        return v

    @field_validator("output_files", "shorts_files", mode="before")
    @classmethod
    def resolve_creative_urls(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not v:
            return v
        for creative in v:
            img = creative.get("image_url")
            if img and not img.startswith("http"):
                creative["image_url"] = media_url(img)
        return v


class AdvertisementUpdate(BaseModel):
    title: str | None = None
    budget: float | None = None
    duration: str | None = None
    trial_start_date: date | None = None
    trial_end_date: date | None = None
    platforms: list[str] | None = None
    target_audience: dict[str, Any] | None = None
    status: AdStatusEnum | None = None
    trial_location: list[dict[str, Any]] | None = None
    patients_required: int | None = None
    booking_config: dict[str, Any] | None = None


class QuestionnaireUpdate(BaseModel):
    questionnaire: dict[str, Any]


class BotConfigUpdate(BaseModel):
    bot_name: str | None = None
    voice_id: str | None = None
    first_message: str | None = None
    conversation_style: str | None = None
    compliance_notes: str | None = None
    language: str | None = None
    allowed_origins: list[str] | None = None
    additional_params: dict[str, Any] | None = None
    pause_schedule: Any | None = None
    meta_campaign_id: str | None = None
