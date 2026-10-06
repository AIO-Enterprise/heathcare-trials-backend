from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class AnalyticsOut(BaseModel):
    id: str
    advertisement_id: str
    recorded_at: datetime
    user_retention: float | None = None
    click_rate: float | None = None
    follow_through: float | None = None
    views: int | None = None
    likes: int | None = None
    demographics: dict[str, Any] | None = None
    impressions: int | None = None
    conversions: int | None = None
    cost_per_click: float | None = None
    spend: float | None = None
    reach: int | None = None
    cpm: float | None = None
    date_label: str | None = None
    source: str | None = None

    class Config:
        from_attributes = True


class OptimizerSuggestion(BaseModel):
    advertisement_id: str
    status: str = "done"
    suggestions: dict[str, Any] | None = None
    context: dict[str, Any] | None = None


class OptimizerDecision(BaseModel):
    decision: Literal["accepted", "rejected", "partial"]
    applied_changes: dict[str, Any] | None = None
