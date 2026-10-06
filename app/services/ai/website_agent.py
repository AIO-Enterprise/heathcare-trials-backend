"""
Website Agent - Static Landing Page Generator
Owner: AI Dev

Architecture:
  - Python injects brand colors into a fixed CSS template (Claude writes no CSS)
  - Claude writes ONLY the <body> HTML using pre-defined CSS classes
  - Survey section is AI-generated from eligibility/website_reqs criteria
  - Survey JS + chat widget JS injected by Python (always correct, never truncated)
  - Voicebot / chatbot interaction section is hidden; JS reveals it after survey submit

Page order: nav → hero → survey → [interaction hidden] → features → cta → footer
"""

import json
import logging
import os

from app.core.config import settings
from app.core.llm import get_ai_client
from app.core.media import media_url
from app.models.models import Advertisement, BrandKit, Company

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────
_BODY_SYSTEM_PROMPT = """You are an expert clinical-trial / healthcare marketing copywriter and HTML developer.

Write the BODY CONTENT for a campaign landing page.
Output ONLY the inner HTML — starting with <nav> and ending with </footer>.
Do NOT output DOCTYPE, <html>, <head>, <style>, or <script> tags.
No inline styles. No custom CSS classes. Use ONLY the classes listed below.

━━ AVAILABLE CSS CLASSES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NAV:        .logo  .nav-cta
HERO:       .hero  .hero-badge  .hero-headline  .hero-sub
            .benefit-chips  .chip  .btn.btn-primary  .btn.btn-outline
SECTION:    .section  .section-title  .section-sub
CARDS:      .card-grid  .card  .card-icon  .card-title  .card-desc
CHECKLIST:  .checklist  .checklist-item
CTA:        .cta-section  .cta-inner  .cta-title  .cta-sub
FOOTER:     footer  .trust-bar  .trust-item  .footer-copy

SURVEY (always include):
  Wrapper:    <section class="survey-section" id="survey-section">
  Card:       .survey-card
  Header:     .survey-header  .survey-title  .survey-sub
  Progress:   <div class="survey-progress-bar"><div class="survey-progress-fill" id="survey-progress"></div></div>
              <p class="survey-step-count" id="survey-step-count">Question 1 of N</p>
  Steps:      <div class="survey-step" data-step="N" data-eligible="COMMA,SEPARATED,ELIGIBLE,VALUES">
                <p class="survey-question">Question text?</p>
                <div class="option-grid">
                  <button class="option-btn" data-value="VALUE">Label</button>
                  ...
                </div>
              </div>
              (make first step have class "survey-step active")
  Result:     <div class="survey-result" id="survey-result">
                <div class="result-card eligible" id="result-eligible">...</div>
                <div class="result-card ineligible" id="result-ineligible">...</div>
              </div>
  Nav:        Place the voice button on the LEFT and the prev/next buttons on the RIGHT inside one nav row:
              <div class="survey-nav">
                <div class="survey-voice-row">
                  <button class="btn-voice-call" id="survey-voice-btn">&#128222; Speak to Us</button>
                </div>
                <div class="survey-nav-right">
                  <button class="btn-survey-prev" id="survey-prev">&#8592; Back</button>
                  <button class="btn-survey-next" id="survey-next">Next &#8594;</button>
                  <button class="btn-survey-submit" id="survey-submit">See My Result &#8594;</button>
                </div>
              </div>
              (survey-voice-row is NEVER hidden — it stays visible through every step AND after results.)

INTERACTION (voicebot — place AFTER survey with id="interaction-reveal"):
  <section class="interaction-section" id="interaction-reveal">
  .interaction-section__title  .interaction-section__sub  .interaction-cards
  .interaction-card  .interaction-card.featured
  .interaction-card__icon  .interaction-card__title  .interaction-card__desc
  .interaction-card__btn  .interaction-card__meta
  CRITICAL IDs — do NOT change these, the voice JS depends on them exactly:
    Card 1 "Instant Call" button: id="voice-call-btn"
    Card 2 "Schedule a Call" button: id="schedule-call-btn"

CHATBOT: The chat widget floats in the bottom-right corner — the system injects it automatically.
  Do NOT add any chat HTML to the page body.
  Instead place this reveal anchor immediately after the survey section:
    <section class="cta-section" id="interaction-reveal">
      <div class="cta-inner">
        <h2 class="cta-title">Chat with [Bot Name] Now</h2>
        <p class="cta-sub">Click the chat button in the bottom-right corner — [Bot Name] is ready to help.</p>
      </div>
    </section>
  (id="interaction-reveal" is required so JS can scroll to it and open the chat panel.)

━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Generate 4–6 survey questions based on the eligibility criteria provided.
2. Each question must have 2–4 answer options. Mark eligible answers in data-eligible.
3. result-eligible: encouraging message + "Connect with us below"
4. result-ineligible: empathetic message + a .consent-box with:
   <label class="consent-label"><input type="checkbox" class="consent-checkbox" id="consent-check" /> "You don't meet the criteria for this trial, but you might qualify for our upcoming trials. May we retain your details to notify you?"</label>
   <button class="consent-submit" id="consent-submit">Yes, Keep Me Updated</button>
   <p class="consent-thanks" id="consent-thanks">✓ Thank you! We'll be in touch when a matching trial opens.</p>
5. The interaction section (voicebot/chatbot) must have id="interaction-reveal" — JS hides it initially and reveals it after survey.
6. Use emoji icons in .card-icon and .interaction-card__icon.
7. Fill ALL content with real campaign copy — no placeholder text.
"""


