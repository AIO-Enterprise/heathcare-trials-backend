"""
Shared AI-skill loading.

Every AI agent (curator, reviewer, voicebot) runs with a company-specific
SKILL.md as its system prompt. This is the single implementation of:
  1. Load the newest trained SkillConfig row for (company, skill_type).
  2. Fall back to the generic template in /skills/templates.
  3. Fail loudly when neither exists — silent fallbacks mask untrained agents.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError
from app.models.models import SkillConfig

logger = logging.getLogger(__name__)

_SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "skills", "templates"))


async def get_skill_config(db: AsyncSession, company_id: str, skill_type: str) -> SkillConfig | None:
    """Return the newest trained SkillConfig row for (company, skill_type), or None.

    Shared loader for callers that need the row itself (not just the prompt) —
    e.g. reinforcement appends lessons to it. Uses first() so multiple trained
    versions can never raise MultipleResultsFound.
    """
    result = await db.execute(
        select(SkillConfig)
        .where(
            SkillConfig.company_id == company_id,
            SkillConfig.skill_type == skill_type,
        )
        .order_by(SkillConfig.version.desc())
    )
    return result.scalars().first()


async def load_skill(db: AsyncSession, company_id: str, skill_type: str) -> str:
    """Return the company's customized skill prompt, or the generic template."""
    result = await db.execute(
        select(SkillConfig)
        .where(
            SkillConfig.company_id == company_id,
            SkillConfig.skill_type == skill_type,
        )
        .order_by(SkillConfig.version.desc())
    )
    skill = result.scalars().first()
    if skill:
        return skill.skill_md

    template_path = os.path.join(_SKILLS_DIR, f"{skill_type}_template.md")
    if os.path.exists(template_path):
        logger.warning(
            "No trained '%s' skill for company %s — using generic template. "
            "Run POST /api/onboarding/train to customise.",
            skill_type,
            company_id,
        )
        with open(template_path, encoding="utf-8") as f:
            return f.read()

    raise ConfigurationError(
        f"Skill '{skill_type}' not found and template is missing. Run POST /api/onboarding/train first."
    )
