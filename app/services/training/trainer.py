import asyncio
import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bedrock import get_client, get_model, is_configured
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.db.database import async_session_factory
from app.models.models import Company, CompanyDocument, SkillConfig
from app.schemas.schemas import TrainingStatus

logger = logging.getLogger(__name__)

_is_postgres = settings.DATABASE_URL.startswith("postgresql")

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "..", "..", "skills"))
TEMPLATES_DIR = os.path.join(SKILLS_DIR, "templates")
OUTPUT_DIR = os.path.join(SKILLS_DIR, "trained")
TRAINER_SKILL = os.path.join(TEMPLATES_DIR, "trainer_template.md")

TEMPLATES_TO_TRAIN = ["curator", "reviewer"]


# ── Helpers ──────────────────────────────────────────────────────────────────
def load_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def call_trainer(client, trainer_skill: str, template: str, company_data: dict) -> str:
    logger.info("Calling AI trainer for skill training...")
    response = client.messages.create(
        model=get_model("generic"),
        max_tokens=4096,
        system=trainer_skill,
        messages=[
            {
                "role": "user",
                "content": f"""## Skill Template
{template}

## Company Onboarding Data
{json.dumps(company_data, indent=2)}
""",
            }
        ],
    )
    return response.content[0].text


# ── TrainingService (used by onboarding API route) ────────────────────────────


class TrainingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def train_company_skills(self, company_id: str) -> TrainingStatus:
        logger.info("Training skills for company %s...", company_id)
        # ── Phase 1: read-only DB fetch (hold connection briefly) ────────────
        async with async_session_factory() as read_db:
            result = await read_db.execute(select(Company).where(Company.id == company_id))
            company = result.scalar_one_or_none()
            if not company:
                raise NotFoundError(f"Company {company_id} not found")

            docs_result = await read_db.execute(select(CompanyDocument).where(CompanyDocument.company_id == company_id))
            docs = docs_result.scalars().all()

            def concat(doc_type):
                return "\n".join(d.content for d in docs if d.doc_type == doc_type and d.content)

            company_data = {
                "company_name": company.name,
                "industry": company.industry or "",
                "usp_summary": concat("usp"),
                "marketing_goals": concat("marketing_goal"),
                "compliance_notes": concat("compliance"),
                "ethical_guidelines": concat("ethical_guideline"),
                "lessons_learned": concat("reference") or "No lessons learned yet.",
            }
            company_name = company.name
        # read_db session is closed here — connection returned to pool

        # ── Phase 2: call Claude (NO DB connection held) ─────────────────────
        trainer_skill = load_file(TRAINER_SKILL)
        filled_skills: dict[str, str] = {}

        for skill_name in TEMPLATES_TO_TRAIN:
            logger.info("Training %s skill for company %s...", skill_name, company_name)
            template_path = os.path.join(TEMPLATES_DIR, f"{skill_name}_template.md")
            template = load_file(template_path)

            if is_configured("generic"):
                client = get_client()
                filled = await asyncio.to_thread(call_trainer, client, trainer_skill, template, company_data)
            else:
                logger.info("No AI backend configured, storing template as-is")
                filled = template

            filled_skills[skill_name] = filled

        # ── Phase 3: write results (hold connection briefly) ─────────────────
        skill_versions = {}
        async with async_session_factory() as write_db:
            for skill_name, filled in filled_skills.items():
                if _is_postgres:
                    stmt = (
                        pg_insert(SkillConfig)
                        .values(
                            company_id=company_id,
                            skill_type=skill_name,
                            skill_md=filled,
                            version=1,
                        )
                        .on_conflict_do_update(
                            index_elements=["company_id", "skill_type"],
                            set_={
                                "skill_md": filled,
                                "version": SkillConfig.version + 1,
                            },
                        )
                        .returning(SkillConfig.version)
                    )
                    row = await write_db.execute(stmt)
                    version = row.scalar_one()
                else:
                    stmt = (
                        sqlite_insert(SkillConfig)
                        .values(
                            company_id=company_id,
                            skill_type=skill_name,
                            skill_md=filled,
                            version=1,
                        )
                        .on_conflict_do_update(
                            index_elements=["company_id", "skill_type"],
                            set_={
                                "skill_md": filled,
                                "version": SkillConfig.version + 1,
                            },
                        )
                    )
                    await write_db.execute(stmt)
                    row = await write_db.execute(
                        select(SkillConfig.version).where(
                            SkillConfig.company_id == company_id,
                            SkillConfig.skill_type == skill_name,
                        )
                    )
                    version = row.scalar_one()

                skill_versions[skill_name] = version
                logger.info("[OK] %s skill saved to DB (v%s)", skill_name, version)

            await write_db.commit()
        # write_db session is closed here

        return TrainingStatus(
            company_id=company_id,
            curator_ready=True,
            reviewer_ready=True,
            skill_versions=skill_versions,
        )


# ── Standalone script (python trainer.py) ────────────────────────────────────


def train():
    COMPANY_DATA_PATH = os.path.join(BASE_DIR, "sample_company.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not is_configured("generic"):
        raise OSError("No AI backend configured. Set AZURE_GENERIC_* or AWS_GENERIC_MODEL.")

    client = get_client()
    trainer_skill = load_file(TRAINER_SKILL)
    company_data = json.loads(load_file(COMPANY_DATA_PATH))

    print(f"Training skills for: {company_data['company_name']} ({company_data['industry']})\n")

    for skill_name in TEMPLATES_TO_TRAIN:
        print(f"  -> Training {skill_name} skill...")
        template_path = os.path.join(TEMPLATES_DIR, f"{skill_name}_template.md")
        template = load_file(template_path)

        filled = call_trainer(client, trainer_skill, template, company_data)

        output_path = os.path.join(OUTPUT_DIR, f"{skill_name}_skill.md")
        with open(output_path, "w") as f:
            f.write(filled)

        print(f"  [OK] {skill_name}_skill.md written -> {output_path}\n")

    print("Training complete.")


if __name__ == "__main__":
    train()
