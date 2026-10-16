"""
M2: Authentication & Security
Owner: Backend Dev 1
Dependencies: config.py, models.py

JWT-based auth with role-based access control (RBAC).
"""

from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
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
    "/api/files",
)

# ─── Password hashing ────────────────────────────────────────────────────────
# Raw bcrypt (not passlib): passlib 1.7.4 is unmaintained and raises on
# bcrypt>=4.1 ("password cannot be longer than 72 bytes" during backend
# detection), which 500s every register/login in fresh environments.
# Hashes stay standard $2b$ — existing rows verify unchanged.

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ─── JWT ──────────────────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


def _bearer_from(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials and credentials.credentials:
        return credentials.credentials
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip() or None  # ponytail: manual fallback, drop when header fallback is deleted


def create_access_token(user_id: str, role: str, company_id: str, token_version: int = 0) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "role": role,
        "company_id": company_id,
        "v": token_version or 0,
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
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = request.cookies.get("access_token") or _bearer_from(request, credentials)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Token revocation — logout-all / deactivate bumps token_version.
    # Missing/NULL counts as 0 so pre-revocation tokens and rows keep working.
    if (payload.get("v") or 0) != (user.token_version or 0):
        raise HTTPException(status_code=401, detail="Session revoked — please sign in again")

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
