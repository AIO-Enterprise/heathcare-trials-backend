from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class PlatformConnection(Base):
    """OAuth tokens for social ad platforms (Meta, etc.) per company.
    One record per company per platform. Token is a long-lived user access token."""

    __tablename__ = "platform_connections"
    __table_args__ = (UniqueConstraint("company_id", "platform", name="uq_platform_connections_company_platform"),)

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(32), nullable=False)
    access_token = Column(Text, nullable=False)
    token_expires_at = Column(DateTime, nullable=True)
    ad_account_id = Column(String(128), nullable=True)
    ad_account_name = Column(String(256), nullable=True)
    page_id = Column(String(128), nullable=True)
    page_name = Column(String(256), nullable=True)
    meta_user_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="platform_connections")
