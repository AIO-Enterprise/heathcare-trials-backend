from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class BrandKit(Base):
    """One-to-one with Company. Stores visual identity and tone data."""

    __tablename__ = "brand_kits"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False, unique=True)
    primary_color = Column(String(16), nullable=True)
    secondary_color = Column(String(16), nullable=True)
    accent_color = Column(String(16), nullable=True)
    primary_font = Column(String(128), nullable=True)
    secondary_font = Column(String(128), nullable=True)
    adjectives = Column(Text, nullable=True)
    dos = Column(Text, nullable=True)
    donts = Column(Text, nullable=True)
    preset_name = Column(String(128), nullable=True)
    pdf_path = Column(String(1024), nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="brand_kit")
