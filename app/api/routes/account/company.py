"""
Company profile routes.

GET  /company/profile   — Return company info (name, industry, locations)
PATCH /company/locations — Update operating locations
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.security import require_roles, verify_password
from app.db.database import get_db
from app.models.models import Company, User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company", tags=["Company"])


class LocationEntry(BaseModel):
    country: str
    cities: list[str] = []


class CompanyProfile(BaseModel):
    id: str
    name: str
    industry: str | None
    locations: list[LocationEntry] = []


class LocationsUpdate(BaseModel):
    locations: list[LocationEntry]


class DeleteAccountRequest(BaseModel):
    password: str


@router.get("/profile", response_model=CompanyProfile)
async def get_profile(
    user: User = Depends(
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
    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one()
    return CompanyProfile(
        id=company.id,
        name=company.name,
        industry=company.industry,
        locations=[LocationEntry(**loc) for loc in (company.locations or [])],
    )


@router.patch("/locations", response_model=CompanyProfile)
async def update_locations(
    body: LocationsUpdate,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one()
    logger.info("Updating locations for company=%s count=%d", user.company_id, len(body.locations))
    company.locations = [loc.model_dump() for loc in body.locations]
    flag_modified(company, "locations")
    await db.flush()
    return CompanyProfile(
        id=company.id,
        name=company.name,
        industry=company.industry,
        locations=body.locations,
    )


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: DeleteAccountRequest,
    user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently delete the company and all associated data.
    Requires the study coordinator's current password for confirmation.
    Cascade deletes handle users, documents, campaigns, brand kit, skills, etc.
    """
    if not verify_password(body.password, user.hashed_pw):
        logger.warning("Account deletion refused — bad password for user=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password. Account not deleted.",
        )

    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    logger.warning("Deleting company=%s and all data — requested by user=%s", user.company_id, user.id)
    await db.delete(company)
    # flush so cascade deletes run before commit
    await db.flush()
