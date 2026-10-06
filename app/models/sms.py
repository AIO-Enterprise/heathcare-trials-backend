from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class SmsMessage(Base):
    """Audit log of every outbound SMS/WhatsApp message.

    Doubles as the dedup source for appointment reminders: the reminder sweeper
    only sends to appointments that have no prior row here with kind='reminder'.
    Keeping reminder state in this table (instead of a new column on
    appointments) avoids altering the existing appointments table.
    """

    __tablename__ = "sms_messages"

    id = Column(String, primary_key=True, default=_uuid)
    # Tenant owner. Nullable only for legacy rows created before scoping.
    company_id = Column(String, ForeignKey("companies.id"), nullable=True)
    to_number = Column(String(32), nullable=False)
    body = Column(Text, nullable=False)
    # confirmation | cancellation | reminder | campaign | manual
    kind = Column(String(32), nullable=False, default="manual")
    # queued | sent | failed
    status = Column(String(16), nullable=False, default="queued")
    provider_sid = Column(String(64), nullable=True)  # Twilio Message SID
    error = Column(String(512), nullable=True)
    appointment_id = Column(String, ForeignKey("appointments.id"), nullable=True)
    created_at = Column(DateTime, default=_now)


class SmsCampaign(Base):
    """Bulk SMS blast to a list of phone numbers (mirrors CallCampaign)."""

    __tablename__ = "sms_campaigns"

    id = Column(String, primary_key=True, default=_uuid)
    # Tenant owner. Nullable only for legacy rows created before scoping.
    company_id = Column(String, ForeignKey("companies.id"), nullable=True)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=True)
    name = Column(String(256), nullable=False)
    body_template = Column(Text, nullable=False)  # supports {name} placeholder
    # queued | running | paused | done | cancelled | failed
    status = Column(String(32), default="queued")
    total = Column(Integer, default=0)
    completed = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    per_minute = Column(Integer, default=60)
    created_at = Column(DateTime, default=_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    records = relationship("SmsRecord", back_populates="campaign", cascade="all, delete-orphan")


class SmsRecord(Base):
    """One phone number within an SmsCampaign."""

    __tablename__ = "sms_records"

    id = Column(String, primary_key=True, default=_uuid)
    campaign_id = Column(String, ForeignKey("sms_campaigns.id"), nullable=False)
    phone_e164 = Column(String(32), nullable=False)
    contact_name = Column(String(256), nullable=True)
    # pending | sent | failed
    status = Column(String(32), default="pending")
    provider_sid = Column(String(64), nullable=True)
    last_error = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=_now)
    sent_at = Column(DateTime, nullable=True)

    campaign = relationship("SmsCampaign", back_populates="records")
