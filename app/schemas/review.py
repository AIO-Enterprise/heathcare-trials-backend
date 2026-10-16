from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class ReviewCreate(BaseModel):
    review_type: Literal["strategy", "ethics", "performance"]
    status: Literal["pending", "approved", "rejected", "revision"] = "pending"
    comments: str | None = None
    suggestions: dict[str, Any] | None = None
    edited_strategy: dict[str, Any] | None = None


class ReviewOut(BaseModel):
    id: str
    advertisement_id: str
    reviewer_id: str
    review_type: str
    status: str
    comments: str | None = None
    suggestions: dict[str, Any] | None = None
    edited_strategy: dict[str, Any] | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class MinorEditRequest(BaseModel):
    field: str  # dot-path e.g. "executive_summary" or "messaging.core_message"
    old_value: str
    new_value: str


class RewriteStrategyRequest(BaseModel):
    instructions: str


class RewriteContentRequest(BaseModel):
    instructions: str


class RewriteQuestionRequest(BaseModel):
    question: dict
    instruction: str
