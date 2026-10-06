from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class AdAnalytics(Base):
    __tablename__ = "ad_analytics"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    recorded_at = Column(DateTime, default=_now)
    user_retention = Column(Float, nullable=True)
    click_rate = Column(Float, nullable=True)
    follow_through = Column(Float, nullable=True)
    call_duration = Column(Float, nullable=True)
    views = Column(Integer, nullable=True)
    likes = Column(Integer, nullable=True)
    demographics = Column(JSON, nullable=True)
    impressions = Column(Integer, nullable=True)
    conversions = Column(Integer, nullable=True)
    cost_per_click = Column(Float, nullable=True)
    spend = Column(Float, nullable=True)
    reach = Column(Integer, nullable=True)
    cpm = Column(Float, nullable=True)
    date_label = Column(String(32), nullable=True)
    source = Column(String(16), default="local")  # "meta" | "local"

    advertisement = relationship("Advertisement", back_populates="analytics")


class OptimizerLog(Base):
    __tablename__ = "optimizer_logs"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    status = Column(String(16), default="pending")  # pending | done | failed
    suggestions = Column(JSON, nullable=True)
    context = Column(JSON, nullable=True)
    human_decision = Column(String(32), nullable=True)
    applied_changes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now)

    advertisement = relationship("Advertisement", back_populates="optimizer_logs")


class ReinforcementLog(Base):
    __tablename__ = "reinforcement_logs"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=True)
    source_type = Column(String(64), nullable=False)
    raw_data = Column(JSON, nullable=False)
    formalized_doc = Column(Text, nullable=True)
    applied_to_skill = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)

    company = relationship("Company", back_populates="reinforcement_logs")
    advertisement = relationship("Advertisement", back_populates="reinforcement_logs")
