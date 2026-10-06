"""
M7: Optimizer Service — v2 (Deterministic + Structured)
Owner: AI Dev
Dependencies: M1, M6

Two deterministic optimization categories:
  1. Cost Optimization  — marginal resource allocation + traffic window analysis
  2. Content Optimization — creative suggestions for website and advertisement

Each suggestion item has:
  - what:   the specific change to make
  - why:    data-driven reason referencing actual metrics
  - prompt: self-contained AI prompt to regenerate or refine this item
"""

import datetime
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import extract_json, get_ai_client
from app.models.models import AdAnalytics, Advertisement, Review

logger = logging.getLogger(__name__)


def _f(v) -> float:
    """Safe float conversion — returns 0.0 for None, non-numeric strings, etc."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_str(v, default: str = "") -> str:
    """Safe string conversion — serialises dicts/lists, returns default for None."""
    if v is None:
        return default
    return str(v)


def _row_sort_key(a) -> tuple:
    """Chronological sort key: ISO date_label first, recorded_at fallback.

    Plain str() sorting misorders non-ISO labels (e.g. "Oct 5" sorts after
    "2026-09-01"), which flips early/late trend halves.
    """
    label = getattr(a, "date_label", "") or ""
    try:
        return (0, datetime.date.fromisoformat(str(label).strip()).isoformat())
    except ValueError:
        pass
    recorded = getattr(a, "recorded_at", None)
    return (1, recorded.isoformat() if recorded is not None else str(label))


class OptimizerService:
    def __init__(self, db: AsyncSession, company_id: str, ai=None):
        self.db = db
        self.company_id = company_id
        # Injectable AI client (DIP) — defaults to the shared provider adapter.
        self.ai = ai or get_ai_client()

    async def generate_suggestions(
        self,
        advertisement: Advertisement,
        analytics: list[AdAnalytics],
        reviews: list[Review],
    ) -> dict[str, Any]:
        """
        Produce deterministic, structured optimization suggestions.

        1. Compute cost signals (marginal returns, traffic windows)
        2. Compute content signals (engagement level vs benchmark, creative weaknesses)
        3. Call Claude with tightly constrained inputs and required output schema
        4. Return cost_optimization, website_optimization, advertisement_optimization
        """
        try:
            cost_analysis = self._analyze_cost(advertisement, analytics)
            content_signals = self._analyze_content_signals(advertisement, analytics)
            reviewer_context = self._extract_reviewer_context(reviews)
        except Exception as exc:
            logger.warning("Optimizer pre-analysis failed for ad %s (%s) — using empty context", advertisement.id, exc)
            cost_analysis = {"status": "no_data"}
            content_signals = {}
            reviewer_context = {}

        # Use deterministic mock when:
        #   • AI backend is not configured
        #   • No analytics rows to analyze (AI would have nothing concrete to cite)
        no_data = cost_analysis.get("status") == "no_data"

        if not self.ai.is_configured() or no_data:
            suggestions = self._deterministic_mock(cost_analysis, content_signals)
        else:
            try:
                suggestions = await self._generate_ai_suggestions(
                    advertisement, cost_analysis, content_signals, reviewer_context
                )
                # If all JSON extractors failed the response will be {"raw_response": ...}.
                # Rather than surface that to the user, fall back to the deterministic mock.
                if "raw_response" in suggestions and "cost_optimization" not in suggestions:
                    logger.warning(
                        "Optimizer AI returned unparseable output for ad %s — "
                        "falling back to deterministic mock. Raw snippet: %.200s",
                        advertisement.id,
                        suggestions.get("raw_response", ""),
                    )
                    suggestions = self._deterministic_mock(cost_analysis, content_signals)
            except Exception as exc:
                logger.warning(
                    "Optimizer AI call failed for ad %s (%s) — falling back to deterministic mock",
                    advertisement.id,
                    exc,
                )
                suggestions = self._deterministic_mock(cost_analysis, content_signals)

        return {
            "suggestions": suggestions,
            "context": {
                "cost_analysis": cost_analysis,
                "content_signals": content_signals,
                "reviewer_context": reviewer_context,
            },
        }

    # ── Deterministic cost analysis ───────────────────────────────────────────

    def _analyze_cost(self, ad: Advertisement, analytics: list[AdAnalytics]) -> dict[str, Any]:
        """
        Derive marginal return and traffic window signals from stored analytics rows.
        All computations are deterministic — same data always yields same output.
        """
        if not analytics:
            return {"status": "no_data"}

        rows = sorted(analytics, key=_row_sort_key)

        spends = [_f(getattr(a, "spend", None)) for a in rows]
        click_rates_pct = [_f(getattr(a, "click_rate", None)) for a in rows]
        impressions = [_f(getattr(a, "impressions", None)) for a in rows]
        cpms = [_f(getattr(a, "cpm", None)) for a in rows]
        date_labels = [getattr(a, "date_label", None) for a in rows]

        # ctrs as ratio (0-1) for trend analysis; click_rates_pct already in % form
        ctrs = [r / 100.0 for r in click_rates_pct]

        total_spend = sum(spends)
        total_imps = sum(impressions)
        # Estimate total clicks from per-row click_rate × impressions
        total_clicks = round(sum(ctrs[i] * impressions[i] for i in range(len(rows))))
        avg_ctr_pct = round(sum(click_rates_pct) / len(click_rates_pct) if click_rates_pct else 0.0, 4)
        avg_cpm = round(sum(cpms) / len(cpms) if cpms else 0.0, 2)
        avg_spend_day = round(total_spend / len(rows) if rows else 0.0, 2)

        # Marginal return: split rows into first/second half and compare CTR
        mid = max(len(rows) // 2, 1)
        early_ctr = sum(ctrs[:mid]) / mid
        late_ctr = sum(ctrs[mid:]) / (len(rows) - mid) if len(rows) > mid else early_ctr

        ctr_trend = (
            "declining" if late_ctr < early_ctr * 0.90 else "improving" if late_ctr > early_ctr * 1.10 else "stable"
        )
        early_spend = sum(spends[:mid]) / mid
        late_spend = sum(spends[mid:]) / (len(rows) - mid) if len(rows) > mid else early_spend
        spend_trend = (
            "increasing"
            if late_spend > early_spend * 1.10
            else "decreasing"
            if late_spend < early_spend * 0.90
            else "stable"
        )

        # Budget signal from marginal analysis
        if ctr_trend == "declining" and spend_trend in ("increasing", "stable"):
            budget_signal = "reduce_and_redistribute"
            budget_rationale = (
                f"CTR declined {round((1 - late_ctr / early_ctr) * 100 if early_ctr else 0, 1)}% "
                f"while spend held steady — diminishing marginal returns detected."
            )
        elif ctr_trend == "improving" and spend_trend in ("decreasing", "stable"):
            budget_signal = "increase_on_winners"
            budget_rationale = (
                f"CTR improved {round((late_ctr / early_ctr - 1) * 100 if early_ctr else 0, 1)}% "
                f"while spend was stable — scale budget on top-performing windows."
            )
        else:
            budget_signal = "rebalance"
            budget_rationale = (
                f"CTR is {ctr_trend} while spend is {spend_trend}. Rebalance spend toward top-CTR windows."
            )

        # Traffic windows: rank days by CTR
        day_perf = [
            {"date": date_labels[i], "ctr": ctrs[i], "spend": spends[i], "clicks": round(ctrs[i] * impressions[i])}
            for i in range(len(rows))
            if date_labels[i]
        ]
        day_perf.sort(key=lambda x: x["ctr"], reverse=True)
        top_windows = day_perf[:3]
        low_windows = day_perf[-3:] if len(day_perf) >= 6 else []

        return {
            "status": "analyzed",
            "data_points": len(rows),
            "total_spend": round(total_spend, 2),
            "total_clicks": int(total_clicks),
            "total_impressions": int(total_imps),
            "avg_ctr_pct": avg_ctr_pct,
            "avg_cpm": avg_cpm,
            "avg_spend_per_day": avg_spend_day,
            "ctr_trend": ctr_trend,
            "spend_trend": spend_trend,
            "budget_signal": budget_signal,
            "budget_rationale": budget_rationale,
            "top_traffic_windows": top_windows,
            "low_traffic_windows": low_windows,
        }

    # ── Content signal analysis ───────────────────────────────────────────────

    def _analyze_content_signals(self, ad: Advertisement, analytics: list[AdAnalytics]) -> dict[str, Any]:
        """
        Compute engagement level vs industry benchmark and identify the primary
        creative weakness. Used to guide deterministic content optimization prompts.
        """
        ad_type_list = ad.ad_type if isinstance(ad.ad_type, list) else []
        is_website = "website" in ad_type_list

        if not analytics:
            engagement_level = "unknown"
            ctr_benchmark = "unknown"
            primary_weakness = "insufficient data — optimise creative proactively"
        else:
            # click_rate is stored as percentage (e.g. 0.27 for 0.27%) — average across rows
            click_rates_pct = [_f(getattr(a, "click_rate", None)) for a in analytics]
            ctr = (sum(click_rates_pct) / len(click_rates_pct) / 100.0) if click_rates_pct else 0.0

            # Industry benchmarks for awareness/traffic ads
            if ctr < 0.005:
                engagement_level = "very_low"
                ctr_benchmark = f"{round(ctr * 100, 3)}% (industry avg ~0.9%)"
                primary_weakness = "headline / visual hook — very low CTR means the creative is not stopping the scroll"
            elif ctr < 0.009:
                engagement_level = "below_avg"
                ctr_benchmark = f"{round(ctr * 100, 3)}% (below avg 0.9%)"
                primary_weakness = (
                    "caption clarity and CTA — below-average CTR suggests copy or call-to-action is not compelling"
                )
            elif ctr < 0.020:
                engagement_level = "average"
                ctr_benchmark = f"{round(ctr * 100, 3)}% (at avg)"
                primary_weakness = (
                    "conversion path — CTR is acceptable, optimise landing page and hashtags for discoverability"
                )
            else:
                engagement_level = "above_avg"
                ctr_benchmark = f"{round(ctr * 100, 3)}% (above avg)"
                primary_weakness = (
                    "scale and fatigue prevention — strong CTR; prevent creative fatigue as frequency rises"
                )

        current_creative = {}
        if ad.strategy_json:
            s = ad.strategy_json
            current_creative = {
                "title": ad.title,
                "caption": _to_str(s.get("caption") or s.get("body"), ""),
                "hashtags": s.get("hashtags", []) if isinstance(s.get("hashtags"), list) else [],
                "cta": _to_str(s.get("cta"), "Book Now"),
                "colors": s.get("brand_colors", []) if isinstance(s.get("brand_colors"), list) else [],
                "font": _to_str(s.get("font_style"), ""),
                "audience": _to_str(s.get("target_audience"), ""),
                "goal": _to_str(s.get("goal"), ""),
            }

        # Website and creative asset context — guard all JSON fields against malformed DB data
        _wr = ad.website_reqs if isinstance(ad.website_reqs, dict) else {}
        _ad = ad.ad_details if isinstance(ad.ad_details, dict) else {}
        _of = ad.output_files if isinstance(ad.output_files, list) else []
        website_context = {
            "hosted_url": ad.hosted_url or None,
            "website_reqs": _wr,
            "ad_details": _ad,
            "output_files": _of,
        }

        return {
            "is_website": is_website,
            "engagement_level": engagement_level,
            "ctr_benchmark": ctr_benchmark,
            "primary_weakness": primary_weakness,
            "current_creative": current_creative,
            "website_context": website_context,
        }

    # ── Reviewer context ──────────────────────────────────────────────────────

    def _extract_reviewer_context(self, reviews: list[Review]) -> dict[str, Any]:
        context: dict[str, Any] = {
            "total_reviews": len(reviews),
            "revision_requests": [],
            "ethical_concerns": [],
            "human_suggestions": [],
        }
        for r in reviews:
            if r.status == "revision":
                context["revision_requests"].append(r.comments)
            if r.review_type == "ethics":
                context["ethical_concerns"].append(r.comments)
            if r.suggestions:
                context["human_suggestions"].append(r.suggestions)
        return context

    # ── AI suggestion generation ──────────────────────────────────────────────

    async def _generate_ai_suggestions(
        self,
        ad: Advertisement,
        cost_analysis: dict,
        content_signals: dict,
        reviewer_context: dict,
    ) -> dict[str, Any]:
        """
        Call Claude with tightly constrained inputs and a strict output schema.
        Every suggestion item is grounded in the pre-computed signals — no free-form
        invention allowed. Falls back to deterministic mock when Bedrock is absent.
        """
        if not self.ai.is_configured():
            return self._deterministic_mock(cost_analysis, content_signals)

        from datetime import date as _date

        today_str = _date.today().isoformat()

        system_prompt = (
            f"You are a precision marketing optimization engine. Today's date is {today_str}. "
            "You receive pre-computed analytics signals and return ONLY a structured JSON object. "
            "Do NOT invent data. Reference the exact numbers provided in every 'why' field. "
            "Output ONLY a raw JSON object — no markdown, no code fences, no commentary. "
            "Your entire response must start with { and end with }."
        )

        ca = cost_analysis
        cs = content_signals
        cc = cs.get("current_creative", {})
        wc = cs.get("website_context", {})

        top_w = ca.get("top_traffic_windows", [])
        low_w = ca.get("low_traffic_windows", [])

        window_summary = ""
        if top_w:
            window_summary += (
                "Top-CTR windows: "
                + ", ".join(
                    f"{w['date']} (CTR {round(w['ctr'] * 100, 3)}%, spend ${round(w['spend'], 2)})" for w in top_w
                )
                + ". "
            )
        if low_w:
            window_summary += (
                "Lowest-CTR windows: "
                + ", ".join(
                    f"{w['date']} (CTR {round(w['ctr'] * 100, 3)}%, spend ${round(w['spend'], 2)})" for w in low_w
                )
                + "."
            )

        creative_ctx = (
            f"Title: '{cc.get('title', ad.title)}' | "
            f"Caption (first 120 chars): '{cc.get('caption', '')[:120]}' | "
            f"Hashtags: {cc.get('hashtags', [])} | "
            f"CTA: '{cc.get('cta', '')}' | "
            f"Colors: {cc.get('colors', [])} | "
            f"Font: '{cc.get('font', '')}' | "
            f"Audience: '{cc.get('audience', '')}' | "
            f"Goal: '{cc.get('goal', '')}'"
        )

        # Website + asset context for richer website/ad creative suggestions
        website_reqs = wc.get("website_reqs") or {}
        ad_details = wc.get("ad_details") or {}
        output_files = wc.get("output_files") or []
        hosted_url = wc.get("hosted_url") or "not deployed yet"

        # Guard: website_reqs must be a dict (could be a list if DB JSON is malformed)
        if not isinstance(website_reqs, dict):
            website_reqs = {}
        if not isinstance(ad_details, dict):
            ad_details = {}
        if not isinstance(output_files, list):
            output_files = []

        website_ctx = (
            f"Hosted URL: {hosted_url} | "
            f"Website requirements: {str(website_reqs)[:300]} | "
            f"Layout/design spec: {str(website_reqs.get('layout') or website_reqs.get('design') or '')[:200]} | "
            f"Font spec: {website_reqs.get('font') or cc.get('font') or 'not set'} | "
            f"Color spec: {website_reqs.get('colors') or cc.get('colors') or 'not set'}"
        )

        def _file_label(f) -> str:
            if isinstance(f, dict):
                return str(f.get("name") or f.get("url") or f.get("filename") or str(f))
            return str(f)

        ad_creative_ctx = (
            f"Ad details: {str(ad_details)[:300]} | Output files: {[_file_label(f) for f in output_files[:5]]}"
        )

        reviewer_notes = (
            f"Revision requests: {reviewer_context.get('revision_requests', [])} | "
            f"Ethical concerns: {reviewer_context.get('ethical_concerns', [])} | "
            f"Human suggestions: {reviewer_context.get('human_suggestions', [])}"
        )

        user_message = f"""## Ad: {ad.title}
