from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class SurveyAnswerItem(BaseModel):
    question_id: str
    question_text: str
    selected_option: str
    is_eligible: bool | None = None


class SurveyResponseCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    age: int = Field(..., ge=1, le=120)
    sex: str = Field(..., pattern="^(male|female|other|prefer_not_to_say)$")
    phone: str = Field(..., min_length=5, max_length=32)
    answers: list[SurveyAnswerItem] = []
    is_eligible: bool | None = None


class CallTranscriptOut(BaseModel):
    speaker: str
    text: str
    turn_index: int | None = None
    timestamp_ms: int | None = None

    class Config:
        from_attributes = True


class VoiceSessionOut(BaseModel):
    id: str
    elevenlabs_conversation_id: str | None = None
    status: str
    phone: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    transcripts: list[CallTranscriptOut] = []
    call_analysis: dict[str, Any] | None = None

    class Config:
        from_attributes = True


class SurveyResponseOut(BaseModel):
    id: str
    advertisement_id: str
    full_name: str
    age: int
    sex: str
    phone: str
    answers: list[dict[str, Any]]
    is_eligible: bool | None = None
    created_at: datetime
    voice_sessions: list[VoiceSessionOut] = []

    class Config:
        from_attributes = True


class SlotInfo(BaseModel):
    time: str  # "HH:MM"
    label: str  # "9:00 AM"
    available: bool


class AvailableSlotsResponse(BaseModel):
    date: str
    duration_minutes: int
    slots: list[SlotInfo]


class AppointmentCreate(BaseModel):
    patient_name: str
    patient_phone: str
    slot_datetime: datetime
    survey_response_id: str | None = None
    notes: str | None = None


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    advertisement_id: str
    survey_response_id: str | None = None
    voice_session_id: str | None = None
    chat_session_id: str | None = None
    patient_name: str
    patient_phone: str
    slot_datetime: datetime
    duration_minutes: int
    status: str
    notes: str | None = None
    created_at: datetime

    @computed_field
    @property
    def source(self) -> str:
        if self.voice_session_id:
            return "voicebot"
        if self.survey_response_id:
            return "survey"
        if self.chat_session_id:
            return "chatbot"
        return "chatbot"
