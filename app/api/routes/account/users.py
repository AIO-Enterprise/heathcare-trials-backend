"""
M2: User Management Routes
Owner: Backend Dev 1
Dependencies: M1, M2

CRUD operations for users within a company.
Only Admin can create/manage users.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, require_roles
from app.db.database import get_db
from app.models.models import User, UserRole
from app.schemas.schemas import UserCreate, UserOut, UserUpdateSelf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["User Management"])


@router.patch("/me", response_model=UserOut)
async def update_self(
    body: UserUpdateSelf,
    current_user: User = Depends(
        require_roles(
            [
                UserRole.STUDY_COORDINATOR,
                UserRole.PROJECT_MANAGER,
                UserRole.ETHICS_MANAGER,
                UserRole.PUBLISHER,
            ]
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """Allow the authenticated user to update their own display name."""
    current_user.full_name = body.full_name.strip()
    await db.flush()
    return current_user


@router.post("/me/upgrade-to-super", response_model=UserOut)
async def upgrade_self_to_super(
    current_user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Let a Study Coordinator elevate their own account to Super User — the merged
    owner role that can perform every campaign stage itself.

    A Study Coordinator is already the top admin of its own tenant, so this is an
    in-tenant elevation, not a cross-company escalation. (Super User callers pass
    this gate too via the require_roles bypass; for them it is a harmless no-op.)
    """
    logger.info("User=%s self-upgraded to SUPER_USER", current_user.id)
    current_user.role = UserRole.SUPER_USER
    await db.flush()
    return current_user


@router.post("/", response_model=UserOut)
async def create_user(
    body: UserCreate,
    admin: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user for the admin's company."""
    # Super User is the company owner and is never assignable through the UI/API —
    # it is created at onboarding or via self-upgrade only. Keep it to the four
    # delegate roles here.
    if body.role == UserRole.SUPER_USER:
        raise HTTPException(status_code=422, detail="Super User cannot be assigned to a new user.")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        company_id=admin.company_id,
        email=body.email,
        hashed_pw=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
    )
    db.add(user)
    await db.flush()
    logger.info("Created user=%s role=%s company=%s", user.id, user.role.value, user.company_id)
    return user


@router.get("/", response_model=list[UserOut])
async def list_users(
    admin: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the admin's company."""
    result = await db.execute(select(User).where(User.company_id == admin.company_id))
    return result.scalars().all()


@router.patch("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    admin: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user. Enforces: no self-deactivation; at least 1 active SC must remain."""
    if admin.id == user_id:
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account.")

    result = await db.execute(select(User).where(User.id == user_id, User.company_id == admin.company_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # The Super User is the company owner — never let a company lose its last one.
    if user.role == UserRole.SUPER_USER:
        count_result = await db.execute(
            select(func.count()).where(
                User.company_id == admin.company_id,
                User.role == UserRole.SUPER_USER,
                User.is_active,
            )
        )
        if count_result.scalar_one() <= 1:
            raise HTTPException(
                status_code=422,
                detail="Cannot deactivate the last active Super User.",
            )

    # If the target is a Study Coordinator, ensure at least one active SC remains after deactivation.
    if user.role == UserRole.STUDY_COORDINATOR:
        count_result = await db.execute(
            select(func.count()).where(
                User.company_id == admin.company_id,
                User.role == UserRole.STUDY_COORDINATOR,
                User.is_active,
            )
        )
        active_sc_count = count_result.scalar_one()
        if active_sc_count <= 1:
            raise HTTPException(
                status_code=422,
                detail="Cannot deactivate the last active Study Coordinator.",
            )

    logger.info("Deactivating user=%s by admin=%s", user_id, admin.id)
    user.is_active = False
    # Revoke any outstanding sessions immediately.
    user.token_version = (user.token_version or 0) + 1
    # Scramble the email so the address can be re-registered in future.
    user.email = f"deactivated_{user.id}@deactivated.invalid"
    return user
