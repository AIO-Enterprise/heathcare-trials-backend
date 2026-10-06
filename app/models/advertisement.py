from sqlalchemy import JSON, Column, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import AdStatus, _now, _uuid


class Advertisement(Base):
    __tablename__ = "advertisements"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    title = Column(String(512), nullable=False)
    ad_type = Column(JSON, nullable=False)
    campaign_category = Column(String(64), nullable=True)
    duration = Column(String(128), nullable=True)
    trial_start_date = Column(Date, nullable=True)
    trial_end_date = Column(Date, nullable=True)
    status = Column(Enum(AdStatus), default=AdStatus.DRAFT)
    budget = Column(Float, nullable=True)
    platforms = Column(JSON, nullable=True)
    target_audience = Column(JSON, nullable=True)
    strategy_json = Column(JSON, nullable=True)
    review_notes = Column(Text, nullable=True)
    website_reqs = Column(JSON, nullable=True)
    ad_details = Column(JSON, nullable=True)
    output_url = Column(String(1024), nullable=True)
    hosted_url = Column(String(1024), nullable=True)
    output_files = Column(JSON, nullable=True)
    shorts_files = Column(JSON, nullable=True)
    # Last AI-generation failure message — surfaced to users so background
    # failures don't look like an eternal "generating…" state.
    generation_error = Column(Text, nullable=True)
    bot_config = Column(JSON, nullable=True)
    booking_config = Column(JSON, nullable=True)  # {slot_duration_minutes, max_per_slot}
    questionnaire = Column(JSON, nullable=True)
    trial_location = Column(JSON, nullable=True)
    patients_required = Column(Integer, nullable=True)
    special_instructions = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="advertisements")
    protocol_docs = relationship("AdvertisementDocument", back_populates="advertisement", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="advertisement", cascade="all, delete-orphan")
    analytics = relationship("AdAnalytics", back_populates="advertisement", cascade="all, delete-orphan")
    optimizer_logs = relationship("OptimizerLog", back_populates="advertisement", cascade="all, delete-orphan")
    reinforcement_logs = relationship("ReinforcementLog", back_populates="advertisement")
    voice_sessions = relationship("VoiceSession", back_populates="advertisement", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="advertisement", cascade="all, delete-orphan")
    call_campaigns = relationship("CallCampaign", back_populates="advertisement", cascade="all, delete-orphan")
    survey_responses = relationship("SurveyResponse", back_populates="advertisement", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="advertisement", cascade="all, delete-orphan")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    reviewer_id = Column(String, ForeignKey("users.id"), nullable=False)
    review_type = Column(String(32), nullable=False)
    status = Column(String(32), default="pending")
    comments = Column(Text, nullable=True)
    suggestions = Column(JSON, nullable=True)
    edited_strategy = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now)

    advertisement = relationship("Advertisement", back_populates="reviews")
    reviewer = relationship("User", back_populates="reviews")
