from sqlalchemy import JSON, Boolean, Column, DateTime, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class Company(Base):
    __tablename__ = "companies"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String(256), nullable=False)
    logo_url = Column(String(512), nullable=True)
    industry = Column(String(128), nullable=True)
    locations = Column(JSON, nullable=True)  # [{ country, cities: [] }]
    created_at = Column(DateTime, default=_now)
    onboarded = Column(Boolean, default=False)

    users = relationship("User", back_populates="company", cascade="all, delete-orphan")
    documents = relationship("CompanyDocument", back_populates="company", cascade="all, delete-orphan")
    advertisements = relationship("Advertisement", back_populates="company", cascade="all, delete-orphan")
    skills = relationship("SkillConfig", back_populates="company", cascade="all, delete-orphan")
    reinforcement_logs = relationship("ReinforcementLog", back_populates="company", cascade="all, delete-orphan")
    brand_kit = relationship("BrandKit", back_populates="company", uselist=False, cascade="all, delete-orphan")
    platform_connections = relationship("PlatformConnection", back_populates="company", cascade="all, delete-orphan")
