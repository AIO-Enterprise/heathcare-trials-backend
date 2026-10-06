"""
M2: Auth Routes
Owner: Backend Dev 1
Dependencies: security.py, models.py, schemas.py

POST /auth/login               — Role-based sign-in
GET  /auth/check-email         — Email availability check (onboarding)
POST /auth/refresh             — Re-issue JWT token
POST /auth/request-password-change  — Send OTP to authenticated user's email
POST /auth/confirm-password-change  — Validate OTP and set new password
"""

import random
import string
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.db.database import get_db
from app.models.models import Company, PasswordResetCode, User
from app.schemas.schemas import ConfirmPasswordChangeRequest, LoginRequest, TokenResponse
from app.services.messaging.email_service import send_otp_email

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _token_response(user: User, company: Company, token: str) -> TokenResponse:
    return TokenResponse(
        access_token=token,
        role=user.role.value,
        company_id=user.company_id,
        company_name=company.name,
        company_industry=company.industry,
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        onboarded=company.onboarded,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Role-based user sign-in.
    Verifies email, password, company name, and role.
    All failures return a generic 401 to avoid leaking information.
    """
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_pw):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    if user.role.value != body.role.value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    company_result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = company_result.scalar_one_or_none()

    if not company or company.name.lower() != body.company.strip().lower():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(user_id=user.id, role=user.role.value, company_id=user.company_id)
    return _token_response(user, company, token)


@router.get("/check-email")
async def check_email(
    email: str = Query(..., description="Email address to check"),
    db: AsyncSession = Depends(get_db),
):
    """Check if an email is already registered. Returns 409 if taken."""
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email_exists")
    return {"available": True}


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Re-issue a fresh 24-hour token for an already-authenticated user.
    Frontend calls this proactively before the current token expires.
    """
    company_result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = company_result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    token = create_access_token(user_id=user.id, role=user.role.value, company_id=user.company_id)
    return _token_response(user, company, token)


# ─── Password Change (authenticated, email-verified) ──────────────────────────


@router.post("/request-password-change", status_code=200)
async def request_password_change(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a 6-digit OTP to the authenticated user's email."""
    # Invalidate any previous unused codes
    old_result = await db.execute(
        select(PasswordResetCode).where(
            PasswordResetCode.user_id == current_user.id,
            PasswordResetCode.used.is_(False),
        )
    )
    for old in old_result.scalars().all():
        old.used = True

    code = "".join(random.choices(string.digits, k=6))
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10)
    db.add(PasswordResetCode(user_id=current_user.id, code=code, expires_at=expires_at))
    await db.flush()

    await send_otp_email(current_user.email, current_user.full_name, code)
    return {"detail": "OTP sent"}


@router.post("/confirm-password-change", status_code=200)
async def confirm_password_change(
    body: ConfirmPasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify the 6-digit OTP and set the new password (single-use, 10-min expiry)."""
    result = await db.execute(
        select(PasswordResetCode).where(
            PasswordResetCode.user_id == current_user.id,
            PasswordResetCode.code == body.code,
            PasswordResetCode.used.is_(False),
        )
    )
    otp = result.scalar_one_or_none()

    now = datetime.now(UTC).replace(tzinfo=None)
    if not otp or otp.expires_at < now:
        raise HTTPException(status_code=422, detail="Invalid or expired verification code.")

    otp.used = True
    current_user.hashed_pw = hash_password(body.new_password)
    return {"detail": "Password updated successfully."}