Ad type: {ad.ad_type}
Engagement level: {cs.get("engagement_level")} | CTR: {cs.get("ctr_benchmark")}
Primary creative weakness: {cs.get("primary_weakness")}
Current creative: {creative_ctx}
Website context: {website_ctx}
Ad creative assets: {ad_creative_ctx}

## Cost Signals
Total spend: ${ca.get("total_spend", 0)} over {ca.get("data_points", 0)} days
Avg daily spend: ${ca.get("avg_spend_per_day", 0)} | Avg CPM: ${ca.get("avg_cpm", 0)}
CTR trend: {ca.get("ctr_trend")} | Spend trend: {ca.get("spend_trend")}
Budget signal: {ca.get("budget_signal")}
Rationale: {ca.get("budget_rationale")}
{window_summary}

## Reviewer Notes
{reviewer_notes}

Return EXACTLY this JSON shape — no extra keys, no deviations:
{{
  "cost_optimization": {{
    "overall_assessment": "<1-2 sentence summary>",
    "items": [
      {{
        "what": "<one-sentence description of what to do>",
        "why": "<data-driven reason citing actual numbers>",
        "action_type": "set_today_budget",
        "action_value": <recommended daily budget in USD as a plain number, e.g. 12.5>
      }},
      {{
        "what": "Pause campaign every <day-of-week> from <HH:MM> to <HH:MM> and resume automatically — based on low-CTR pattern in data",
        "why": "<cite the exact CTR % and spend $ for that window vs campaign average>",
        "action_type": "schedule_pause",
        "action_value": {{
          "pause_label": "<e.g. 'Every Sunday' or 'Weekday evenings 21:00–06:00'>",
          "pause_days":  ["<day-of-week name, e.g. Sunday — NEVER a date string>"],
          "pause_hours": "<HH:MM-HH:MM range e.g. '00:00-23:59' for full day or '21:00-06:00' for overnight — NEVER null>"
        }}
      }}
    ],
    "budget_distribution": {{
      "increase_days": ["<day or date>"],
      "reduce_days":   ["<day or date>"],
      "reallocation_pct": "<e.g. 25%>"
    }},
    "traffic_windows": [
      {{
        "window": "<date or period>",
        "recommended_action": "<increase|reduce|pause>",
        "reasoning": "<why>"
      }}
    ]
  }},
  "website_optimization": [
    {{
      "what": "Try this caption: '<new caption text here, 1-3 sentences>'",
      "why":  "<data-driven reason>",
      "action_type": "edit_caption",
      "action_value": "<the new caption text to apply>"
    }},
    {{
      "what": "Add this content block: '<brief description of what to add and where>'",
      "why":  "<data-driven reason>",
      "action_type": "edit_content",
      "action_value": "<description of the content change to make — will prompt a website regeneration>"
    }}
  ],
  "bot_optimization": [
    {{
      "what": "Switch voice to <voice name> — <short reason>",
      "why":  "<data-driven reason>",
      "action_type": "switch_voice",
      "action_value": "<ElevenLabs voice_id string>"
    }}
  ],
  "advertisement_optimization": [
    {{
      "what": "Try this caption: '<new ad caption, 1-2 sentences>'",
      "why":  "<data-driven reason>",
      "action_type": "edit_ad_caption",
      "action_value": "<the new caption text>"
    }},
    {{
      "what": "Use these hashtags: <#tag1 #tag2 #tag3 ...>",
      "why":  "<data-driven reason>",
      "action_type": "edit_ad_hashtags",
      "action_value": ["<hashtag1>", "<hashtag2>", "<hashtag3>"]
    }},
    {{
      "what": "Regenerate the ad image: <specific visual change instruction>",
      "why":  "<data-driven reason>",
      "action_type": "regenerate_creative",
      "action_value": null
    }}
  ]
}}

