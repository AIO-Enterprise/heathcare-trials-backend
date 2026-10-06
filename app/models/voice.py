from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class CallCampaign(Base):
    """Bulk outbound call campaign — a list of phone numbers dialled via a single ad's agent."""

    __tablename__ = "call_campaigns"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    name = Column(String(256), nullable=False)
    # queued | running | paused | done | cancelled | failed
    status = Column(String(32), default="queued")
    total = Column(Integer, default=0)
    completed = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    concurrency = Column(Integer, default=2)
    per_minute = Column(Integer, default=20)
    created_at = Column(DateTime, default=_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    advertisement = relationship("Advertisement", back_populates="call_campaigns")
    records = relationship("CallRecord", back_populates="campaign", cascade="all, delete-orphan")


class CallRecord(Base):
    """One phone number within a CallCampaign."""

    __tablename__ = "call_records"

    id = Column(String, primary_key=True, default=_uuid)
    campaign_id = Column(String, ForeignKey("call_campaigns.id"), nullable=False)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    phone_e164 = Column(String(32), nullable=False)
    contact_name = Column(String(256), nullable=True)
    # pending | dialing | in_progress | completed | failed | no_answer
    status = Column(String(32), default="pending")
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=2)
    conversation_id = Column(String(256), nullable=True)
    last_error = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=_now)
    called_at = Column(DateTime, nullable=True)

    campaign = relationship("CallCampaign", back_populates="records")


class VoiceSession(Base):
    """Tracks each browser-initiated ElevenLabs voice call session."""

    __tablename__ = "voice_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    elevenlabs_conversation_id = Column(String(256), nullable=True, unique=True)
    status = Column(String(32), default="active")  # active | ended | failed
    started_at = Column(DateTime, default=_now)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    caller_metadata = Column(JSON, nullable=True)
    phone = Column(String(32), nullable=True)
    survey_response_id = Column(String, ForeignKey("survey_responses.id"), nullable=True)
    call_analysis = Column(JSON, nullable=True)

    advertisement = relationship("Advertisement", back_populates="voice_sessions")
    transcripts = relationship("CallTranscript", back_populates="session", cascade="all, delete-orphan")
    survey_response = relationship("SurveyResponse", back_populates="voice_sessions", foreign_keys=[survey_response_id])
    booking = relationship("Appointment", back_populates="voice_session", uselist=False)


class CallTranscript(Base):
    """Speaker turns fetched from ElevenLabs after a session ends."""

    __tablename__ = "call_transcripts"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("voice_sessions.id"), nullable=False)
    speaker = Column(String(16), nullable=False)  # "agent" | "user"
    text = Column(Text, nullable=False)
    turn_index = Column(Integer, nullable=True)
    timestamp_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now)

    session = relationship("VoiceSession", back_populates="transcripts")


class ChatSession(Base):
    """Per-visitor conversation history scoped to a single campaign."""

    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("campaign_id", "session_id", name="uq_chat_session"),)

    id = Column(String, primary_key=True, default=_uuid)
    campaign_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    session_id = Column(String, nullable=False)
    messages = Column(JSON, default=list)
    chat_analysis = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    advertisement = relationship("Advertisement", back_populates="chat_sessions")
