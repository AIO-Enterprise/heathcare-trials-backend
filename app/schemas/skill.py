from datetime import datetime

from pydantic import BaseModel


class TrainingRequest(BaseModel):
    """Triggered after onboarding to initialize Curator + Reviewer skills."""

    company_id: str


class TrainingStatus(BaseModel):
    company_id: str
    curator_ready: bool
    reviewer_ready: bool
    skill_versions: dict[str, int]


class SkillOut(BaseModel):
    id: str
    skill_type: str
    version: int
    lessons_learnt: str | None = None
    updated_at: datetime

    class Config:
        from_attributes = True
