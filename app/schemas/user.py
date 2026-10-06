from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.enums import UserRoleEnum


class UserUpdateSelf(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=256)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str
    role: UserRoleEnum


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRoleEnum
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
