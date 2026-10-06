from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.models.base import _now, _uuid


class SkillConfig(Base):
    __tablename__ = "skill_configs"
    __table_args__ = (UniqueConstraint("company_id", "skill_type", name="uq_skill_configs_company_skill"),)

    id = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    skill_type = Column(String(64), nullable=False)
    skill_md = Column(Text, nullable=False)
    version = Column(Integer, default=1)
    lessons_learnt = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="skills")
