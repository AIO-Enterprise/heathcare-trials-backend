from pydantic import BaseModel, EmailStr, Field

from app.schemas.enums import UserRoleEnum


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    company: str
    role: UserRoleEnum


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRoleEnum
    company_id: str
    company_name: str
    company_industry: str | None = None
    user_id: str
    full_name: str = ""
    email: str = ""
    onboarded: bool = False


class ConfirmPasswordChangeRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)
