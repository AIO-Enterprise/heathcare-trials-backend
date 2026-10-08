"""
Pydantic-layer enums — single-sourced from app.models.base.

The *Enum aliases below are the same StrEnum classes the ORM uses, so a
value added in one layer can never drift from the other. Member names are
UPPERCASE (e.g. UserRoleEnum.STUDY_COORDINATOR); values are unchanged.
"""

from app.models.base import AdStatus as AdStatusEnum  # noqa: F401
from app.models.base import AdType as AdTypeEnum  # noqa: F401
from app.models.base import DocumentType as DocumentTypeEnum  # noqa: F401
from app.models.base import UserRole as UserRoleEnum  # noqa: F401
