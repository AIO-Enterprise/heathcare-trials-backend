"""
M6: Reviewer Service (Reviewer Skill Template Agent)
Owner: AI Dev
Dependencies: M1, M4 (Training)

Reviews Curator-generated strategies and produces:
- Website Requirements (for the Website Development Agent)
- Advertisement Details (for the Advertisement Agent)
- Compliance checks and ethical flags
"""

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import extract_json, get_ai_client
from app.models.models import Advertisement, Review
from app.services.ai.skills import load_skill

logger = logging.getLogger(__name__)


class ReviewerService:
    def __init__(self, db: AsyncSession, company_id: str, ai=None):
        self.db = db
        self.company_id = company_id
        # Injectable AI client (DIP) — defaults to the shared provider adapter.
        self.ai = ai or get_ai_client()

    async def pre_review(self, advertisement: Advertisement) -> dict[str, Any]:
        """
        AI pre-review of the Curator's strategy.
        Produces structured outputs for downstream agents.

        Returns:
            {
                "review_verdict": "approved" | "needs_revision",
                "website_requirements": {...},
                "ad_details": {...},
                "ethical_flags": [...],
                "compliance_check": {...}
            }
        """
        skill_md = await load_skill(self.db, self.company_id, "reviewer")
        prompt = self._build_review_prompt(advertisement)
        return await self._call_claude(skill_md, prompt)

    async def incorporate_human_feedback(
        self,
        advertisement: Advertisement,
        reviews: list[Review],
    ) -> dict[str, Any]:
        """
        Re-process strategy incorporating human reviewer feedback.
        Called when a human reviewer sends suggestions back to the AI.
        """
        skill_md = await load_skill(self.db, self.company_id, "reviewer")

        feedback_context = "\n\n".join(
            [
                f"### Review by {r.review_type} reviewer\n"
                f"Status: {r.status}\n"
                f"Comments: {r.comments or 'None'}\n"
                f"Suggestions: {json.dumps(r.suggestions) if r.suggestions else 'None'}\n"
                f"Edited Strategy: {json.dumps(r.edited_strategy) if r.edited_strategy else 'None'}"
                for r in reviews
            ]
        )

        prompt = f"""{self._build_review_prompt(advertisement)}

## Human Reviewer Feedback (HIGH PRIORITY)
{feedback_context}

Incorporate all human feedback and produce an updated review output.
"""
        return await self._call_claude(skill_md, prompt)

    def _build_review_prompt(self, ad: Advertisement) -> str:
        strategy = json.dumps(ad.strategy_json, indent=2) if ad.strategy_json else "No strategy generated yet"
        return f"""## Strategy to Review

**Advertisement:** {ad.title}
**Type:** {", ".join(ad.ad_type if isinstance(ad.ad_type, list) else [])}
**Budget:** {ad.budget or "Not specified"}

### Strategy JSON
```json
{strategy}
```

Review this strategy and produce the complete review output as JSON,
including website_requirements and ad_details sections.
Respond ONLY with the JSON object.
"""

    async def _call_claude(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        if not self.ai.is_configured():
            return self._mock_review()

        text = await self.ai.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=4096,
        )

        try:
            return extract_json(text)
        except json.JSONDecodeError:
            # Fail closed-ish: surface the parse failure instead of pretending
            # the review succeeded with an empty verdict.
            logger.warning("Reviewer output could not be parsed as JSON (%d chars)", len(text))
            return {"raw_response": text, "parse_error": True}

    def _mock_review(self) -> dict[str, Any]:
        return {
            "review_verdict": "approved",
            "quality_score": 8.5,
            "compliance_check": {"passed": True, "issues": []},
            "ethical_flags": [],
            "website_requirements": {
                "pages": ["Home", "About", "Services", "Contact", "Blog"],
                "design_system": {"colors": ["#1a1a2e", "#16213e", "#e94560"], "font": "Inter"},
                "content_blocks": ["Hero", "Features", "Testimonials", "CTA", "Footer"],
                "seo_requirements": {"meta_tags": True, "structured_data": True},
            },
            "ad_details": {
                "formats": ["1080x1080 Static", "1080x1920 Story", "16:9 Video"],
                "copy_variants": [
                    {"headline": "Transform Your Business", "body": "Start your journey today"},
                    {"headline": "Results That Speak", "body": "Join thousands of satisfied clients"},
                ],
                "visual_specs": {"style": "Modern minimal", "imagery": "Professional photography"},
                "platform_configs": {
                    "instagram": {"placements": ["feed", "stories", "reels"]},
                    "google": {"campaign_type": "search+display"},
                },
            },
        }
