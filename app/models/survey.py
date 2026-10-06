from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class SurveyResponse(Base):
    """Participant personal details + survey answers after completing a questionnaire."""

    __tablename__ = "survey_responses"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    full_name = Column(String(256), nullable=False)
    age = Column(Integer, nullable=False)
    sex = Column(String(32), nullable=False)
    phone = Column(String(32), nullable=False)
    answers = Column(JSON, default=list)
    is_eligible = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=_now)

    advertisement = relationship("Advertisement", back_populates="survey_responses")
    voice_sessions = relationship(
        "VoiceSession", back_populates="survey_response", foreign_keys="[VoiceSession.survey_response_id]"
    )
    appointments = relationship("Appointment", back_populates="survey_response")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(String, primary_key=True, default=_uuid)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    survey_response_id = Column(String, ForeignKey("survey_responses.id"), nullable=True)
    voice_session_id = Column(String, ForeignKey("voice_sessions.id"), nullable=True)
    chat_session_id = Column(String, ForeignKey("chat_sessions.id"), nullable=True)
    patient_name = Column(String(256), nullable=False)
    patient_phone = Column(String(32), nullable=False)
    slot_datetime = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, nullable=False, default=30)
    status = Column(String(32), default="confirmed")  # confirmed | cancelled
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)

    advertisement = relationship("Advertisement", back_populates="appointments")
    survey_response = relationship("SurveyResponse", back_populates="appointments")
    voice_session = relationship("VoiceSession", back_populates="booking")
