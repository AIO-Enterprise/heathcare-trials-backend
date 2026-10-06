from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import UserRole, _now, _uuid


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    email = Column(String(256), unique=True, nullable=False)
    hashed_pw = Column(String(512), nullable=False)
    full_name = Column(String(256), nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)

    company = relationship("Company", back_populates="users")
    reviews = relationship("Review", back_populates="reviewer", cascade="all, delete-orphan")


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    code = Column(String(6), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)

    user = relationship("User")