Rules:
- cost_optimization.items: exactly 2 items — one set_today_budget, one schedule_pause. Both grounded in cost signals.
- set_today_budget action_value must be a plain number derived from past CTR/spend trends. Never a past date, never a percentage string.
- schedule_pause: derive the day-of-week from the lowest-CTR date in the data, decide a full-day or sub-day pause window, and express it as a recurring forward schedule (day-of-week + HH:MM-HH:MM). pause_days must be day-of-week names (e.g. "Sunday"), NEVER date strings. pause_hours must always be a time range string, never null. The "what" must read as a complete, decided action the system will take — not a suggestion for the user to consider.
- website_optimization: 2 items — one edit_caption (provide the actual new caption text), one edit_content.
- bot_optimization: 1 item — recommend the most suitable voice for the audience (pick from available voices).
- advertisement_optimization: 3 items — one edit_ad_caption (with actual caption text), one edit_ad_hashtags (actual hashtag list), one regenerate_creative.
- Every "why" must cite a specific metric (CTR %, spend $, trend).
- Items ordered by expected impact: highest delta first.
"""

        text = await self.ai.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=8000,
        )

        try:
            return extract_json(text)
        except json.JSONDecodeError:
            logger.warning("Optimizer LLM output not parseable as JSON — returning raw")
            return {"raw_response": text}

    # ── Deterministic mock (no Bedrock) ───────────────────────────────────────

    def _deterministic_mock(self, cost_analysis: dict, content_signals: dict) -> dict[str, Any]:
        """
        Fallback when Bedrock is not configured.
        Derives all suggestions from the pre-computed signals — no random strings.
        """
        ca = cost_analysis
        cs = content_signals
        cc = cs.get("current_creative", {})

        budget_signal = _to_str(ca.get("budget_signal"), "rebalance")
        ctr_trend = _to_str(ca.get("ctr_trend"), "stable")
        budget_rationale = _to_str(ca.get("budget_rationale"), "No analytics data yet.")
        avg_ctr = _f(ca.get("avg_ctr_pct", 0))
        total_spend = _f(ca.get("total_spend", 0))
        data_points = int(ca.get("data_points", 0) or 0)
        top_w = ca.get("top_traffic_windows", []) if isinstance(ca.get("top_traffic_windows"), list) else []
        low_w = ca.get("low_traffic_windows", []) if isinstance(ca.get("low_traffic_windows"), list) else []

        engagement = cs.get("engagement_level", "unknown")
        weakness = cs.get("primary_weakness", "creative quality")
        ctr_benchmark = cs.get("ctr_benchmark", "N/A")

        caption = _to_str(cc.get("caption"), "")
        hashtags = cc.get("hashtags", []) if isinstance(cc.get("hashtags"), list) else []
        cta = _to_str(cc.get("cta"), "Book Now")
        audience = _to_str(cc.get("audience"), "general audience")
        goal = _to_str(cc.get("goal"), "healthcare trial")

        # ── Cost items ────────────────────────────────────────────────────────

        no_analytics = data_points == 0

        from datetime import date as _date

        today_str_mock = _date.today().isoformat()

        # set_today_budget — derive recommended budget from avg spend + trend
        if no_analytics:
            budget_today = round(max(5.0, total_spend / max(data_points, 1)), 2)
            budget_what = f"Set today's ({today_str_mock}) daily budget to ${budget_today} to start collecting data"
            budget_why = (
                "No performance data yet — a modest initial budget will generate the CTR signals needed to optimise."
            )
        else:
            multiplier = (
                1.3
                if budget_signal == "increase_on_winners"
                else (0.7 if budget_signal == "reduce_and_redistribute" else 1.0)
            )
            budget_today = round(max(1.0, (total_spend / max(data_points, 1))) * multiplier, 2)
            budget_what = (
                f"Set today's ({today_str_mock}) daily budget to ${budget_today} based on {ctr_trend} CTR trend"
            )
            budget_why = budget_rationale

        # schedule_pause — resolve day-of-week from lowest-CTR date, set concrete hours
        _DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if low_w:
            lw = low_w[0] if isinstance(low_w[0], dict) else {}
            try:
                from datetime import date as _dt

                _d = _dt.fromisoformat(str(lw.get("date", "")))
                dow = _DAY_NAMES[_d.weekday()]
            except Exception:
                dow = "Sunday"
            lw_ctr = _f(lw.get("ctr", 0))
            lw_spend = _f(lw.get("spend", 0))
            lw_ctr_pct = round(lw_ctr * 100, 3)
            pause_label = f"Every {dow}"
            pause_days = [dow]
            pause_hours = "00:00-23:59"
            pause_what = (
                f"Pause campaign every {dow} (00:00–23:59) and resume Monday 00:00 — "
                f"{dow}s show {lw_ctr_pct}% CTR vs {avg_ctr}% average"
            )
            pause_why = (
                f"{dow}s recorded only {lw_ctr_pct}% CTR on ${round(lw_spend, 2)} spend "
                f"vs campaign avg {avg_ctr}% — every dollar spent on {dow}s returns "
                f"less than {round(lw_ctr_pct / max(avg_ctr, 0.001), 1)}x the campaign average"
            )
        else:
            pause_label = "Every Sunday"
            pause_days = ["Sunday"]
            pause_hours = "00:00-23:59"
            pause_what = "Pause campaign every Sunday (00:00–23:59) and resume Monday 00:00 — weekends show lowest healthcare trial engagement"
            pause_why = "Healthcare trial audiences engage significantly less on Sundays — pausing prevents wasted impressions on low-intent days"

        cost_items = [
            {
                "what": budget_what,
                "why": budget_why,
                "action_type": "set_today_budget",
                "action_value": budget_today,
            },
            {
                "what": pause_what,
                "why": pause_why,
                "action_type": "schedule_pause",
                "action_value": {
                    "pause_label": pause_label,
                    "pause_days": pause_days,
                    "pause_hours": pause_hours,
                },
            },
        ]

        # ── Content items ─────────────────────────────────────────────────────
        below_avg = engagement in ("very_low", "below_avg")

        # Website — always: one caption suggestion + one content suggestion
        new_caption = (
            f"Are you or someone you know part of '{audience}'? "
            f"{'Join' if not caption else caption[:60].rstrip('. ') + ' — find out if you qualify.'} "
            f"{cta}."
        )
        website_items = [
            {
                "what": f'Try this caption: "{new_caption}"',
                "why": f"CTR {ctr_benchmark} — front-loading audience relevance in the first sentence increases click intent",
                "action_type": "edit_caption",
                "action_value": new_caption,
            },
            {
                "what": (
                    "Add a 'Why this trial?' FAQ block near the top of the page to reduce bounce rate"
                    if below_avg
                    else "Add a social-proof block showing trial progress near the top of the page to reduce bounce rate"
                ),
                "why": (
                    f"Below-average CTR ({ctr_benchmark}) — answering common concerns early converts more visitors"
                    if below_avg
                    else f"Above-average CTR ({ctr_benchmark}) — social proof compounds existing engagement"
                ),
                "action_type": "edit_content",
                "action_value": (
                    "Add a FAQ block answering 'Why join this trial?', 'What happens during the study?', and 'Who is eligible?' near the top of the page."
                    if below_avg
                    else "Add a social-proof block showing enrollment progress (e.g. X participants enrolled so far) and a quote from the research team near the top of the page."
                ),
            },
        ]

        # Bot — recommend the most empathetic voice for healthcare audiences
        bot_items = [
            {
                "what": (
                    "Switch voice to Grace — warm, friendly tone better suited for healthcare trial outreach"
                    if below_avg
                    else "Switch voice to Rachel — calm, professional tone aligned with above-average engagement profile"
                ),
                "why": f"CTR {ctr_benchmark} for '{audience}' — voice tone is the primary trust signal in outbound calls",
                "action_type": "switch_voice",
                "action_value": "oWAxZDx7w5VEj9dCyTzz" if below_avg else "EXAVITQu4vr4xnSDxMaL",
            },
        ]

        # Ad creative — caption suggestion + hashtag list + image regeneration
        new_ad_caption = (
            f"{'Struggling with' if below_avg else 'Living with'} {goal.replace('trial', '').strip()}? "
            f"A new study may help. {cta}."
        )
        suggested_hashtags = [
            "#ClinicalTrial",
            f"#{goal.replace(' ', '')[:15]}",
            "#HealthResearch",
            "#MedicalStudy",
            f"#{(audience or 'Healthcare').replace(' ', '')[:15]}",
        ]
        ad_items = [
            {
                "what": f'Try this caption: "{new_ad_caption}"',
                "why": f"CTR {ctr_benchmark} — 2-sentence problem→solution structure improves read-through before the CTA",
                "action_type": "edit_ad_caption",
                "action_value": new_ad_caption,
            },
            {
                "what": f"Use these hashtags: {' '.join(suggested_hashtags)}",
                "why": f"Current tags {hashtags or '(none)'} — {'broader reach needed' if below_avg else 'niche tags compound existing engagement'}",
                "action_type": "edit_ad_hashtags",
                "action_value": suggested_hashtags,
            },
            {
                "what": (
                    "Regenerate the ad image: warm tones, clear human focal point, minimal text overlay"
                    if below_avg
                    else "Regenerate the ad image: introduce a clinical/professional variant to A/B test against current creative"
                ),
                "why": f"Primary creative weakness is '{weakness}' — image is the fastest scroll-stop lever",
                "action_type": "regenerate_creative",
                "action_value": None,
            },
        ]

        # ── Assemble output ───────────────────────────────────────────────────
        increase_days = [str(w.get("date", "")) for w in top_w if isinstance(w, dict)]
        reduce_days = [str(w.get("date", "")) for w in low_w if isinstance(w, dict)]
        realloc_pct = "25–30%" if budget_signal != "rebalance" else "10–15%"

        return {
            "cost_optimization": {
                "overall_assessment": (
                    f"Campaign has spent ${total_spend} across {data_points} days "
                    f"with a {ctr_trend} CTR trend. {budget_rationale}"
                ),
                "items": cost_items,
                "budget_distribution": {
                    "increase_days": increase_days,
                    "reduce_days": reduce_days,
                    "reallocation_pct": realloc_pct,
                },
                "traffic_windows": [
                    {
                        "window": w["date"],
                        "recommended_action": "increase",
                        "reasoning": f"CTR {round(w['ctr'] * 100, 3)}% vs avg {avg_ctr}%",
                    }
                    for w in top_w
                ]
                + [
                    {
                        "window": w["date"],
                        "recommended_action": "reduce",
                        "reasoning": f"CTR {round(w['ctr'] * 100, 3)}% — below marginal return threshold",
                    }
                    for w in low_w
                ],
            },
            "website_optimization": website_items,
            "bot_optimization": bot_items,
            "advertisement_optimization": ad_items,
        }
