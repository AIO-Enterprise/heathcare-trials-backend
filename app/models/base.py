import enum
import uuid
from datetime import UTC, datetime


def _now():
    # Naive UTC datetime — DB columns are DateTime (no timezone).
    return datetime.now(UTC).replace(tzinfo=None)


def _uuid():
    return str(uuid.uuid4())


class UserRole(enum.StrEnum):
    STUDY_COORDINATOR = "study_coordinator"
    PROJECT_MANAGER = "project_manager"
    ETHICS_MANAGER = "ethics_manager"
    PUBLISHER = "publisher"
    # Company owner. Has every capability of the four roles above merged into one
    # account (see require_roles() in core/security.py). Created by default at
    # onboarding; a Study Coordinator may also self-upgrade to it.
    SUPER_USER = "super_user"


class AdType(enum.StrEnum):
    WEBSITE = "website"
    ADS = "ads"
    VOICEBOT = "voicebot"
    CHATBOT = "chatbot"


class AdStatus(enum.StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    STRATEGY_CREATED = "strategy_created"
    UNDER_REVIEW = "under_review"
    ETHICS_REVIEW = "ethics_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    PAUSED = "paused"
    OPTIMIZING = "optimizing"


class DocumentType(enum.StrEnum):
    USP = "usp"
    COMPLIANCE = "compliance"
    POLICY = "policy"
    MARKETING_GOAL = "marketing_goal"
    ETHICAL_GUIDELINE = "ethical_guideline"
    REFERENCE = "reference"
    PROTOCOL = "protocol"
    INPUT = "input"
