from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import DocumentType, _now, _uuid


class CompanyDocument(Base):
    """Global company-level docs: USP, compliance, policies, guidelines.
    Shown on My Company page. Used by curator as baseline RAG context."""

    __tablename__ = "company_documents"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    doc_type = Column(Enum(DocumentType), nullable=False)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=True)
    file_path = Column(String(1024), nullable=True)
    priority = Column(Integer, default=0)
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="documents")


class AdvertisementDocument(Base):
    """Campaign-specific protocol docs scoped to a single advertisement.
    Higher default priority (10) than CompanyDocument so campaign context wins."""

    __tablename__ = "advertisement_documents"

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    advertisement_id = Column(String, ForeignKey("advertisements.id"), nullable=False)
    doc_type = Column(String(64), nullable=False)  # freeform — campaign types vary
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=True)
    file_path = Column(String(1024), nullable=True)
    priority = Column(Integer, default=10)
    created_at = Column(DateTime, default=_now)

    advertisement = relationship("Advertisement", back_populates="protocol_docs")
