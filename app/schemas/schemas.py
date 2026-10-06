"""
Re-exports every schema so existing imports like
    from app.schemas.schemas import AdvertisementOut, LoginRequest
continue to work without changes.
"""

from app.schemas.advertisement import (  # noqa: F401
    QUESTIONNAIRE_CAMPAIGN_CATEGORIES,
    AdvertisementCreate,
    AdvertisementOut,
    AdvertisementUpdate,
    BookingConfig,
    BotConfigUpdate,
    QuestionnaireUpdate,
)
from app.schemas.analytics import (  # noqa: F401
    AnalyticsOut,
    OptimizerDecision,
    OptimizerSuggestion,
)
from app.schemas.auth import (  # noqa: F401
    ConfirmPasswordChangeRequest,
    LoginRequest,
    TokenResponse,
)
from app.schemas.brand_kit import BrandKitCreate, BrandKitOut, BrandKitUpdate  # noqa: F401
from app.schemas.document import (  # noqa: F401
    AdvertisementDocumentOut,
    ChunkUploadRequest,
    ConfirmUploadRequest,
    DocumentCreate,
    DocumentOut,
    DocumentUpdate,
    FinalizeUploadRequest,
    PresignRequest,
    PresignResponse,
    StartUploadRequest,
)
from app.schemas.enums import (  # noqa: F401
    AdStatusEnum,
    AdTypeEnum,
    DocumentTypeEnum,
    UserRoleEnum,
)
from app.schemas.onboarding import (  # noqa: F401
    LogoUploadResponse,
    OnboardingRequest,
    OnboardingResponse,
)
from app.schemas.review import (  # noqa: F401
    MinorEditRequest,
    ReviewCreate,
    ReviewOut,
    RewriteQuestionRequest,
    RewriteStrategyRequest,
)
from app.schemas.skill import SkillOut, TrainingRequest, TrainingStatus  # noqa: F401
from app.schemas.survey import (  # noqa: F401
    AppointmentCreate,
    AppointmentOut,
    AvailableSlotsResponse,
    CallTranscriptOut,
    SlotInfo,
    SurveyAnswerItem,
    SurveyResponseCreate,
    SurveyResponseOut,
    VoiceSessionOut,
)
from app.schemas.user import UserCreate, UserOut, UserUpdateSelf  # noqa: F401