class WebsiteAgentService:
    def __init__(self, company_id: str, ai=None):
        self.company_id = company_id
        # Injectable AI client (DIP) — defaults to the shared provider adapter.
        self.ai = ai or get_ai_client()

    async def generate_website(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        output_dir = os.path.join(settings.OUTPUT_DIR, self.company_id, ad.id, "website")
        os.makedirs(output_dir, exist_ok=True)

        ad_types = [t.lower() for t in (ad.ad_type or [])]
        bot_config = ad.bot_config or {}
        bot_name = bot_config.get("name", "Assistant") if isinstance(bot_config, dict) else "Assistant"
        bot_welcome = (
            bot_config.get("welcome_message", f"Hi! I'm {bot_name}. How can I help you today?")
            if isinstance(bot_config, dict)
            else f"Hi! I'm {bot_name}."
        )
        css = self._build_css(brand_kit)
        body = await self._generate_body(ad, brand_kit, company)
        from datetime import date, timedelta

        today = date.today()
        win_start = max(ad.trial_start_date, today) if ad.trial_start_date else today
        win_end = ad.trial_end_date if ad.trial_end_date else today + timedelta(days=30)
        html = self._wrap_html(
            ad.title,
            css,
            body,
            ad_types,
            bot_name,
            bot_welcome,
            ad_id=ad.id,
            booking_window_start=str(win_start),
            booking_window_end=str(win_end),
        )

        index_path = os.path.join(output_dir, "index.html")
        # Drop lone surrogate code points that Claude occasionally emits —
        # they are invalid in UTF-8 and would raise UnicodeEncodeError on write.
        html = html.encode("utf-8", errors="ignore").decode("utf-8")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)

        return media_url(f"/outputs/{self.company_id}/{ad.id}/website/index.html")

    # ── CSS template (brand colors injected, never written by Claude) ─────────

    def _build_css(self, brand_kit: BrandKit | None) -> str:
        primary = (brand_kit.primary_color if brand_kit else None) or "#1e3a5f"
        secondary = (brand_kit.secondary_color if brand_kit else None) or "#0f172a"
        accent = (brand_kit.accent_color if brand_kit else None) or "#10b981"
        font = (brand_kit.primary_font if brand_kit else None) or "Inter"
        font_url = font.replace(" ", "+")

        return f"""
    @import url('https://fonts.googleapis.com/css2?family={font_url}:wght@400;500;600;700;800&display=swap');

    :root {{
      --primary:   {primary};
      --secondary: {secondary};
      --accent:    {accent};
      --bg:        #f0f4f8;
      --white:     #ffffff;
      --text:      #1e293b;
      --muted:     #64748b;
      --border:    #e2e8f0;
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: '{font}', system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; -webkit-font-smoothing: antialiased; }}

    /* NAV */
    nav {{ background: var(--secondary); padding: 16px 48px; display: flex; align-items: center; justify-content: space-between; }}
    .logo {{ color: #fff; font-size: 1.05rem; font-weight: 700; display: flex; align-items: center; gap: 10px; text-decoration: none; }}
    .logo img {{ height: 36px; object-fit: contain; }}
    .nav-cta {{ background: var(--accent); color: #fff; padding: 9px 22px; border-radius: 50px; font-size: 0.88rem; font-weight: 600; text-decoration: none; border: none; cursor: pointer; transition: opacity 0.2s; }}
    .nav-cta:hover {{ opacity: 0.88; }}

    /* HERO */
    .hero {{ background: linear-gradient(140deg, var(--primary) 0%, var(--secondary) 100%); color: #fff; padding: 80px 48px; text-align: center; }}
    .hero-badge {{ display: inline-flex; align-items: center; gap: 6px; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22); border-radius: 50px; padding: 5px 16px; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; margin-bottom: 24px; }}
    .hero-headline {{ font-size: clamp(2rem, 5vw, 3.25rem); font-weight: 800; line-height: 1.15; letter-spacing: -0.02em; margin-bottom: 18px; }}
    .hero-headline span {{ color: var(--accent); }}
    .hero-sub {{ font-size: 1.05rem; color: rgba(255,255,255,0.78); max-width: 560px; margin: 0 auto 32px; }}
    .benefit-chips {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-bottom: 36px; }}
    .chip {{ display: inline-flex; align-items: center; gap: 5px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50px; padding: 5px 14px; font-size: 0.8rem; color: rgba(255,255,255,0.9); }}
    .chip::before {{ content: '✓'; color: var(--accent); font-weight: 700; }}

    /* BUTTONS */
    .btn {{ display: inline-block; padding: 13px 34px; border-radius: 50px; font-size: 0.97rem; font-weight: 700; text-decoration: none; border: none; cursor: pointer; transition: opacity 0.18s, transform 0.1s; }}
    .btn:hover {{ opacity: 0.88; transform: translateY(-1px); }}
    .btn-primary {{ background: var(--accent); color: #fff; }}
    .btn-outline {{ background: transparent; color: #fff; border: 2px solid rgba(255,255,255,0.45); }}
    .btn-dark {{ background: var(--secondary); color: #fff; }}

    /* SECTIONS */
    .section {{ padding: 72px 48px; max-width: 1100px; margin: 0 auto; }}
    .section-title {{ font-size: clamp(1.5rem, 3vw, 2rem); font-weight: 700; text-align: center; color: var(--primary); margin-bottom: 10px; }}
    .section-sub {{ text-align: center; color: var(--muted); font-size: 1rem; max-width: 540px; margin: 0 auto 44px; }}

    /* CARDS */
    .card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 20px; }}
    .card {{ background: var(--white); border: 1px solid var(--border); border-radius: 14px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); transition: box-shadow 0.2s; }}
    .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,0.1); }}
    .card-icon {{ width: 44px; height: 44px; border-radius: 10px; background: rgba(16,185,129,0.1); display: flex; align-items: center; justify-content: center; font-size: 1.25rem; margin-bottom: 14px; }}
    .card-title {{ font-size: 0.95rem; font-weight: 700; color: var(--text); margin-bottom: 6px; }}
    .card-desc {{ font-size: 0.85rem; color: var(--muted); line-height: 1.6; }}

    /* CHECKLIST */
    .checklist {{ list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }}
    .checklist-item {{ background: var(--white); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; display: flex; align-items: center; gap: 10px; font-size: 0.9rem; font-weight: 500; color: var(--text); }}
    .checklist-item::before {{ content: '✓'; color: var(--accent); font-weight: 700; font-size: 1rem; flex-shrink: 0; }}

    /* CTA */
    .cta-section {{ background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%); color: #fff; text-align: center; padding: 80px 48px; }}
    .cta-inner {{ max-width: 640px; margin: 0 auto; }}
    .cta-title {{ font-size: clamp(1.75rem, 4vw, 2.5rem); font-weight: 800; line-height: 1.18; margin-bottom: 14px; }}
    .cta-sub {{ font-size: 1.05rem; color: rgba(255,255,255,0.75); margin-bottom: 32px; }}

    /* FOOTER */
    footer {{ background: var(--secondary); padding: 28px 48px; }}
    .trust-bar {{ display: flex; justify-content: center; gap: 40px; flex-wrap: wrap; margin-bottom: 18px; }}
    .trust-item {{ display: flex; align-items: center; gap: 7px; font-size: 0.82rem; color: rgba(255,255,255,0.6); }}
    .trust-item strong {{ color: rgba(255,255,255,0.9); }}
    .footer-copy {{ text-align: center; font-size: 0.75rem; color: rgba(255,255,255,0.38); }}

    /* ═══ SURVEY ═══════════════════════════════════════════════════════════ */
    .survey-section {{ background: var(--white); padding: 72px 48px; }}
    .survey-card {{ max-width: 680px; margin: 0 auto; background: var(--white); border: 1px solid var(--border); border-radius: 20px; padding: 44px 48px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .survey-header {{ margin-bottom: 32px; }}
    .survey-title {{ font-size: 1.6rem; font-weight: 800; color: var(--primary); margin-bottom: 8px; text-align: center; }}
    .survey-sub {{ color: var(--muted); text-align: center; font-size: 0.95rem; margin-bottom: 24px; }}
    .survey-progress-bar {{ height: 6px; background: var(--border); border-radius: 50px; overflow: hidden; margin-bottom: 10px; }}
    .survey-progress-fill {{ height: 100%; background: var(--accent); border-radius: 50px; width: 0%; transition: width 0.4s ease; }}
    .survey-step-count {{ font-size: 0.78rem; color: var(--muted); text-align: right; font-weight: 500; }}
    .survey-step {{ display: none; }}
    .survey-step.active {{ display: block; }}
    .survey-question {{ font-size: 1.1rem; font-weight: 700; color: var(--text); margin-bottom: 20px; line-height: 1.45; }}
    .option-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
    .option-btn {{ padding: 14px 18px; border: 2px solid var(--border); border-radius: 12px; background: var(--white); font-size: 0.9rem; font-weight: 500; color: var(--text); cursor: pointer; text-align: left; transition: border-color 0.15s, background 0.15s; font-family: inherit; }}
    .option-btn:hover {{ border-color: var(--accent); background: rgba(16,185,129,0.04); }}
    .option-btn.selected {{ border-color: var(--accent); background: rgba(16,185,129,0.08); color: var(--primary); font-weight: 700; }}
    .survey-voice-row {{ display: flex; align-items: center; }}
    .btn-voice-call {{ display: inline-flex; align-items: center; gap: 6px; background: transparent; color: var(--primary); border: 1.5px solid var(--primary); padding: 7px 14px; border-radius: 50px; font-size: 0.78rem; font-weight: 600; cursor: pointer; font-family: inherit; transition: background 0.15s, color 0.15s; white-space: nowrap; }}
    .btn-voice-call:hover {{ background: var(--primary); color: #fff; }}
    .survey-nav {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--border); }}
    .survey-nav-right {{ display: flex; align-items: center; gap: 12px; }}
    .btn-survey-prev {{ background: transparent; border: 1.5px solid var(--border); color: var(--muted); padding: 10px 24px; border-radius: 50px; font-size: 0.9rem; font-weight: 600; cursor: pointer; font-family: inherit; transition: border-color 0.15s; }}
    .btn-survey-prev:hover {{ border-color: var(--primary); color: var(--primary); }}
    .btn-survey-next, .btn-survey-submit {{ background: var(--accent); color: #fff; padding: 11px 28px; border-radius: 50px; font-size: 0.9rem; font-weight: 700; border: none; cursor: pointer; font-family: inherit; transition: opacity 0.15s; }}
    .btn-survey-next:hover, .btn-survey-submit:hover {{ opacity: 0.88; }}
    .btn-survey-next:disabled, .btn-survey-submit:disabled {{ opacity: 0.4; cursor: not-allowed; }}
    .survey-result {{ display: none; text-align: center; padding: 8px 0; }}
    .result-card {{ border-radius: 16px; padding: 32px 28px; display: none; }}
    .result-card.eligible {{ background: rgba(16,185,129,0.08); border: 2px solid var(--accent); }}
    .result-card.ineligible {{ background: rgba(245,158,11,0.08); border: 2px solid #f59e0b; }}
    .result-icon {{ font-size: 2.5rem; margin-bottom: 12px; }}
    .result-card h3 {{ font-size: 1.25rem; font-weight: 800; margin-bottom: 8px; color: var(--text); }}
    .result-card p {{ font-size: 0.95rem; color: var(--muted); line-height: 1.6; }}
    .result-card.eligible .result-icon::before {{ content: '✅'; }}
    .result-card.ineligible .result-icon::before {{ content: '💛'; }}
    .consent-box {{ margin-top: 20px; background: rgba(245,158,11,0.06); border: 1.5px solid #f59e0b; border-radius: 12px; padding: 18px 20px; text-align: left; }}
    .consent-label {{ display: flex; align-items: flex-start; gap: 12px; cursor: pointer; font-size: 0.9rem; color: var(--text); line-height: 1.5; }}
    .consent-checkbox {{ width: 18px; height: 18px; accent-color: var(--accent); cursor: pointer; flex-shrink: 0; margin-top: 2px; }}
    .consent-submit {{ display: none; margin-top: 14px; width: 100%; padding: 11px; border-radius: 50px; background: var(--accent); color: #fff; font-size: 0.9rem; font-weight: 700; border: none; cursor: pointer; font-family: inherit; transition: opacity 0.2s; }}
    .consent-submit:hover {{ opacity: 0.88; }}
    .consent-thanks {{ display: none; margin-top: 14px; text-align: center; font-size: 0.88rem; color: var(--accent); font-weight: 600; }}

    /* ═══ REGISTRATION FORM (shown after survey) ════════════════════════════ */
    #reg-section {{ display: none; background: var(--white); padding: 40px 48px; }}
    .reg-card {{ max-width: 540px; margin: 0 auto; background: var(--white); border: 1px solid var(--border); border-radius: 20px; padding: 40px 44px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .reg-title {{ font-size: 1.4rem; font-weight: 800; color: var(--primary); margin-bottom: 6px; }}
    .reg-sub {{ font-size: 0.9rem; color: var(--muted); margin-bottom: 24px; line-height: 1.6; }}
    .reg-note {{ padding: 12px 16px; border-radius: 9px; font-size: 0.85rem; font-weight: 500; margin-bottom: 22px; line-height: 1.5; }}
    .reg-note.eligible {{ background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); color: #065f46; }}
    .reg-note.ineligible {{ background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); color: #92400e; }}
    .reg-field {{ margin-bottom: 16px; }}
    .reg-label {{ display: block; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 6px; }}
    .reg-input, .reg-select {{ width: 100%; padding: 11px 14px; border: 1.5px solid var(--border); border-radius: 10px; font-size: 0.9rem; font-family: inherit; color: var(--text); outline: none; transition: border-color 0.15s; box-sizing: border-box; background: var(--white); appearance: none; }}
    .reg-input:focus, .reg-select:focus {{ border-color: var(--accent); }}
    .reg-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .reg-btn {{ width: 100%; background: var(--accent); color: #fff; border: none; border-radius: 50px; padding: 13px; font-size: 0.95rem; font-weight: 700; cursor: pointer; font-family: inherit; transition: opacity 0.15s; margin-top: 8px; }}
    .reg-btn:hover {{ opacity: 0.88; }}
    .reg-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .reg-error {{ background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.25); color: #dc2626; padding: 11px 14px; border-radius: 9px; font-size: 0.83rem; margin-bottom: 14px; display: none; }}
    .reg-done {{ background: rgba(34,197,94,0.08); border: 1px solid rgba(34,197,94,0.25); color: #16a34a; padding: 20px 18px; border-radius: 14px; font-size: 0.9rem; line-height: 1.6; display: none; text-align: center; font-weight: 600; }}
    .reg-privacy {{ font-size: 0.72rem; color: var(--muted); text-align: center; margin-top: 12px; line-height: 1.5; }}
    @media (max-width: 768px) {{
      #reg-section {{ padding: 28px 16px; }}
      .reg-card {{ padding: 28px 20px; }}
      .reg-row {{ grid-template-columns: 1fr; }}
    }}

    /* ═══ VOICEBOT ═══════════════════════════════════════════════════════════ */
    .interaction-section {{ background: var(--bg); padding: 72px 48px; text-align: center; }}
    .interaction-section__title {{ font-size: clamp(1.5rem, 3vw, 2rem); font-weight: 800; color: var(--text); margin-bottom: 10px; }}
    .interaction-section__sub {{ color: var(--muted); font-size: 1rem; max-width: 500px; margin: 0 auto 44px; }}
    .interaction-cards {{ display: flex; justify-content: center; gap: 24px; flex-wrap: wrap; max-width: 760px; margin: 0 auto 28px; }}
    .interaction-card {{ background: var(--white); border: 2px solid var(--border); border-radius: 18px; padding: 36px 28px; width: 300px; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 12px; transition: box-shadow 0.2s, border-color 0.2s; }}
    .interaction-card:hover {{ box-shadow: 0 6px 24px rgba(0,0,0,0.1); }}
    .interaction-card.featured {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }}
    .interaction-card__icon {{ width: 64px; height: 64px; border-radius: 50%; background: rgba(16,185,129,0.12); display: flex; align-items: center; justify-content: center; font-size: 1.75rem; margin-bottom: 4px; }}
    .interaction-card__title {{ font-size: 1.1rem; font-weight: 700; color: var(--text); }}
    .interaction-card__desc {{ font-size: 0.86rem; color: var(--muted); line-height: 1.6; }}
    .interaction-card__btn {{ width: 100%; padding: 12px 20px; border-radius: 50px; font-size: 0.92rem; font-weight: 700; border: none; cursor: pointer; margin-top: 4px; transition: opacity 0.2s; text-decoration: none; display: inline-block; color: #fff; font-family: inherit; }}
    .interaction-card.featured .interaction-card__btn {{ background: var(--accent); }}
    .interaction-card:not(.featured) .interaction-card__btn {{ background: var(--secondary); }}
    .interaction-card__meta {{ font-size: 0.75rem; color: var(--muted); }}
    .interaction-note {{ font-size: 0.8rem; color: var(--muted); max-width: 520px; margin: 0 auto; }}
    .mic-btn {{ width: 64px; height: 64px; border-radius: 50%; background: var(--accent); border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1.6rem; color: #fff; position: relative; margin: 0 auto; }}
    .mic-btn::after {{ content: ''; position: absolute; inset: -6px; border-radius: 50%; border: 2px solid var(--accent); opacity: 0; animation: mic-pulse 2s ease-out infinite; }}
    @keyframes mic-pulse {{ 0% {{ transform: scale(1); opacity: 0.6; }} 100% {{ transform: scale(1.5); opacity: 0; }} }}

    /* ═══ CHATBOT — floating widget ════════════════════════════════════════ */
    #chat-float {{ position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; align-items: flex-end; gap: 12px; }}
    .chat-toggle-btn {{ width: 58px; height: 58px; border-radius: 50%; background: var(--accent); border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; box-shadow: 0 4px 20px rgba(0,0,0,0.22); transition: transform 0.2s; flex-shrink: 0; }}
    .chat-toggle-btn:hover {{ transform: scale(1.08); }}
    .chat-unread {{ position: absolute; top: 0; right: 0; width: 16px; height: 16px; background: #ef4444; border-radius: 50%; border: 2px solid #fff; display: flex; align-items: center; justify-content: center; font-size: 0.6rem; color: #fff; font-weight: 700; }}
    .chat-panel {{ background: var(--white); border-radius: 18px; box-shadow: 0 8px 40px rgba(0,0,0,0.18); width: 340px; overflow: hidden; flex-direction: column; display: none; transform-origin: bottom right; }}
    .chat-panel.open {{ display: flex; animation: chat-pop 0.22s ease; }}
    @keyframes chat-pop {{ from {{ opacity: 0; transform: scale(0.88); }} to {{ opacity: 1; transform: scale(1); }} }}
    .chat-panel__header {{ background: linear-gradient(135deg, var(--primary), var(--secondary)); padding: 14px 16px; display: flex; align-items: center; gap: 10px; }}
    .chat-avatar {{ width: 38px; height: 38px; border-radius: 50%; background: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 1.1rem; flex-shrink: 0; }}
    .chat-agent-info {{ flex: 1; }}
    .chat-agent-name {{ color: #fff; font-size: 0.92rem; font-weight: 700; }}
    .chat-agent-status {{ color: rgba(255,255,255,0.7); font-size: 0.72rem; display: flex; align-items: center; gap: 4px; }}
    .chat-agent-status::before {{ content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); display: inline-block; }}
    .chat-close-btn {{ background: rgba(255,255,255,0.15); border: none; color: #fff; width: 28px; height: 28px; border-radius: 50%; cursor: pointer; font-size: 0.85rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background 0.15s; }}
    .chat-close-btn:hover {{ background: rgba(255,255,255,0.28); }}
    .chat-messages {{ height: 260px; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 8px; background: #f8fafc; }}
    .chat-bubble {{ max-width: 82%; padding: 9px 13px; border-radius: 14px; font-size: 0.86rem; line-height: 1.5; }}
    .chat-bubble.bot {{ background: var(--white); border: 1px solid var(--border); color: var(--text); align-self: flex-start; border-bottom-left-radius: 3px; }}
    .chat-bubble.user {{ background: var(--accent); color: #fff; align-self: flex-end; border-bottom-right-radius: 3px; }}
    .chat-footer {{ padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; background: var(--white); }}
    .chat-input {{ flex: 1; border: 1px solid var(--border); border-radius: 50px; padding: 8px 14px; font-size: 0.86rem; font-family: inherit; outline: none; color: var(--text); background: #f8fafc; }}
    .chat-input:focus {{ border-color: var(--accent); background: var(--white); }}
    .chat-send-btn {{ background: var(--accent); color: #fff; border: none; border-radius: 50%; width: 34px; height: 34px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 0.95rem; flex-shrink: 0; transition: opacity 0.2s; font-family: inherit; }}
    .chat-send-btn:hover {{ opacity: 0.85; }}

    /* RESPONSIVE */
    @media (max-width: 768px) {{
      nav {{ padding: 14px 20px; }}
      .hero {{ padding: 52px 20px; }}
      .section {{ padding: 52px 20px; }}
      .cta-section {{ padding: 52px 20px; }}
      .survey-section {{ padding: 40px 16px; }}
      .survey-card {{ padding: 28px 20px; }}
      .interaction-section {{ padding: 52px 20px; }}
      #chat-float {{ bottom: 16px; right: 16px; }}
      .chat-panel {{ width: 300px; }}
      footer {{ padding: 24px 20px; }}
      .interaction-cards {{ flex-direction: column; align-items: center; }}
    }}
"""

    # ── HTML wrapper ───────────────────────────────────────────────────────────

    def _wrap_html(
        self,
        title: str,
        css: str,
        body: str,
        ad_types: list[str],
        bot_name: str = "Assistant",
        bot_welcome: str = "Hi! How can I help you today?",
        ad_id: str = "",
        booking_window_start: str = "",
        booking_window_end: str = "",
    ) -> str:
        has_chat = "chatbot" in ad_types
        has_voice = "voicebot" in ad_types
        has_interact = has_chat or has_voice

        survey_js = """
<script>
/* ── Survey engine ─────────────────────────────────────────────────────── */
(function () {
  var steps    = Array.from(document.querySelectorAll('.survey-step'));
  var current  = 0;
  var answers  = {};
  var progress = document.getElementById('survey-progress');
  var counter  = document.getElementById('survey-step-count');
  var btnPrev  = document.getElementById('survey-prev');
  var btnNext  = document.getElementById('survey-next');
  var btnSub   = document.getElementById('survey-submit');
  var result   = document.getElementById('survey-result');

  if (!steps.length) return;

  function setProgress() {
    var pct = (current / steps.length) * 100;
    if (progress) progress.style.width = pct + '%';
    if (counter)  counter.textContent  = 'Question ' + (current + 1) + ' of ' + steps.length;
  }

  function show(idx) {
    steps.forEach(function (s, i) { s.classList.toggle('active', i === idx); });
    if (btnPrev) btnPrev.style.display = idx > 0 ? 'inline-block' : 'none';
    var last = idx === steps.length - 1;
    if (btnNext) btnNext.style.display = last ? 'none' : 'inline-block';
    if (btnSub)  btnSub.style.display  = last ? 'inline-block' : 'none';
    setProgress();
  }

  /* Answer selection */
  document.querySelectorAll('.option-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var step = btn.closest('.survey-step');
      step.querySelectorAll('.option-btn').forEach(function (b) { b.classList.remove('selected'); });
      btn.classList.add('selected');
      var idx = steps.indexOf(step);
      var eligible = (step.dataset.eligible || '').split(',').map(function (v) { return v.trim().toLowerCase(); });
      answers[idx] = { value: btn.dataset.value, ok: eligible.includes(btn.dataset.value.toLowerCase()) };
    });
  });

  /* Shake animation on no-answer-next */
  function shake(el) {
    el.style.animation = 'none';
    el.offsetHeight; /* reflow */
    el.style.animation = 'survey-shake 0.4s ease';
  }

  if (btnNext) btnNext.addEventListener('click', function () {
    if (!answers[current]) { shake(steps[current]); return; }
    current++;
    show(current);
  });

  if (btnPrev) btnPrev.addEventListener('click', function () {
    current--;
    show(current);
  });

  /* Build answer payload for registration form — stored on window so reg form JS can read them */
  window._surveyAnswers  = [];
  window._surveyEligible = false;

  if (btnSub) btnSub.addEventListener('click', function () {
    if (!answers[current]) { shake(steps[current]); return; }
    /* Score */
    var total   = steps.length;
    var okCount = Object.values(answers).filter(function (a) { return a.ok; }).length;
    var pass    = okCount >= Math.ceil(total * 0.6);
    window._surveyEligible = pass;

    /* Build answers array for submission */
    window._surveyAnswers = steps.map(function (s, i) {
      var ans = answers[i] || {};
      return {
        question_id:     s.dataset.step || String(i + 1),
        question_text:   (s.querySelector('.survey-question') || {}).textContent || '',
        selected_option: ans.value || '',
        is_eligible:     ans.ok === true ? true : ans.ok === false ? false : null
      };
    });

    /* Hide questions + nav buttons, show result (voice-row stays visible) */
    steps.forEach(function (s) { s.style.display = 'none'; });
    var navRight = document.querySelector('.survey-nav-right');
    if (navRight) navRight.style.display = 'none';
    /* .survey-voice-row stays visible inside .survey-nav */
    if (progress && progress.parentElement) progress.style.width = '100%';
    if (counter) counter.textContent = 'Complete';

    var elEl = document.getElementById('result-eligible');
    var inEl = document.getElementById('result-ineligible');
    if (elEl) elEl.style.display = pass ? 'block' : 'none';
    if (inEl) inEl.style.display = pass ? 'none'  : 'block';
    if (result) result.style.display = 'block';

    /* Show registration form after short delay */
    setTimeout(function () {
      var regSec = document.getElementById('reg-section');
      if (regSec) {
        regSec.style.display = 'block';
        var regNote = document.getElementById('reg-note');
        if (regNote) {
          regNote.style.display = 'block';
          if (pass) {
            regNote.className = 'reg-note eligible';
            regNote.textContent = 'Great news \u2014 you appear to be eligible! Please fill in your details so the study team can reach you.';
          } else {
            regNote.className = 'reg-note ineligible';
            regNote.textContent = 'Thank you for completing the survey. Please fill in your details so the study team can contact you with more information.';
          }
        }
        regSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 1200);
  });

  /* Consent checkbox — show submit button when ticked */
  var consentCheck  = document.getElementById('consent-check');
  var consentSubmit = document.getElementById('consent-submit');
  var consentThanks = document.getElementById('consent-thanks');
  if (consentCheck && consentSubmit) {
    consentCheck.addEventListener('change', function () {
      consentSubmit.style.display = this.checked ? 'block' : 'none';
    });
    consentSubmit.addEventListener('click', function () {
      consentSubmit.style.display = 'none';
      consentCheck.disabled = true;
      if (consentThanks) consentThanks.style.display = 'block';
    });
  }

  show(0);
})();
</script>
<style>
  @keyframes survey-shake {
    0%,100% { transform: translateX(0); }
    20%,60%  { transform: translateX(-8px); }
    40%,80%  { transform: translateX(8px); }
  }
</style>"""

        # Floating chat widget (Python-injected, always correct)
        chat_float_html = ""
        chat_js = ""
        if has_chat:
            chat_float_html = f"""
<div id="chat-float">
  <div class="chat-panel" id="chat-panel">
    <div class="chat-panel__header">
      <div class="chat-avatar">🤖</div>
      <div class="chat-agent-info">
        <p class="chat-agent-name">{bot_name}</p>
        <p class="chat-agent-status">Online &middot; Ready to help</p>
      </div>
      <button class="chat-close-btn" id="chat-close" aria-label="Close chat">&#10005;</button>
    </div>
    <div class="chat-messages" id="chat-messages">
      <div class="chat-bubble bot">{bot_welcome}</div>
    </div>
    <div class="chat-footer">
      <input id="chat-input" class="chat-input" type="text" placeholder="Type your message&hellip;" autocomplete="off" />
      <button class="chat-send-btn" id="chat-send" aria-label="Send">&#10148;</button>
    </div>
  </div>
  <button class="chat-toggle-btn" id="chat-toggle" aria-label="Open chat" style="position:relative;">
    💬
    <span class="chat-unread" id="chat-unread">1</span>
  </button>
</div>"""

            chat_js = f"""
<script>
/* ── Floating chat widget ──────────────────────────────────────────────── */
(function () {{
  var panel   = document.getElementById('chat-panel');
  var toggle  = document.getElementById('chat-toggle');
  var close   = document.getElementById('chat-close');
  var input   = document.getElementById('chat-input');
  var send    = document.getElementById('chat-send');
  var box     = document.getElementById('chat-messages');
  var unread  = document.getElementById('chat-unread');
  if (!panel || !toggle) return;

  var CAMPAIGN_ID  = '{ad_id}';
  var API_BASE     = window.location.origin;
  var STORAGE_KEY  = 'chat_session_' + CAMPAIGN_ID;
  var sending      = false;

  /* Session ID is scoped per campaign — persists across page refreshes */
  var sessionId = localStorage.getItem(STORAGE_KEY) || null;

  function openChat() {{
    panel.classList.add('open');
    if (unread) unread.style.display = 'none';
    if (input)  input.focus();
  }}
  function closeChat() {{ panel.classList.remove('open'); }}

  toggle.addEventListener('click', function () {{
    panel.classList.contains('open') ? closeChat() : openChat();
  }});
  if (close) close.addEventListener('click', closeChat);

  function addMsg(text, cls) {{
    var b = document.createElement('div');
    b.className = 'chat-bubble ' + cls;
    b.textContent = text;
    box.appendChild(b);
    box.scrollTop = box.scrollHeight;
    return b;
  }}

  function setInput(disabled) {{
    sending        = disabled;
    input.disabled = disabled;
    send.disabled  = disabled;
  }}

  async function sendMsg() {{
    var val = (input.value || '').trim();
    if (!val || sending) return;
    addMsg(val, 'user');
    input.value = '';
    setInput(true);

    var typing = addMsg('\u2026', 'bot');

    try {{
      var payload = {{ campaignId: CAMPAIGN_ID, message: val }};
      if (sessionId) payload.sessionId = sessionId;

      var resp = await fetch(API_BASE + '/api/chat', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      var data  = resp.ok ? await resp.json() : null;
      var reply = (data && data.reply) ? data.reply : "I\u2019m sorry, I couldn\u2019t process that. Please try again.";

      /* Persist the session id returned by the server */
      if (data && data.sessionId) {{
        sessionId = data.sessionId;
        try {{ localStorage.setItem(STORAGE_KEY, sessionId); }} catch (e) {{}}
      }}

      box.removeChild(typing);
      addMsg(reply, 'bot');
    }} catch (e) {{
      box.removeChild(typing);
      addMsg("I\u2019m sorry, something went wrong. Please try again.", 'bot');
    }}

    setInput(false);
    input.focus();
  }}

  if (send)  send.addEventListener('click', sendMsg);
  if (input) input.addEventListener('keydown', function (e) {{ if (e.key === 'Enter') sendMsg(); }});

  /* Auto-open after survey reveal */
  var observer = new MutationObserver(function (mutations) {{
    mutations.forEach(function (m) {{
      if (m.type === 'attributes' && m.attributeName === 'style') {{
        var ia = document.getElementById('interaction-reveal');
        if (ia && ia.style.display !== 'none') {{
          setTimeout(openChat, 600);
          observer.disconnect();
        }}
      }}
    }});
  }});
  var ia = document.getElementById('interaction-reveal');
  if (ia) observer.observe(ia, {{ attributes: true }});
  else setTimeout(openChat, 3000);
}})();
</script>"""

        # Registration form — injected into body before the interaction section
        reg_form_html = f"""
<section id="reg-section">
  <div class="reg-card">
    <h2 class="reg-title">Your Details</h2>
    <p class="reg-sub">Help the study team get in touch with you.</p>
    <div id="reg-note" class="reg-note ineligible" style="display:none"></div>
    <div class="reg-error" id="reg-error"></div>
    <div class="reg-done" id="reg-done">
      &#10003;&ensp;Thank you! Your details have been received.<br>
      A member of the study team will be in touch with you shortly.
      <div id="reg-book-wrap" style="display:none;margin-top:18px;">
        <p style="margin:0 0 10px;font-size:.9rem;color:#374151;">You appear to be eligible for this trial. Would you like to book your first visit?</p>
        <button id="reg-book-btn" style="background:var(--accent,#10b981);color:#fff;border:none;border-radius:50px;padding:12px 28px;font-size:.95rem;font-weight:700;cursor:pointer;font-family:inherit;">&#128197;&ensp;Book Appointment</button>
      </div>
    </div>
    <div id="reg-form-fields">
      <div class="reg-field">
        <label class="reg-label" for="reg-name">Full Name *</label>
        <input class="reg-input" id="reg-name" type="text" placeholder="e.g. Jane Smith" autocomplete="name" />
      </div>
      <div class="reg-row">
        <div class="reg-field">
          <label class="reg-label" for="reg-age">Age *</label>
          <input class="reg-input" id="reg-age" type="number" placeholder="e.g. 34" min="1" max="120" />
        </div>
        <div class="reg-field">
          <label class="reg-label" for="reg-sex">Sex *</label>
          <select class="reg-select" id="reg-sex">
            <option value="">Select&hellip;</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="other">Other</option>
            <option value="prefer_not_to_say">Prefer not to say</option>
          </select>
        </div>
      </div>
      <div class="reg-field">
        <label class="reg-label" for="reg-phone">Phone Number *</label>
        <input class="reg-input" id="reg-phone" type="tel" placeholder="e.g. +1 555 123 4567" autocomplete="tel" />
      </div>
      <button class="reg-btn" id="reg-submit">Submit Details</button>
      <p class="reg-privacy">Your information will only be used by the study team to contact you about this trial. It will not be shared with third parties.</p>
    </div>
  </div>
</section>
<script>
(function () {{
  var AD_ID    = '{ad_id}';
  var API_BASE = window.location.origin;

  var nameEl   = document.getElementById('reg-name');
  var ageEl    = document.getElementById('reg-age');
  var sexEl    = document.getElementById('reg-sex');
  var phoneEl  = document.getElementById('reg-phone');
  var submitEl = document.getElementById('reg-submit');
  var errorEl  = document.getElementById('reg-error');
  var doneEl   = document.getElementById('reg-done');
  var fieldsEl = document.getElementById('reg-form-fields');
  var noteEl   = document.getElementById('reg-note');

  /* Note content is set by survey JS before revealing #reg-section */

  function showError(msg) {{
    if (!errorEl) return;
    errorEl.textContent = msg;
    errorEl.style.display = 'block';
  }}
  function hideError() {{ if (errorEl) errorEl.style.display = 'none'; }}

  if (!submitEl) return;
  submitEl.addEventListener('click', async function () {{
    hideError();
    var name  = (nameEl  && nameEl.value.trim())  || '';
    var age   = parseInt((ageEl   && ageEl.value)   || '', 10);
    var sex   = (sexEl   && sexEl.value)   || '';
    var phone = (phoneEl && phoneEl.value.trim())  || '';

    if (!name)                     {{ showError('Please enter your full name.');   return; }}
    if (isNaN(age) || age < 1 || age > 120) {{ showError('Please enter a valid age.');    return; }}
    if (!sex)                      {{ showError('Please select your sex.');         return; }}
    if (!phone || phone.length < 5){{ showError('Please enter a valid phone number.'); return; }}

    submitEl.disabled    = true;
    submitEl.textContent = 'Submitting\u2026';

    /* Get answers + eligibility captured by survey engine */
    var answers  = window._surveyAnswers  || [];
    var eligible = window._surveyEligible || false;

    try {{
      var resp = await fetch(API_BASE + '/api/advertisements/' + AD_ID + '/survey-responses', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          full_name:   name,
          age:         age,
          sex:         sex,
          phone:       phone,
          answers:     answers,
          is_eligible: eligible
        }})
      }});
      if (!resp.ok) {{
        var err = await resp.json().catch(function () {{ return {{}}; }});
        throw new Error((err.detail) || ('Submission failed (HTTP ' + resp.status + ')'));
      }}
      /* Success */
      var regData = await resp.json().catch(function () {{ return {{}}; }});
      window._surveyResponseId = regData.id || null;
      if (fieldsEl) fieldsEl.style.display = 'none';
      if (doneEl)   doneEl.style.display   = 'block';
      if (noteEl)   noteEl.style.display   = 'none';

      /* Show book-appointment button if eligible */
      if (eligible) {{
        var bookWrap = document.getElementById('reg-book-wrap');
        if (bookWrap) bookWrap.style.display = 'block';
        /* Pre-fill appt name/phone from reg form */
        var an = document.getElementById('appt-patient-name');
        var ap = document.getElementById('appt-patient-phone');
        if (an) an.value = name;
        if (ap) ap.value = phone;
      }}

      /* Reveal interaction section after brief delay */
      setTimeout(function () {{
        var ia = document.getElementById('interaction-reveal');
        if (ia) {{
          ia.style.display = '';
          ia.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        }}
      }}, 2000);
    }} catch (e) {{
      showError(e.message || 'Something went wrong. Please try again.');
      submitEl.disabled    = false;
      submitEl.textContent = 'Submit Details';
    }}
  }});
}})();
</script>
<section id="appt-section" style="display:none;background:#f0f4f8;padding:48px 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:40px 36px;box-shadow:0 4px 24px rgba(0,0,0,0.07);">
    <div style="text-align:center;margin-bottom:24px;">
      <div style="font-size:2.2rem;margin-bottom:6px;">&#128197;</div>
      <h2 style="font-size:1.4rem;font-weight:800;color:var(--primary,#1e3a5f);margin:0 0 6px;">Book Your First Visit</h2>
      <p style="color:#64748b;font-size:.9rem;line-height:1.5;margin:0;">Select a date and an available time slot for your first trial appointment.</p>
    </div>
    <div style="margin-bottom:14px;">
      <label style="display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;margin-bottom:6px;">Full Name</label>
      <input id="appt-patient-name" type="text" placeholder="e.g. Jane Smith" style="width:100%;box-sizing:border-box;padding:10px 13px;border:1.5px solid #e2e8f0;border-radius:9px;font-size:.9rem;font-family:inherit;outline:none;" />
    </div>
    <div style="margin-bottom:18px;">
      <label style="display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;margin-bottom:6px;">Phone Number</label>
      <input id="appt-patient-phone" type="tel" placeholder="e.g. +1 555 123 4567" style="width:100%;box-sizing:border-box;padding:10px 13px;border:1.5px solid #e2e8f0;border-radius:9px;font-size:.9rem;font-family:inherit;outline:none;" />
    </div>
    <div style="margin-bottom:14px;">
      <label style="display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;margin-bottom:6px;">Select Date</label>
      <input id="appt-date" type="date" style="width:100%;box-sizing:border-box;padding:10px 13px;border:1.5px solid #e2e8f0;border-radius:9px;font-size:.9rem;font-family:inherit;outline:none;" />
    </div>
    <div style="margin-bottom:18px;">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px;">
        <label style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;">Available Slots</label>
        <span id="appt-tz" style="font-size:.65rem;color:#94a3b8;"></span>
      </div>
      <div id="appt-slots" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">
        <p style="font-size:.82rem;color:#94a3b8;grid-column:1/-1;">Select a date above to see available slots.</p>
      </div>
    </div>
    <div id="appt-status" style="display:none;padding:10px 13px;border-radius:9px;font-size:.84rem;font-weight:600;margin-bottom:14px;"></div>
    <button id="appt-confirm-btn" style="width:100%;background:var(--accent,#10b981);color:#fff;border:none;border-radius:50px;padding:13px;font-size:.95rem;font-weight:700;cursor:pointer;font-family:inherit;">Confirm Appointment &#8594;</button>
    <div id="appt-done" style="display:none;text-align:center;padding:20px;background:rgba(16,185,129,.08);border:2px solid var(--accent,#10b981);border-radius:13px;margin-top:16px;">
      <div style="font-size:1.8rem;margin-bottom:6px;">&#9989;</div>
      <p id="appt-done-msg" style="font-size:1rem;font-weight:800;color:#065f46;margin:0 0 4px;">Appointment confirmed!</p>
      <p style="font-size:.83rem;color:#64748b;margin:0;">The study team will send you a confirmation shortly.</p>
    </div>
  </div>
</section>
<style>
#appt-slots button{{padding:9px 4px;text-align:center;border:1.5px solid #e2e8f0;border-radius:8px;cursor:pointer;font-size:.78rem;font-weight:600;color:#374151;background:#fff;transition:all .15s;font-family:inherit;}}
#appt-slots button:hover:not(:disabled){{border-color:var(--primary,#1e3a5f);color:var(--primary,#1e3a5f);background:#f8fafc;}}
#appt-slots button.appt-sel{{background:var(--primary,#1e3a5f);color:#fff;border-color:var(--primary,#1e3a5f);}}
#appt-slots button:disabled{{opacity:.28;cursor:not-allowed;}}
</style>
<script>
(function () {{
  var AD_ID  = '{ad_id}';
  var API    = window.location.origin;
  var dateEl = document.getElementById('appt-date');
  var slots  = document.getElementById('appt-slots');
  var tzEl   = document.getElementById('appt-tz');
  var stEl   = document.getElementById('appt-status');
  var cBtn   = document.getElementById('appt-confirm-btn');
  var doneEl = document.getElementById('appt-done');
  var msgEl  = document.getElementById('appt-done-msg');
  var nameEl = document.getElementById('appt-patient-name');
  var phEl   = document.getElementById('appt-patient-phone');
  var bookBtn= document.getElementById('reg-book-btn');
  var apptSec= document.getElementById('appt-section');
  if (!dateEl || !cBtn) return;

  /* Restrict date to the campaign booking window */
  var WIN_START = '{booking_window_start}';
  var WIN_END   = '{booking_window_end}';
  var now = new Date();
  var todayStr = now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
  dateEl.min = WIN_START && WIN_START > todayStr ? WIN_START : todayStr;
  if (WIN_END) dateEl.max = WIN_END;
  try {{ tzEl.textContent = '('+Intl.DateTimeFormat().resolvedOptions().timeZone+')'; }} catch(e) {{}}

  var _cache = {{}}, _selTime = '';

  function esc(s) {{ return String(s).replace(/[&<>"']/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}

  function showSt(msg, tp) {{
    stEl.innerHTML = msg;
    var bg = tp === 'error' ? '#fef2f2;color:#b91c1c;border:1.5px solid #fecaca'
           : tp === 'ok'    ? '#f0fdf4;color:#15803d;border:1.5px solid #bbf7d0'
           :                  '#eff6ff;color:#1d4ed8;border:1.5px solid #bfdbfe';
    stEl.style.cssText = 'display:block;padding:10px 13px;border-radius:9px;font-size:.84rem;font-weight:600;margin-bottom:14px;background:' + bg;
  }}
  function hideSt() {{ stEl.style.display = 'none'; }}

  function renderSlots(data) {{
    _selTime = '';
    slots.innerHTML = '';
    var any = false;
    (data || []).forEach(function(s) {{
      var b = document.createElement('button');
      b.textContent = s.label;
      if (!s.available) {{
        b.disabled = true;
      }} else {{
        any = true;
        b.addEventListener('click', function() {{
          _selTime = s.time;
          slots.querySelectorAll('button').forEach(function(x) {{ x.classList.remove('appt-sel'); }});
          b.classList.add('appt-sel');
          hideSt();
        }});
      }}
      slots.appendChild(b);
    }});
    if (!any) slots.innerHTML = '<p style="font-size:.82rem;color:#94a3b8;grid-column:1/-1;">No slots available &mdash; try another date.</p>';
  }}

  function loadSlots(d) {{
    if (_cache[d]) {{ renderSlots(_cache[d]); return; }}
    slots.innerHTML = '<p style="font-size:.82rem;color:#94a3b8;grid-column:1/-1;">Loading slots&hellip;</p>';
    fetch(API+'/api/advertisements/'+AD_ID+'/appointments/slots?date='+d)
      .then(function(r) {{ return r.ok ? r.json() : Promise.reject(r.status); }})
      .then(function(data) {{ _cache[d] = data.slots || []; renderSlots(_cache[d]); }})
      .catch(function() {{ slots.innerHTML = '<p style="font-size:.82rem;color:#ef4444;grid-column:1/-1;">Could not load slots. Please try again.</p>'; }});
  }}

  dateEl.addEventListener('change', function() {{ if (dateEl.value) loadSlots(dateEl.value); }});

  /* Book Appointment button opens the section */
  if (bookBtn && apptSec) {{
    bookBtn.addEventListener('click', function() {{
      apptSec.style.display = '';
      apptSec.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
  }}

  cBtn.addEventListener('click', function() {{
    var nm = nameEl ? nameEl.value.trim() : '';
    var ph = phEl   ? phEl.value.trim()   : '';
    if (!nm) {{ showSt('Please enter your name.', 'error'); return; }}
    if (!ph) {{ showSt('Please enter your phone number.', 'error'); return; }}
    if (!dateEl.value) {{ showSt('Please select a date.', 'error'); return; }}
    if (!_selTime)     {{ showSt('Please select a time slot.', 'error'); return; }}

    var iso   = dateEl.value + 'T' + _selTime + ':00';
    var srId  = window._surveyResponseId || null;
    cBtn.disabled = true;
    cBtn.textContent = 'Booking…';
    showSt('Reserving your slot…', 'info');

    fetch(API+'/api/advertisements/'+AD_ID+'/appointments', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{patient_name:nm, patient_phone:ph, slot_datetime:iso, survey_response_id:srId}})
    }}).then(function(r) {{
      if (r.status === 409) {{ throw new Error('That slot was just taken — please pick another.'); }}
      if (!r.ok) return r.json().catch(function() {{ return {{}}; }}).then(function(j) {{ throw new Error(j.detail || 'Booking failed'); }});
      return r.json();
    }}).then(function() {{
      var lbl = _selTime;
      if (_cache[dateEl.value]) {{
        var match = _cache[dateEl.value].find(function(s) {{ return s.time === _selTime; }});
        if (match) lbl = match.label;
      }}
      hideSt();
      cBtn.style.display = 'none';
      if (msgEl) msgEl.textContent = '✅ Confirmed for ' + dateEl.value + ' at ' + lbl;
      if (doneEl) doneEl.style.display = 'block';
      delete _cache[dateEl.value];
    }}).catch(function(e) {{
      showSt('&#9888; ' + esc(e.message), 'error');
      cBtn.disabled = false;
      cBtn.textContent = 'Confirm Appointment →';
      if (dateEl.value) loadSlots(dateEl.value);
    }});
  }});
}})();
</script>"""

        # Hide interaction section initially (JS reveals it post-survey)
        if has_interact:
            body = body.replace(
                'id="interaction-reveal"',
                'id="interaction-reveal" style="display:none"',
                1,
            )

        # Inject registration form at the right position:
        # before the interaction section → before the footer → or appended before </body>
        _inject_marker = 'id="interaction-reveal"'
        _footer_marker = "<footer"
        if _inject_marker in body:
            _pos = body.index(_inject_marker)
            _open = body.rfind("<", 0, _pos)
            body = body[:_open] + reg_form_html + "\n" + body[_open:]
        elif _footer_marker in body:
            _pos = body.rfind(_footer_marker)
            body = body[:_pos] + reg_form_html + "\n" + body[_pos:]
        else:
            body = body + "\n" + reg_form_html

        # ── Survey voice row — rich modal with country picker + scheduling ────────
        # Always injected (not gated on voicebot). Calls /voice-call/request.
        survey_voice_js = f"""
<style>
/* ── CTA Agent Modal ───────────────────────────────────────────────────────── */
#cta-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);}}
#cta-overlay.cta-open{{display:flex;animation:ctaFdIn .2s ease;}}
@keyframes ctaFdIn{{from{{opacity:0}}to{{opacity:1}}}}
#cta-box{{background:#fff;border-radius:22px;max-width:460px;width:100%;max-height:92vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 60px rgba(0,0,0,.28);animation:ctaUp .3s cubic-bezier(.34,1.56,.64,1);}}
@keyframes ctaUp{{from{{transform:translateY(44px);opacity:0}}to{{transform:translateY(0);opacity:1}}}}
.cta-hdr{{display:flex;align-items:center;justify-content:space-between;padding:22px 24px 0;flex-shrink:0;}}
.cta-hdr-title{{font-size:.97rem;font-weight:800;color:#111;}}
.cta-x{{background:none;border:none;cursor:pointer;width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:18px;line-height:1;transition:background .15s,color .15s;}}
.cta-x:hover{{background:#f1f5f9;color:#374151;}}
.cta-phase{{display:none;padding:20px 24px 26px;overflow-y:auto;}}
.cta-phase.cta-active{{display:block;animation:ctaPhIn .22s ease;}}
@keyframes ctaPhIn{{from{{opacity:0;transform:translateX(18px)}}to{{opacity:1;transform:translateX(0)}}}}
.cta-back{{display:inline-flex;align-items:center;gap:5px;background:none;border:none;cursor:pointer;font-size:.78rem;font-weight:600;color:var(--primary);padding:0;font-family:inherit;margin-bottom:14px;}}
.cta-back:hover{{opacity:.7;}}
/* Country dropdown */
.cta-dd{{position:relative;margin-bottom:12px;}}
.cta-dd-btn{{display:flex;align-items:center;gap:8px;width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:10px 12px;cursor:pointer;background:#fff;font-size:.9rem;font-family:inherit;box-sizing:border-box;transition:border-color .15s;text-align:left;}}
.cta-dd-btn:hover,.cta-dd-btn:focus{{border-color:var(--primary);outline:none;}}
.cta-chevron{{margin-left:auto;color:#94a3b8;font-size:10px;transition:transform .2s;flex-shrink:0;}}
.cta-dd-btn.open .cta-chevron{{transform:rotate(180deg);}}
.cta-dd-panel{{display:none;position:absolute;top:calc(100% + 4px);left:0;right:0;background:#fff;border:1.5px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.13);z-index:60;overflow:hidden;}}
.cta-dd-panel.open{{display:block;animation:ctaDd .15s ease;}}
@keyframes ctaDd{{from{{opacity:0;transform:translateY(-5px)}}to{{opacity:1;transform:translateY(0)}}}}
.cta-dd-search{{width:100%;border:none;border-bottom:1px solid #f1f5f9;padding:10px 12px;font-size:.85rem;font-family:inherit;outline:none;box-sizing:border-box;background:#fafafa;}}
.cta-dd-list{{max-height:210px;overflow-y:auto;}}
.cta-dd-item{{display:flex;align-items:center;gap:10px;padding:9px 12px;cursor:pointer;font-size:.84rem;transition:background .1s;}}
.cta-dd-item:hover{{background:#f8fafc;}}
.cta-dd-item.cta-sel{{background:#f0fdf4;font-weight:600;}}
.cta-dd-dial{{margin-left:auto;font-size:.75rem;color:#94a3b8;flex-shrink:0;}}
/* Phone row */
.cta-phone-row{{display:flex;gap:8px;margin-bottom:6px;}}
.cta-dial{{display:flex;align-items:center;justify-content:center;gap:4px;padding:10px 10px;border:1.5px solid #e2e8f0;border-radius:10px;font-size:.87rem;font-weight:700;color:#374151;white-space:nowrap;background:#f8fafc;}}
.cta-phone-inp{{flex:1;border:1.5px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:.9rem;font-family:inherit;outline:none;transition:border-color .15s;box-sizing:border-box;min-width:0;}}
.cta-phone-inp:focus{{border-color:var(--primary);}}
.cta-phone-inp.cta-err{{border-color:#ef4444;}}
/* Status bar */
.cta-status{{display:none;padding:10px 13px;border-radius:10px;font-size:.82rem;font-weight:600;line-height:1.45;margin-top:10px;}}
.cta-info{{background:#eff6ff;color:#1d4ed8;border:1.5px solid #bfdbfe;}}
.cta-ok{{background:#f0fdf4;color:#15803d;border:1.5px solid #bbf7d0;}}
.cta-err-bar{{background:#fef2f2;color:#b91c1c;border:1.5px solid #fecaca;}}
/* Action buttons */
.cta-btn-primary{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;background:var(--accent);color:#fff;border:none;border-radius:12px;padding:13px;font-size:.95rem;font-weight:700;cursor:pointer;font-family:inherit;transition:filter .2s,transform .1s;margin-bottom:10px;}}
.cta-btn-primary:hover{{filter:brightness(1.07);transform:translateY(-1px);}}
.cta-btn-primary:active{{transform:translateY(0);}}
.cta-btn-primary:disabled{{opacity:.6;cursor:not-allowed;transform:none;filter:none;}}
.cta-btn-outline{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;background:#fff;color:var(--primary);border:1.5px solid var(--primary);border-radius:12px;padding:12px;font-size:.9rem;font-weight:600;cursor:pointer;font-family:inherit;transition:background .2s,color .2s;}}
.cta-btn-outline:hover{{background:var(--primary);color:#fff;}}
.cta-btn-outline:disabled{{opacity:.6;cursor:not-allowed;}}
.cta-btn-confirm{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;background:var(--primary);color:#fff;border:none;border-radius:12px;padding:13px;font-size:.95rem;font-weight:700;cursor:pointer;font-family:inherit;transition:filter .2s,transform .1s;}}
.cta-btn-confirm:hover{{filter:brightness(1.08);transform:translateY(-1px);}}
.cta-btn-confirm:active{{transform:translateY(0);}}
.cta-btn-confirm:disabled{{opacity:.6;cursor:not-allowed;transform:none;filter:none;}}
/* Divider */
.cta-div{{display:flex;align-items:center;gap:10px;margin:12px 0;color:#94a3b8;font-size:.75rem;}}
.cta-div::before,.cta-div::after{{content:'';flex:1;height:1px;background:#e2e8f0;}}
/* Label */
.cta-lbl{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;display:block;margin-bottom:6px;}}
/* Date input */
.cta-date{{width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:.9rem;font-family:inherit;outline:none;box-sizing:border-box;transition:border-color .15s;margin-bottom:16px;}}
.cta-date:focus{{border-color:var(--primary);}}
.cta-date.cta-err{{border-color:#ef4444;}}
/* Time slots */
.cta-slots{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:16px;}}
.cta-slot{{padding:9px 4px;text-align:center;border:1.5px solid #e2e8f0;border-radius:9px;cursor:pointer;font-size:.77rem;font-weight:600;color:#374151;transition:all .15s;background:#fff;}}
.cta-slot:hover:not(:disabled){{border-color:var(--primary);color:var(--primary);background:#f8fafc;}}
.cta-slot.cta-slot-sel{{background:var(--primary);color:#fff;border-color:var(--primary);}}
.cta-slot:disabled{{opacity:.28;cursor:not-allowed;}}
/* Success ring */
.cta-ring{{width:70px;height:70px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 18px;font-size:32px;}}
.cta-ring-green{{background:linear-gradient(135deg,#10b981,#059669);}}
.cta-ring-blue{{background:linear-gradient(135deg,#3b82f6,#1d4ed8);}}
/* Spinner */
.cta-spin{{display:inline-block;width:15px;height:15px;border:2.5px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:ctaSp .7s linear infinite;}}
@keyframes ctaSp{{to{{transform:rotate(360deg)}}}}
/* Mobile */
@media(max-width:500px){{
  #cta-overlay{{align-items:flex-end;padding:0;}}
  #cta-box{{max-height:94vh;border-radius:20px 20px 0 0;}}
  .cta-slots{{grid-template-columns:repeat(3,1fr);}}
}}
</style>
<script>
/* ── CTA Agent Modal — country picker + call now + schedule ─────────────── */
(function () {{

  var AD_ID    = '{ad_id}';
  var API_BASE = window.location.origin;

  /* ── Country list ──────────────────────────────────────────────────────── */
  var CTRY = [
    {{d:'+1',  f:'\ud83c\uddfa\ud83c\uddf8',n:'United States'}},
    {{d:'+44', f:'\ud83c\uddec\ud83c\udde7',n:'United Kingdom'}},
    {{d:'+1',  f:'\ud83c\udde8\ud83c\udde6',n:'Canada'}},
    {{d:'+61', f:'\ud83c\udde6\ud83c\uddfa',n:'Australia'}},
    {{d:'+91', f:'\ud83c\uddee\ud83c\uddf3',n:'India'}},
    {{d:'+49', f:'\ud83c\udde9\ud83c\uddea',n:'Germany'}},
    {{d:'+33', f:'\ud83c\uddeb\ud83c\uddf7',n:'France'}},
    {{d:'+34', f:'\ud83c\uddea\ud83c\uddf8',n:'Spain'}},
    {{d:'+39', f:'\ud83c\uddee\ud83c\uddf9',n:'Italy'}},
    {{d:'+55', f:'\ud83c\udde7\ud83c\uddf7',n:'Brazil'}},
    {{d:'+52', f:'\ud83c\uddf2\ud83c\uddfd',n:'Mexico'}},
    {{d:'+81', f:'\ud83c\uddef\ud83c\uddf5',n:'Japan'}},
    {{d:'+82', f:'\ud83c\uddf0\ud83c\uddf7',n:'South Korea'}},
    {{d:'+86', f:'\ud83c\udde8\ud83c\uddf3',n:'China'}},
    {{d:'+31', f:'\ud83c\uddf3\ud83c\uddf1',n:'Netherlands'}},
    {{d:'+46', f:'\ud83c\uddf8\ud83c\uddea',n:'Sweden'}},
    {{d:'+47', f:'\ud83c\uddf3\ud83c\uddf4',n:'Norway'}},
    {{d:'+45', f:'\ud83c\udde9\ud83c\uddf0',n:'Denmark'}},
    {{d:'+358',f:'\ud83c\uddeb\ud83c\uddee',n:'Finland'}},
    {{d:'+41', f:'\ud83c\udde8\ud83c\udded',n:'Switzerland'}},
    {{d:'+43', f:'\ud83c\udde6\ud83c\uddf9',n:'Austria'}},
    {{d:'+32', f:'\ud83c\udde7\ud83c\uddea',n:'Belgium'}},
    {{d:'+48', f:'\ud83c\uddf5\ud83c\uddf1',n:'Poland'}},
    {{d:'+351',f:'\ud83c\uddf5\ud83c\uddf9',n:'Portugal'}},
    {{d:'+30', f:'\ud83c\uddec\ud83c\uddf7',n:'Greece'}},
    {{d:'+90', f:'\ud83c\uddf9\ud83c\uddf7',n:'Turkey'}},
    {{d:'+966',f:'\ud83c\uddf8\ud83c\udde6',n:'Saudi Arabia'}},
    {{d:'+971',f:'\ud83c\udde6\ud83c\uddea',n:'UAE'}},
    {{d:'+65', f:'\ud83c\uddf8\ud83c\uddec',n:'Singapore'}},
    {{d:'+60', f:'\ud83c\uddf2\ud83c\uddfe',n:'Malaysia'}},
    {{d:'+63', f:'\ud83c\uddf5\ud83c\udded',n:'Philippines'}},
    {{d:'+62', f:'\ud83c\uddee\ud83c\udde9',n:'Indonesia'}},
    {{d:'+66', f:'\ud83c\uddf9\ud83c\udded',n:'Thailand'}},
    {{d:'+84', f:'\ud83c\uddfb\ud83c\uddf3',n:'Vietnam'}},
    {{d:'+64', f:'\ud83c\uddf3\ud83c\uddff',n:'New Zealand'}},
    {{d:'+54', f:'\ud83c\udde6\ud83c\uddf7',n:'Argentina'}},
    {{d:'+57', f:'\ud83c\udde8\ud83c\uddf4',n:'Colombia'}},
    {{d:'+92', f:'\ud83c\uddf5\ud83c\uddf0',n:'Pakistan'}},
    {{d:'+20', f:'\ud83c\uddea\ud83c\uddec',n:'Egypt'}},
    {{d:'+27', f:'\ud83c\uddff\ud83c\udde6',n:'South Africa'}},
    {{d:'+234',f:'\ud83c\uddf3\ud83c\uddec',n:'Nigeria'}},
    {{d:'+254',f:'\ud83c\uddf0\ud83c\uddea',n:'Kenya'}},
    {{d:'+353',f:'\ud83c\uddee\ud83c\uddea',n:'Ireland'}},
    {{d:'+852',f:'\ud83c\udded\ud83c\uddf0',n:'Hong Kong'}},
    {{d:'+972',f:'\ud83c\uddee\ud83c\uddf1',n:'Israel'}},
    {{d:'+7',  f:'\ud83c\uddf7\ud83c\uddfa',n:'Russia'}},
    {{d:'+420',f:'\ud83c\udde8\ud83c\uddff',n:'Czech Republic'}},
    {{d:'+36', f:'\ud83c\udded\ud83c\uddfa',n:'Hungary'}},
    {{d:'+40', f:'\ud83c\uddf7\ud83c\uddf4',n:'Romania'}},
    {{d:'+380',f:'\ud83c\uddfa\ud83c\udde6',n:'Ukraine'}},
  ];

  /* ── State ──────────────────────────────────────────────────────────────── */
  var ctry    = CTRY[0];
  var selSlot = '';
  var ddOpen  = false;

  /* ── Build modal HTML ───────────────────────────────────────────────────── */
  var _w = document.createElement('div');
  _w.innerHTML = (
    '<div id="cta-overlay" role="dialog" aria-modal="true" aria-label="Connect to an Agent">' +
      '<div id="cta-box">' +
        '<div class="cta-hdr">' +
          '<div style="display:flex;align-items:center;gap:9px">' +
            '<span style="font-size:1.4rem">&#128222;</span>' +
            '<span class="cta-hdr-title">Connect to an Agent</span>' +
          '</div>' +
          '<button class="cta-x" id="cta-x" aria-label="Close">&#10005;</button>' +
        '</div>' +
        /* ── Phase 1: phone ── */
        '<div class="cta-phase cta-active" id="cta-ph1">' +
          '<p style="font-size:.83rem;color:#64748b;margin:0 0 18px;line-height:1.55">Enter your number and choose how you\\'d like us to connect.</p>' +
          '<span class="cta-lbl">Country</span>' +
          '<div class="cta-dd">' +
            '<button class="cta-dd-btn" id="cta-ddb" type="button" aria-haspopup="listbox" aria-expanded="false">' +
              '<span id="cta-df" style="font-size:1.15rem"></span>' +
              '<span id="cta-dn" style="flex:1;text-align:left"></span>' +
              '<span class="cta-chevron">&#9660;</span>' +
            '</button>' +
            '<div class="cta-dd-panel" id="cta-ddp" role="listbox">' +
              '<input class="cta-dd-search" id="cta-dds" type="text" placeholder="\ud83d\udd0d Search country\u2026" aria-label="Search country" />' +
              '<div class="cta-dd-list" id="cta-ddl"></div>' +
            '</div>' +
          '</div>' +
          '<span class="cta-lbl">Phone Number</span>' +
          '<div class="cta-phone-row">' +
            '<div class="cta-dial" id="cta-badge"></div>' +
            '<input class="cta-phone-inp" id="cta-phone" type="tel" placeholder="555 000 0000" autocomplete="tel-national" aria-label="Phone number" />' +
          '</div>' +
          '<div class="cta-status" id="cta-s1"></div>' +
          '<div style="height:16px"></div>' +
          '<button class="cta-btn-primary" id="cta-now">&#128222;&ensp;Get a Call Now</button>' +
          '<div class="cta-div">or</div>' +
          '<button class="cta-btn-outline" id="cta-sched">&#128197;&ensp;Schedule a Call</button>' +
        '</div>' +
        /* ── Phase 2: schedule ── */
        '<div class="cta-phase" id="cta-ph2">' +
          '<button class="cta-back" id="cta-back">&#8592; Back</button>' +
          '<p style="font-size:.88rem;font-weight:700;color:#111;margin:0 0 16px">Choose a date &amp; time</p>' +
          '<span class="cta-lbl">Date</span>' +
          '<input class="cta-date" id="cta-date" type="date" aria-label="Preferred call date" />' +
          '<div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">' +
            '<span class="cta-lbl" style="margin:0">Available Times</span>' +
            '<span id="cta-tz" style="font-size:.66rem;color:#94a3b8"></span>' +
          '</div>' +
          '<div class="cta-slots" id="cta-slots"></div>' +
          '<div class="cta-status" id="cta-s2"></div>' +
          '<button class="cta-btn-confirm" id="cta-confirm">Confirm Schedule &#8594;</button>' +
        '</div>' +
        /* ── Phase 3: success call ── */
        '<div class="cta-phase" id="cta-ph3" style="text-align:center;padding:38px 24px">' +
          '<div class="cta-ring cta-ring-green">&#9989;</div>' +
          '<p style="font-size:1.08rem;font-weight:800;color:#111;margin:0 0 8px">Calling you now!</p>' +
          '<p style="font-size:.85rem;color:#64748b;line-height:1.6;margin:0 0 6px">Your phone will ring in a few seconds. Pick up to speak with our agent.</p>' +
          '<p id="cta-ph3-hint" style="font-size:.75rem;color:#94a3b8;line-height:1.5;margin:0 0 22px;display:none">Didn’t get a call? The number you entered was <strong id="cta-ph3-num"></strong>. Check it’s correct and try again.</p>' +
          '<button class="cta-btn-outline" id="cta-ok1" style="max-width:180px;margin:0 auto">Done</button>' +
        '</div>' +
        /* ── Phase 4: success schedule ── */
        '<div class="cta-phase" id="cta-ph4" style="text-align:center;padding:38px 24px">' +
          '<div class="cta-ring cta-ring-blue">&#128197;</div>' +
          '<p style="font-size:1.08rem;font-weight:800;color:#111;margin:0 0 8px">Call Scheduled!</p>' +
          '<p id="cta-conf" style="font-size:.85rem;color:#64748b;line-height:1.6;margin:0 0 22px"></p>' +
          '<button class="cta-btn-outline" id="cta-ok2" style="max-width:180px;margin:0 auto">Done</button>' +
        '</div>' +
      '</div>' +
    '</div>'
  );
  document.body.appendChild(_w);

  /* ── DOM refs ────────────────────────────────────────────────────────────── */
  var overlay  = document.getElementById('cta-overlay');
  var xBtn     = document.getElementById('cta-x');
  var ddb      = document.getElementById('cta-ddb');
  var ddp      = document.getElementById('cta-ddp');
  var dds      = document.getElementById('cta-dds');
  var ddl      = document.getElementById('cta-ddl');
  var dfEl     = document.getElementById('cta-df');
  var dnEl     = document.getElementById('cta-dn');
  var badge    = document.getElementById('cta-badge');
  var phoneInp = document.getElementById('cta-phone');
  var s1       = document.getElementById('cta-s1');
  var nowBtn   = document.getElementById('cta-now');
  var schedBtn = document.getElementById('cta-sched');
  var backBtn  = document.getElementById('cta-back');
  var dateInp  = document.getElementById('cta-date');
  var slotsEl  = document.getElementById('cta-slots');
  var tzEl     = document.getElementById('cta-tz');
  var s2       = document.getElementById('cta-s2');
  var confirmB = document.getElementById('cta-confirm');
  var ok1      = document.getElementById('cta-ok1');
  var ok2      = document.getElementById('cta-ok2');
  var confMsg  = document.getElementById('cta-conf');
  var ph3Hint  = document.getElementById('cta-ph3-hint');
  var ph3Num   = document.getElementById('cta-ph3-num');
  var _callHintTimer = null;

  /* ── Phase helpers ──────────────────────────────────────────────────────── */
  var PH = ['cta-ph1','cta-ph2','cta-ph3','cta-ph4'];
  function showPh(id) {{
    PH.forEach(function(p) {{ document.getElementById(p).classList.remove('cta-active'); }});
    document.getElementById(id).classList.add('cta-active');
  }}

  /* ── Open / close ───────────────────────────────────────────────────────── */
  function openModal() {{
    overlay.classList.add('cta-open');
    document.body.style.overflow = 'hidden';
    showPh('cta-ph1');
    reset1();
    setTimeout(function() {{ phoneInp.focus(); }}, 340);
  }}
  function closeModal() {{
    overlay.classList.remove('cta-open');
    document.body.style.overflow = '';
    closeDd();
    if (_callHintTimer) {{ clearTimeout(_callHintTimer); _callHintTimer = null; }}
  }}
  xBtn.addEventListener('click', closeModal);
  ok1.addEventListener('click', closeModal);
  ok2.addEventListener('click', closeModal);
  overlay.addEventListener('click', function(e) {{ if (e.target === overlay) closeModal(); }});
  document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape' && overlay.classList.contains('cta-open')) closeModal(); }});

  /* ── Country dropdown ───────────────────────────────────────────────────── */
  function renderDd(q) {{
    q = (q || '').toLowerCase();
    ddl.innerHTML = '';
    CTRY.forEach(function(c) {{
      if (q && c.n.toLowerCase().indexOf(q) === -1 && c.d.indexOf(q) === -1) return;
      var it = document.createElement('div');
      it.className = 'cta-dd-item' + (c === ctry ? ' cta-sel' : '');
      it.setAttribute('role','option');
      it.setAttribute('aria-selected', String(c === ctry));
      it.innerHTML = '<span style="font-size:1.15rem;line-height:1">' + c.f + '</span>' +
                     '<span>' + c.n + '</span>' +
                     '<span class="cta-dd-dial">' + c.d + '</span>';
      it.addEventListener('click', function() {{ selectCtry(c); }});
      ddl.appendChild(it);
    }});
    if (!ddl.children.length) ddl.innerHTML = '<div style="padding:10px 12px;font-size:.82rem;color:#94a3b8">No results</div>';
  }}
  function selectCtry(c) {{
    ctry = c;
    dfEl.textContent  = c.f;
    dnEl.textContent  = c.n;
    badge.textContent = c.f + '\u00a0' + c.d;
    closeDd();
    renderDd('');
  }}
  function openDd() {{
    ddOpen = true;
    ddp.classList.add('open');
    ddb.classList.add('open');
    ddb.setAttribute('aria-expanded','true');
    dds.value = '';
    renderDd('');
    setTimeout(function() {{ dds.focus(); }}, 40);
  }}
  function closeDd() {{
    ddOpen = false;
    ddp.classList.remove('open');
    ddb.classList.remove('open');
    ddb.setAttribute('aria-expanded','false');
  }}
  ddb.addEventListener('click', function(e) {{ e.stopPropagation(); ddOpen ? closeDd() : openDd(); }});
  dds.addEventListener('input', function() {{ renderDd(dds.value); }});
  document.addEventListener('click', function(e) {{
    if (ddOpen && !ddp.contains(e.target) && e.target !== ddb) closeDd();
  }});
  selectCtry(CTRY[0]);  /* init */

  /* ── Phone helpers ──────────────────────────────────────────────────────── */
  function digits() {{ return (phoneInp.value || '').replace(/\\D/g,''); }}
  function fullNum() {{ return ctry.d + digits(); }}
  function validPhone() {{
    if (digits().length < 5) {{ phoneInp.classList.add('cta-err'); return false; }}
    phoneInp.classList.remove('cta-err');
    return true;
  }}
  phoneInp.addEventListener('input', function() {{ phoneInp.classList.remove('cta-err'); hideSt(s1); }});
  phoneInp.addEventListener('keydown', function(e) {{ if (e.key === 'Enter') {{ e.preventDefault(); nowBtn.click(); }} }});

  /* ── Status helpers ─────────────────────────────────────────────────────── */
  function esc(s) {{ return String(s).replace(/[&<>"']/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}
  function showSt(el, msg, t) {{
    el.className = 'cta-status ' + (t === 'error' ? 'cta-err-bar' : t === 'ok' ? 'cta-ok' : 'cta-info');
    el.innerHTML = msg;
    el.style.display = 'block';
  }}
  function hideSt(el) {{ el.style.display = 'none'; }}

  /* ── API ────────────────────────────────────────────────────────────────── */
  function apiCall(phone, scheduledFor) {{
    var payload = scheduledFor ? {{phone_number:phone,scheduled_for:scheduledFor}} : {{phone_number:phone}};
    return fetch(API_BASE + '/api/advertisements/' + AD_ID + '/voice-call/request', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(payload)
    }}).then(function(r) {{
      if (!r.ok) return r.json().catch(function(){{return{{}}}}).then(function(j){{
        throw new Error(j.detail || 'Request failed (HTTP ' + r.status + ')');
      }});
      return r.json();
    }});
  }}

  /* ── Reset phase 1 ──────────────────────────────────────────────────────── */
  function reset1() {{
    hideSt(s1);
    nowBtn.disabled = false;
    nowBtn.innerHTML = '&#128222;&ensp;Get a Call Now';
    schedBtn.disabled = false;
    phoneInp.classList.remove('cta-err');
  }}

  /* ── Call Now ───────────────────────────────────────────────────────────── */
  nowBtn.addEventListener('click', function() {{
    if (!validPhone()) {{ showSt(s1,'Please enter a valid phone number.','error'); phoneInp.focus(); return; }}
    var dialledNumber = fullNum();
    nowBtn.disabled = true;
    nowBtn.innerHTML = '<span class="cta-spin"></span>&ensp;Connecting\u2026';
    schedBtn.disabled = true;
    showSt(s1,'Reaching our agent \u2014 your phone will ring shortly\u2026','info');
    apiCall(dialledNumber).then(function() {{
      if (ph3Hint) ph3Hint.style.display = 'none';
      if (ph3Num)  ph3Num.textContent = dialledNumber;
      showPh('cta-ph3');
      if (_callHintTimer) clearTimeout(_callHintTimer);
      _callHintTimer = setTimeout(function() {{
        if (ph3Hint) ph3Hint.style.display = 'block';
      }}, 30000);
    }}).catch(function(e) {{
      showSt(s1,'&#9888; ' + esc(e.message),'error');
      reset1();
    }});
  }});

  /* ── Schedule ───────────────────────────────────────────────────────────── */
  schedBtn.addEventListener('click', function() {{
    if (!validPhone()) {{ showSt(s1,'Please enter your phone number first.','error'); phoneInp.focus(); return; }}
    var t = new Date();
    var mm = String(t.getMonth()+1).padStart(2,'0');
    var dd = String(t.getDate()).padStart(2,'0');
    dateInp.min   = t.getFullYear() + '-' + mm + '-' + dd;
    dateInp.value = '';
    selSlot = '';
    slotsEl.innerHTML = '<p style="font-size:.8rem;color:#94a3b8;grid-column:1/-1;padding:6px 0">Select a date to see available slots.</p>';
    hideSt(s2);
    confirmB.disabled = false;
    confirmB.innerHTML = 'Confirm Schedule &#8594;';
    try {{ tzEl.textContent = '(' + Intl.DateTimeFormat().resolvedOptions().timeZone + ')'; }} catch(e) {{ tzEl.textContent=''; }}
    showPh('cta-ph2');
  }});

  backBtn.addEventListener('click', function() {{ showPh('cta-ph1'); hideSt(s1); }});

  /* ── Time slots ─────────────────────────────────────────────────────────── */
  var SLOTS = [
    '9:00 AM','9:30 AM','10:00 AM','10:30 AM','11:00 AM','11:30 AM',
    '12:00 PM','12:30 PM','1:00 PM','1:30 PM','2:00 PM','2:30 PM',
    '3:00 PM','3:30 PM','4:00 PM','4:30 PM','5:00 PM','5:30 PM'
  ];
  function slot24(s) {{
    var m = s.match(/(\\d+):(\\d+)\\s(AM|PM)/);
    var h = parseInt(m[1]), mn = parseInt(m[2]), pm = m[3]==='PM';
    if (pm && h!==12) h+=12;
    if (!pm && h===12) h=0;
    return h*60+mn;
  }}
  function renderSlots(dateStr) {{
    selSlot = '';
    slotsEl.innerHTML = '';
    var now = new Date();
    var todayStr = now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
    var isToday  = dateStr===todayStr;
    var nowMins  = now.getHours()*60+now.getMinutes();
    var anyAdded = false;
    SLOTS.forEach(function(s) {{
      var btn = document.createElement('button');
      btn.className = 'cta-slot';
      btn.textContent = s;
      if (isToday && slot24(s) <= nowMins+60) {{
        btn.disabled = true;
      }} else {{
        anyAdded = true;
        btn.addEventListener('click', function() {{
          selSlot = s;
          slotsEl.querySelectorAll('.cta-slot').forEach(function(b){{b.classList.remove('cta-slot-sel');}});
          btn.classList.add('cta-slot-sel');
          hideSt(s2);
        }});
      }}
      slotsEl.appendChild(btn);
    }});
    if (!anyAdded) slotsEl.innerHTML='<p style="font-size:.8rem;color:#94a3b8;grid-column:1/-1">No slots available for today \u2014 try tomorrow.</p>';
  }}
  dateInp.addEventListener('change', function() {{ if (dateInp.value) renderSlots(dateInp.value); }});

  /* ── Confirm schedule ───────────────────────────────────────────────────── */
  function toISO(dateStr, slot) {{
    var m=slot.match(/(\\d+):(\\d+)\\s(AM|PM)/);
    var h=parseInt(m[1]),mn=parseInt(m[2]),pm=m[3]==='PM';
    if (pm&&h!==12) h+=12;
    if (!pm&&h===12) h=0;
    return dateStr+'T'+String(h).padStart(2,'0')+':'+String(mn).padStart(2,'0');
  }}
  confirmB.addEventListener('click', function() {{
    if (!dateInp.value) {{ showSt(s2,'Please select a date.','error'); return; }}
    if (!selSlot)       {{ showSt(s2,'Please select a time slot.','error'); return; }}
    confirmB.disabled = true;
    confirmB.innerHTML = '<span class="cta-spin"></span>&ensp;Scheduling\u2026';
    showSt(s2,'Booking your call\u2026','info');
    apiCall(fullNum(), toISO(dateInp.value, selSlot)).then(function() {{
      confMsg.textContent = 'We\\'ll call ' + fullNum() + ' on ' + dateInp.value + ' at ' + selSlot + '.';
      showPh('cta-ph4');
    }}).catch(function(e) {{
      showSt(s2,'&#9888; ' + esc(e.message),'error');
      confirmB.disabled = false;
      confirmB.innerHTML = 'Confirm Schedule &#8594;';
    }});
  }});

  /* ── Open on button click ───────────────────────────────────────────────── */
  document.addEventListener('click', function(e) {{
    var btn = e.target.closest('#survey-voice-btn, .btn-voice-call');
    if (btn) openModal();
  }});

}})();
</script>"""

        # ElevenLabs voice call JS (interaction-section buttons — only when voicebot is active)
        voice_js = ""
        if has_voice and ad_id:
            voice_js = f"""
<script>
/* ── ElevenLabs Outbound Call — interaction section buttons ──────────────── */
(function () {{

  var AD_ID    = '{ad_id}';
  var API_BASE = window.location.origin;

  /* ── Utility: find a button by ID or by text fallback ─────────────────── */
  function findBtn(id, hints) {{
    var el = document.getElementById(id);
    if (el) return el;
    var all = document.querySelectorAll('#interaction-reveal button, .interaction-card__btn');
    for (var i = 0; i < all.length; i++) {{
      var t = (all[i].textContent || '').toLowerCase();
      for (var j = 0; j < hints.length; j++) if (t.indexOf(hints[j]) !== -1) {{ all[i].id = id; return all[i]; }}
    }}
    return null;
  }}

  /* ── Shared modal builder ──────────────────────────────────────────────── */
  function makeModal(cfg) {{
    /* cfg: {{ id, icon, title, subtitle, fields[], submitText }} */
    var fieldsHtml = cfg.fields.map(function (f) {{
      var input = f.type === 'tel'
        ? '<input id="' + f.id + '" type="tel" placeholder="+1 (555) 000-0000" autocomplete="tel"'
        : f.type === 'date'
        ? '<input id="' + f.id + '" type="date" min="' + todayStr() + '"'
        : '<input id="' + f.id + '" type="' + f.type + '"';
      input += ' style="width:100%;box-sizing:border-box;border:1.5px solid #e2e8f0;border-radius:10px;padding:10px 13px;font-size:0.9rem;font-family:inherit;outline:none;margin-bottom:14px" />';
      return '<label style="display:block;text-align:left;font-size:0.78rem;font-weight:700;color:#374151;margin-bottom:4px">' + f.label + '</label>' + input;
    }}).join('');

    var wrap = document.createElement('div');
    wrap.innerHTML =
      '<div id="' + cfg.id + '" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:10000;align-items:center;justify-content:center;padding:16px">' +
        '<div style="background:#fff;border-radius:20px;padding:36px 32px;max-width:420px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,0.25);text-align:center;box-sizing:border-box">' +
          '<div style="font-size:2.2rem;margin-bottom:8px">' + cfg.icon + '</div>' +
          '<h3 style="font-size:1.15rem;font-weight:800;margin:0 0 6px;color:#111">' + cfg.title + '</h3>' +
          '<p style="color:#64748b;font-size:0.875rem;margin:0 0 20px;line-height:1.5">' + cfg.subtitle + '</p>' +
          fieldsHtml +
          '<button class="m-submit" style="width:100%;background:#10b981;color:#fff;border:none;border-radius:50px;padding:13px;font-size:0.95rem;font-weight:700;cursor:pointer;font-family:inherit">' + cfg.submitText + '</button>' +
          '<div class="m-status" style="display:none;margin-top:14px;padding:11px 14px;border-radius:10px;font-size:0.85rem;font-weight:600;line-height:1.4"></div>' +
          '<button class="m-close" style="margin-top:10px;background:none;border:none;color:#94a3b8;cursor:pointer;font-size:0.82rem;font-family:inherit">Cancel</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);

    var backdrop = document.getElementById(cfg.id);
    var submit   = backdrop.querySelector('.m-submit');
    var status   = backdrop.querySelector('.m-status');
    var closeBtn = backdrop.querySelector('.m-close');

    function open()  {{ backdrop.style.display = 'flex'; }}
    function close() {{ backdrop.style.display = 'none'; status.style.display = 'none'; submit.disabled = false; submit.textContent = cfg.submitText; }}
    function esc(s) {{ return String(s).replace(/[&<>"']/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}
    function showStatus(msg, type) {{
      var styles = type === 'error'   ? 'background:#fef2f2;color:#b91c1c;border:1.5px solid #fecaca'
                 : type === 'success' ? 'background:#f0fdf4;color:#15803d;border:1.5px solid #bbf7d0'
                 :                     'background:#eff6ff;color:#1d4ed8;border:1.5px solid #bfdbfe';
      status.style.cssText = 'display:block;margin-top:14px;padding:11px 14px;border-radius:10px;font-size:0.85rem;font-weight:600;line-height:1.4;' + styles;
      status.innerHTML = msg;
    }}

    closeBtn.addEventListener('click', close);
    backdrop.addEventListener('click', function (e) {{ if (e.target === backdrop) close(); }});

    /* Collect field values + validate */
    function collect() {{
      var vals = {{}}, ok = true;
      cfg.fields.forEach(function (f) {{
        var el = document.getElementById(f.id);
        el.style.borderColor = '#e2e8f0';
        var v = (el.value || '').trim();
        if (!v) {{ el.style.borderColor = '#ef4444'; ok = false; }}
        vals[f.id] = v;
      }});
      return ok ? vals : null;
    }}

    return {{ open: open, close: close, showStatus: showStatus, collect: collect, submit: submit }};
  }}

  function todayStr() {{
    var d = new Date(), mm = String(d.getMonth()+1).padStart(2,'0'), dd = String(d.getDate()).padStart(2,'0');
    return d.getFullYear() + '-' + mm + '-' + dd;
  }}

  /* ── POST to backend ───────────────────────────────────────────────────── */
  async function requestCall(phone, scheduledFor) {{
    var body = {{ phone_number: phone }};
    if (scheduledFor) body.scheduled_for = scheduledFor;
    var resp = await fetch(API_BASE + '/api/advertisements/' + AD_ID + '/voice-call/request', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body)
    }});
    if (!resp.ok) {{
      var err = 'Call request failed (HTTP ' + resp.status + ').';
      try {{ err = (await resp.json()).detail || err; }} catch(_) {{}}
      throw new Error(err);
    }}
    return await resp.json();
  }}

  /* ══ 1. INSTANT CALL (interaction section "Call Me Now" button) ═══════════ */
  var im = makeModal({{
    id: 'instant-modal',
    icon: '&#128222;',
    title: 'Call Me Now',
    subtitle: 'Enter your phone number and our staff will call you within seconds.',
    fields: [{{ id: 'ic-phone', label: 'Your Phone Number', type: 'tel' }}],
    submitText: 'Call Me Now \u2192'
  }});

  var voiceCallBtn = findBtn('voice-call-btn', ['call me now', 'instant call', 'call now']);
  if (voiceCallBtn) voiceCallBtn.addEventListener('click', im.open);

  im.submit.addEventListener('click', async function () {{
    var vals = im.collect();
    if (!vals) return;
    im.submit.disabled = true; im.submit.textContent = 'Calling\u2026';
    im.showStatus('Connecting our staff \u2014 your phone will ring shortly\u2026', 'info');
    try {{
      await requestCall(vals['ic-phone']);
      im.showStatus('&#10003; Calling you now! Pick up when your phone rings.', 'success');
      setTimeout(function () {{ im.close(); }}, 4000);
    }} catch (e) {{
      im.showStatus(esc(e.message), 'error');
      im.submit.disabled = false; im.submit.textContent = 'Call Me Now \u2192';
    }}
  }});

  /* ══ 2. SCHEDULED CALL ═════════════════════════════════════════════════ */
  var schedBtn = findBtn('schedule-call-btn', ['schedule', 'callback', 'call back', 'request a call']);
  if (schedBtn) {{
    var sm = makeModal({{
      id: 'schedule-modal',
      icon: '&#128197;',
      title: 'Schedule a Call',
      subtitle: 'Pick a date &amp; time and enter your number &mdash; our staff will call you at your chosen slot.',
      fields: [
        {{ id: 'sc-date',  label: 'Date',             type: 'date' }},
        {{ id: 'sc-time',  label: 'Preferred Time',   type: 'time' }},
        {{ id: 'sc-phone', label: 'Your Phone Number', type: 'tel'  }}
      ],
      submitText: 'Confirm Schedule'
    }});

    schedBtn.addEventListener('click', sm.open);

    sm.submit.addEventListener('click', async function () {{
      var vals = sm.collect();
      if (!vals) return;
      var scheduledFor = vals['sc-date'] + 'T' + vals['sc-time'];
      sm.submit.disabled = true; sm.submit.textContent = 'Scheduling\u2026';
      sm.showStatus('Scheduling your call\u2026', 'info');
      try {{
        await requestCall(vals['sc-phone'], scheduledFor);
        sm.showStatus('&#10003; Scheduled! We will call ' + esc(vals['sc-phone']) + ' on ' + esc(vals['sc-date']) + ' at ' + esc(vals['sc-time']) + '.', 'success');
        schedBtn.textContent = 'Call Scheduled';
        setTimeout(function () {{ sm.close(); schedBtn.textContent = 'Schedule Another Call'; }}, 5000);
      }} catch (e) {{
        sm.showStatus(esc(e.message), 'error');
        sm.submit.disabled = false; sm.submit.textContent = 'Confirm Schedule';
      }}
    }});
  }}

}})();
</script>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
  <style>{css}
  </style>
</head>
<body>
{body}
{chat_float_html}
{survey_js}
{chat_js}
{survey_voice_js}
{voice_js}
</body>
</html>"""

    # ── Body prompt ────────────────────────────────────────────────────────────

    def _build_body_prompt(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        company_name = company.name if company else "Our Company"
        logo_url = company.logo_url if company else None
        industry = company.industry if company else ""
        adjectives = (brand_kit.adjectives if brand_kit else None) or "professional, trustworthy"
        dos = (brand_kit.dos if brand_kit else None) or ""
        donts = (brand_kit.donts if brand_kit else None) or ""

        ad_types = [t.lower() for t in (ad.ad_type or [])]
        strategy = ad.strategy_json or {}
        messaging = strategy.get("messaging", {}) or {}
        core_msg = messaging.get("core_message", ad.title) if isinstance(messaging, dict) else ad.title
        cta_text = messaging.get("cta", "Get Started") if isinstance(messaging, dict) else "Get Started"
        key_phrases = messaging.get("key_phrases", []) if isinstance(messaging, dict) else []
        exec_sum = strategy.get("executive_summary", "")
        target_aud = strategy.get("target_audience", {})
        primary_aud = target_aud.get("primary", "") if isinstance(target_aud, dict) else str(target_aud)
        kpis = strategy.get("kpis", [])

        website_reqs = ad.website_reqs or {}
        must_have = website_reqs.get("must_have", []) if isinstance(website_reqs, dict) else []
        must_avoid = website_reqs.get("must_avoid", []) if isinstance(website_reqs, dict) else []
        website_reqs.get("accessibility", []) if isinstance(website_reqs, dict) else []

        bot_config = ad.bot_config or {}
        bot_name = bot_config.get("name", "Assistant") if isinstance(bot_config, dict) else "Assistant"
        bot_config.get("welcome_message", f"Hi! I'm {bot_name}. How can I help you today?") if isinstance(
            bot_config, dict
        ) else f"Hi! I'm {bot_name}."

        logo_html = f'<img src="{logo_url}" alt="{company_name}" />' if logo_url else company_name

        # Interaction section instructions
        interaction_block = ""
        if "voicebot" in ad_types:
            interaction_block = f"""
VOICEBOT SECTION — add immediately after the survey section:
<section class="interaction-section" id="interaction-reveal">
  Title: "How would you like to speak with {bot_name}?"
  Sub: short subtitle
  Two .interaction-card divs inside .interaction-cards:
    Card 1 (featured): icon=📞, title="Call Me Now",
      desc="Enter your phone number and {bot_name} will call you within seconds.",
      btn id="voice-call-btn" text="Call Me Now →", meta="AI calls your phone · Instant · Free"
    Card 2: icon=📅, title="Schedule a Call",
      desc="Pick a date, time, and enter your phone number — we'll call you at your chosen slot.",
      btn id="schedule-call-btn" text="Schedule a Call →", meta="Phone call · You choose the time · Free"
  Add <p class="interaction-note"> with privacy/consent note.
IMPORTANT: the id="interaction-reveal" attribute is required on the <section> tag. Button IDs must be exactly: voice-call-btn and schedule-call-btn.
"""
        elif "chatbot" in ad_types:
            interaction_block = f"""
CHATBOT SECTION — the chat widget floats bottom-right (system-injected). Do NOT add chat HTML.
Place this reveal anchor immediately after the survey section:
  <section class="cta-section" id="interaction-reveal">
    <div class="cta-inner">
      <h2 class="cta-title">Chat with {bot_name} Now</h2>
      <p class="cta-sub">Click the chat button in the bottom-right corner — {bot_name} is ready to help.</p>
    </div>
  </section>
The id="interaction-reveal" is required — JS scrolls to it and opens the floating chat panel automatically.
"""

        return f"""## Campaign Details
- Company: {company_name} ({industry})
- Title: {ad.title}
- Ad types: {", ".join(ad_types)}
- Core message: {core_msg}
- CTA: {cta_text}
- Target audience: {primary_aud}
- Tone: {adjectives}
- Key phrases: {", ".join(key_phrases) if key_phrases else "N/A"}

## Strategy Summary
{exec_sum or "Craft compelling copy from the campaign title and audience."}

## Benefits / KPIs (use as chips and card content)
{json.dumps(kpis, indent=2) if kpis else "Derive 3–4 benefits from context."}

## Eligibility Criteria (use to generate survey questions)
{json.dumps(must_have, indent=2) if must_have else "Generate relevant eligibility questions from the campaign context."}

## Must Avoid
{json.dumps(must_avoid, indent=2) if must_avoid else "Nothing flagged."}

## Brand DOs / DON'Ts
DOs: {dos or "N/A"}
DON'Ts: {donts or "N/A"}

## Logo HTML
{logo_html}

{interaction_block}

---
Page order (strict):
1. <nav>
2. .hero  (headline, benefit chips, CTA button)
3. #survey-section  (4–6 eligibility questions generated from criteria above)
4. {('Voicebot/chatbot section with id="interaction-reveal"') if (interaction_block) else "Features section (.card-grid)"}
5. Features section with .card-grid (why join / what to expect)
6. .cta-section
7. <footer>

Start output with <nav>. End with </footer>. No inline styles. No custom classes.
"""

    # ── Claude call ────────────────────────────────────────────────────────────

    async def _generate_body(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        if not self.ai.is_configured():
            return self._mock_body(ad, brand_kit, company)

        prompt = self._build_body_prompt(ad, brand_kit, company)

        body = await self.ai.complete(
            system=_BODY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        return body.strip()

    # ── Mock (when no API key configured) ────────────────────────────────────

    def _mock_body(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        from html import escape as _esc

        company_name = _esc(company.name if company else "Our Company", quote=True)
        logo_url = _esc(company.logo_url, quote=True) if (company and company.logo_url) else None
        title = _esc(ad.title, quote=True)
        ad_types = [t.lower() for t in (ad.ad_type or [])]
        bot_config = ad.bot_config or {}
        bot_name = (
            _esc(bot_config.get("name", "Assistant"), quote=True) if isinstance(bot_config, dict) else "Assistant"
        )
        logo_html = f'<img src="{logo_url}" alt="{company_name}" />' if logo_url else company_name

        interaction_html = ""
        if "voicebot" in ad_types:
            interaction_html = f"""
<section class="interaction-section" id="interaction-reveal">
  <h2 class="interaction-section__title">How would you like to speak with {bot_name}?</h2>
  <p class="interaction-section__sub">Choose your preferred way to connect with our staff.</p>
  <div class="interaction-cards">
    <div class="interaction-card featured">
      <div class="interaction-card__icon">🎤</div>
      <p class="interaction-card__title">Talk to {bot_name} Now</p>
      <p class="interaction-card__desc">{bot_name} will speak with you right now through your browser — no phone needed.</p>
      <button class="interaction-card__btn" id="voice-call-btn">Call Me Now →</button>
      <p class="interaction-card__meta">AI calls your phone · Instant · Free</p>
    </div>
    <div class="interaction-card">
      <div class="interaction-card__icon">📅</div>
      <p class="interaction-card__title">Schedule a Call</p>
      <p class="interaction-card__desc">Pick a date and time that works for you, enter your phone number, and we'll call you right on schedule.</p>
      <button class="interaction-card__btn" id="schedule-call-btn">Schedule a Call →</button>
      <p class="interaction-card__meta">Phone call · You choose the time · Free</p>
    </div>
  </div>
  <p class="interaction-note">Your responses are confidential and used only to assess eligibility.</p>
</section>"""
        elif "chatbot" in ad_types:
            interaction_html = f"""
<section class="cta-section" id="interaction-reveal">
  <div class="cta-inner">
    <h2 class="cta-title">Chat with {bot_name} Now</h2>
    <p class="cta-sub">Click the chat button in the bottom-right corner &mdash; {bot_name} is ready to help you right now.</p>
  </div>
</section>"""

        return f"""<nav>
  <a href="#" class="logo">{logo_html}</a>
  <a href="#survey-section" class="nav-cta">Check Eligibility</a>
</nav>

<section class="hero">
  <div class="hero-badge">&#9650; Campaign</div>
  <h1 class="hero-headline">{title}</h1>
  <p class="hero-sub">A professionally crafted campaign powered by AI — tailored to your brand and marketing strategy.</p>
  <div class="benefit-chips">
    <span class="chip">Free &amp; Instant</span>
    <span class="chip">AI-Powered</span>
    <span class="chip">Brand Aligned</span>
  </div>
  <a href="#survey-section" class="btn btn-primary">Check Your Eligibility &rarr;</a>
</section>

<section class="survey-section" id="survey-section">
  <div class="survey-card">
    <div class="survey-header">
      <h2 class="survey-title">Check Your Eligibility</h2>
      <p class="survey-sub">Answer a few quick questions. Takes about 2 minutes.</p>
      <div class="survey-progress-bar"><div class="survey-progress-fill" id="survey-progress"></div></div>
      <p class="survey-step-count" id="survey-step-count">Question 1 of 3</p>
    </div>

    <div class="survey-step active" data-step="1" data-eligible="yes">
      <p class="survey-question">Are you 18 years of age or older?</p>
      <div class="option-grid">
        <button class="option-btn" data-value="yes">Yes</button>
        <button class="option-btn" data-value="no">No, I'm under 18</button>
      </div>
    </div>

    <div class="survey-step" data-step="2" data-eligible="yes">
      <p class="survey-question">Have you been diagnosed with a relevant condition in the past 12 months?</p>
      <div class="option-grid">
        <button class="option-btn" data-value="yes">Yes</button>
        <button class="option-btn" data-value="no">No</button>
        <button class="option-btn" data-value="unsure">I'm not sure</button>
      </div>
    </div>

    <div class="survey-step" data-step="3" data-eligible="yes,maybe">
      <p class="survey-question">Are you available for follow-up appointments over the next 6 months?</p>
      <div class="option-grid">
        <button class="option-btn" data-value="yes">Yes, fully available</button>
        <button class="option-btn" data-value="maybe">Possibly, schedule dependent</button>
        <button class="option-btn" data-value="no">No, not available</button>
      </div>
    </div>

    <div class="survey-result" id="survey-result">
      <div class="result-card eligible" id="result-eligible">
        <div class="result-icon"></div>
        <h3>You may be eligible!</h3>
        <p>Based on your responses, you appear to meet the initial criteria. Connect with us below — our team will be in touch shortly.</p>
      </div>
      <div class="result-card ineligible" id="result-ineligible">
        <div class="result-icon"></div>
        <h3>Not quite a match right now</h3>
        <p>You may not meet all the current criteria, but other opportunities may be coming soon.</p>
        <div class="consent-box">
          <label class="consent-label">
            <input type="checkbox" class="consent-checkbox" id="consent-check" />
            You don't meet the criteria for this trial, but you might qualify for our upcoming trials. May we retain your details to notify you?
          </label>
          <button class="consent-submit" id="consent-submit">Yes, Keep Me Updated</button>
          <p class="consent-thanks" id="consent-thanks">&#10003; Thank you! We'll be in touch when a matching trial opens.</p>
        </div>
      </div>
    </div>

    <div class="survey-nav">
      <div class="survey-voice-row">
        <button class="btn-voice-call" id="survey-voice-btn">&#128222; Speak to Us</button>
      </div>
      <div class="survey-nav-right">
        <button class="btn-survey-prev" id="survey-prev">&#8592; Back</button>
        <button class="btn-survey-next" id="survey-next">Next &rarr;</button>
        <button class="btn-survey-submit" id="survey-submit">See My Result &rarr;</button>
      </div>
    </div>
  </div>
</section>

{interaction_html}

<div style="background:#fff;">
  <div class="section">
    <h2 class="section-title">Why Participate</h2>
    <p class="section-sub">Here's what you can expect when you join.</p>
    <div class="card-grid">
      <div class="card"><div class="card-icon">&#128200;</div><p class="card-title">Expert Care</p><p class="card-desc">Access to leading specialists throughout the study.</p></div>
      <div class="card"><div class="card-icon">&#9989;</div><p class="card-title">No Cost</p><p class="card-desc">All study-related visits and treatments are provided free of charge.</p></div>
      <div class="card"><div class="card-icon">&#9889;</div><p class="card-title">Fast Results</p><p class="card-desc">Receive preliminary feedback quickly after assessment.</p></div>
    </div>
  </div>
</div>

<div id="cta" class="cta-section">
  <div class="cta-inner">
    <h2 class="cta-title">Ready to take the next step?</h2>
    <p class="cta-sub">Complete the eligibility check above and our team will be in touch.</p>
    <a href="#survey-section" class="btn btn-primary">Check Eligibility &rarr;</a>
  </div>
</div>

<footer>
  <div class="trust-bar">
    <span class="trust-item"><strong>{company_name}</strong> &mdash; Powered by AI</span>
    <span class="trust-item"><strong>Privacy Protected</strong> &mdash; Your data is confidential</span>
    <span class="trust-item"><strong>Ethics Approved</strong></span>
  </div>
  <p class="footer-copy">&copy; {company_name} &middot; {title}</p>
</footer>"""
