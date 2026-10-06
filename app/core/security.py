"""
M2: Authentication & Security
Owner: Backend Dev 1
Dependencies: config.py, models.py

JWT-based auth with role-based access control (RBAC).
"""

from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models.models import Advertisement, Company, User, UserRole

# Routes that authenticated-but-not-yet-onboarded users are allowed to call.
# /api/documents is included because Study Coordinators / Ethics Managers may
# upload or manage company documents before or during the initial AI-training
# step that flips Company.onboarded to True. Role checks on each route still
# prevent unauthorised access.
_ONBOARDING_PREFIXES = (
    "/api/onboarding",
    "/api/auth",
    "/api/health",
    "/api/brand-kit",
    "/api/users",
    "/api/company",
    "/api/documents",
)

# ─── Password hashing ────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─── JWT ──────────────────────────────────────────────────────────────────────

security = HTTPBearer()


def create_access_token(user_id: str, role: str, company_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "role": role,
        "company_id": company_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ─── Dependency: get current user ─────────────────────────────────────────────


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Block access to all non-onboarding routes until the company finishes setup.
    path = request.url.path
    if not any(path.startswith(p) for p in _ONBOARDING_PREFIXES):
        company_result = await db.execute(select(Company).where(Company.id == user.company_id))
        company = company_result.scalar_one_or_none()
        if company and not company.onboarded:
            raise HTTPException(
                status_code=403,
                detail="onboarding_incomplete",
            )

    return user


# ─── Role-based access helpers ────────────────────────────────────────────────


def require_roles(allowed_roles: list[UserRole]):
    """
    FastAPI dependency factory.
    Usage: Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PUBLISHER]))

    SUPER_USER is the company owner and satisfies every role gate, so it is
    never listed explicitly — it short-circuits the check below. This is what
    lets one account perform the work of all four roles without touching any
    of the existing require_roles([...]) call sites.
    """

    async def _check(user: User = Depends(get_current_user)):
        if user.role == UserRole.SUPER_USER:
            return user
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role.value}' is not authorized for this action",
            )
        return user

    return _check


async def get_company_ad_or_404(
    db: AsyncSession,
    user: User,
    ad_id: str,
) -> "Advertisement":
    """
    Load an advertisement scoped to the caller's tenant.

    SUPER_USERs may access any tenant's campaign (admin bypass); everyone else
    only ever resolves ads belonging to their own company. A cross-tenant id
    is indistinguishable from a nonexistent one (404 — no existence leak).
    """
    stmt = select(Advertisement).where(Advertisement.id == ad_id)
    if user.role != UserRole.SUPER_USER:
        stmt = stmt.where(Advertisement.company_id == user.company_id)
    result = await db.execute(stmt)
    ad = result.scalar_one_or_none()
    if not ad:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return ad
