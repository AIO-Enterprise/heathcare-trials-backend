"""
Pydantic-layer enums that mirror the SQLAlchemy enums in app.models.base.
Values are identical; the classes differ only in naming convention.
"""

from enum import StrEnum


class UserRoleEnum(StrEnum):
    study_coordinator = "study_coordinator"
    project_manager = "project_manager"
    ethics_manager = "ethics_manager"
    publisher = "publisher"
    super_user = "super_user"


class AdTypeEnum(StrEnum):
    website = "website"
    ads = "ads"
    voicebot = "voicebot"
    chatbot = "chatbot"


class AdStatusEnum(StrEnum):
    draft = "draft"
    generating = "generating"
    strategy_created = "strategy_created"
    under_review = "under_review"
    ethics_review = "ethics_review"
    approved = "approved"
    published = "published"
    paused = "paused"
    optimizing = "optimizing"


class DocumentTypeEnum(StrEnum):
    usp = "usp"
    compliance = "compliance"
    policy = "policy"
    marketing_goal = "marketing_goal"
    ethical_guideline = "ethical_guideline"
    reference = "reference"
    protocol = "protocol"
    input = "input"
