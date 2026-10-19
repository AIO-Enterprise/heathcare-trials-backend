"""Website Agent - static landing page generator with one fixed shell."""

import json
import logging
import os
from html import escape as _html_escape

from app.core.config import settings
from app.core.llm import get_ai_client
from app.core.media import media_url
from app.models.models import Advertisement, BrandKit, Company
from app.services.storage import file_storage

logger = logging.getLogger(__name__)

# ── System prompt (adds optional FAQ) ─────────────────────────────────────────
_BODY_SYSTEM_PROMPT = """You are an expert clinical-trial / healthcare marketing copywriter.

Return ONLY valid JSON for a fixed landing-page shell. No markdown. No prose outside JSON.

Schema:
{
  "hero_badge": "short badge text, sentence case",
  "hero_headline": "headline text",
  "hero_headline_accent": "1-4 word phrase from the headline that gets highlighted",
  "hero_sub": "hero supporting copy, max 2 sentences",
  "hero_cta": "primary CTA label",
  "benefit_chips": ["chip 1", "chip 2", "chip 3"],
  "survey_title": "survey section title",
  "survey_sub": "survey section subtitle",
  "survey_questions": [
    {
      "question": "question text",
      "eligible": ["yes", "maybe"],
      "options": [
        {"label": "Yes", "value": "yes"},
        {"label": "No", "value": "no"}
      ]
    }
  ],
  "eligible_title": "eligible result heading",
  "eligible_text": "eligible result body",
  "ineligible_title": "ineligible result heading",
  "ineligible_text": "ineligible result body",
  "interaction_title": "title for the post-survey interaction section",
  "interaction_sub": "subtitle for the post-survey interaction section",
  "features_title": "features section title",
  "features_sub": "features section subtitle",
  "features": [
    {"icon": "📊", "title": "feature title", "description": "feature description"}
  ],
  "faq": [
    {"q": "question a participant would ask", "a": "short, honest answer"}
  ],
  "cta_title": "closing CTA title",
  "cta_sub": "closing CTA supporting text"
}

Rules:
1. Output 4-6 survey questions.
2. Each survey question must have 2-4 options.
3. Option values must be short lowercase tokens like yes/no/maybe.
4. eligible must contain the option values that count as eligible.
5. interaction_title and interaction_sub should work for either voice-call or chat follow-up.
6. features must contain exactly 3 items. The first is shown largest, so lead with the strongest benefit.
7. faq must contain 3-4 items covering: time commitment, privacy, cost/obligation, what happens after eligibility.
   Never promise payment, outcomes or treatment results.
8. Keep copy concise and concrete. No placeholders.
"""

# ── CSS template: plain string + .replace() so no brace-doubling ──────────────
_CSS_TEMPLATE = """
@import url('https://fonts.googleapis.com/css2?family=__FONT_URL__:wght@400;500;600;700;800&display=swap');

:root{
  --primary:__PRIMARY__; --secondary:__SECONDARY__; --accent:__ACCENT__;
  --ink:#0f172a; --text:#334155; --muted:#64748b; --border:#e2e8f0; --bg:#f6f7fb; --white:#fff;
  --tint:color-mix(in srgb,var(--primary) 7%,#fff);
  --accent-soft:color-mix(in srgb,var(--accent) 14%,#fff);
  --shadow:0 1px 2px rgba(15,23,42,.05),0 24px 56px -20px color-mix(in srgb,var(--primary) 40%,transparent);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'__FONT__',system-ui,-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased}
:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 70%,#fff);outline-offset:2px}
[id]{scroll-margin-top:88px}

/* NAV */
nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;padding:14px 40px;background:rgba(255,255,255,.82);backdrop-filter:saturate(1.4) blur(14px);-webkit-backdrop-filter:saturate(1.4) blur(14px);border-bottom:1px solid var(--border)}
.logo{display:flex;align-items:center;gap:10px;color:var(--ink);font-size:1.05rem;font-weight:800;text-decoration:none;letter-spacing:-.01em}
.logo img{height:34px;object-fit:contain}
.nav-cta{background:var(--ink);color:#fff;padding:10px 22px;border-radius:999px;font-size:.88rem;font-weight:700;text-decoration:none;transition:transform .15s,box-shadow .15s}
.nav-cta:hover{transform:translateY(-1px);box-shadow:0 8px 20px -8px rgba(15,23,42,.5)}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:8px;padding:15px 30px;border-radius:999px;font-size:1rem;font-weight:700;text-decoration:none;border:none;cursor:pointer;font-family:inherit;transition:transform .15s,box-shadow .15s}
.btn:hover{transform:translateY(-2px)}
.btn-primary{background:var(--accent);color:#fff;box-shadow:0 10px 24px -10px var(--accent)}
.btn-dark{background:var(--secondary);color:#fff}
.btn-light{background:#fff;color:var(--ink)}

/* HERO */
.hero{position:relative;overflow:hidden;padding:64px 40px 88px;background:
  radial-gradient(55% 60% at 92% -5%,color-mix(in srgb,var(--accent) 22%,transparent),transparent 70%),
  radial-gradient(50% 55% at -5% 105%,color-mix(in srgb,var(--primary) 16%,transparent),transparent 70%),#fff}
.hero-grid{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:1.05fr .95fr;gap:64px;align-items:start}
.hero-copy{padding-top:28px}
.hero-badge{display:inline-flex;align-items:center;gap:8px;background:var(--accent-soft);color:var(--primary);border-radius:999px;padding:7px 16px 7px 12px;font-size:.84rem;font-weight:700;margin-bottom:22px}
.hero-badge::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--accent)}
.hero-headline{font-size:clamp(2.3rem,4.6vw,3.7rem);font-weight:800;line-height:1.07;letter-spacing:-.035em;color:var(--ink);margin-bottom:20px}
.hero-headline span{background:linear-gradient(transparent 64%,color-mix(in srgb,var(--accent) 38%,transparent) 64%);padding:0 .08em}
.hero-sub{font-size:1.12rem;color:var(--text);max-width:50ch;margin-bottom:26px}
.benefit-chips{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:32px}
.chip{display:inline-flex;align-items:center;gap:7px;background:#fff;border:1px solid var(--border);border-radius:999px;padding:7px 15px;font-size:.86rem;font-weight:600;color:var(--ink)}
.chip::before{content:'';width:16px;height:16px;border-radius:50%;background:var(--accent) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M4.5 8.5l2.2 2.2 4.8-5' fill='none' stroke='white' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat}
.hero-actions{display:flex;align-items:center;flex-wrap:wrap;gap:18px}
.hero-note{font-size:.88rem;color:var(--muted)}

/* SURVEY (lives in the hero) */
.survey-card{background:#fff;border:1px solid var(--border);border-radius:28px;padding:34px;box-shadow:var(--shadow)}
.survey-title{font-size:1.45rem;font-weight:800;color:var(--ink);letter-spacing:-.02em;margin-bottom:6px}
.survey-sub{color:var(--muted);font-size:.93rem;margin-bottom:22px}
.survey-progress-bar{height:8px;background:var(--tint);border-radius:999px;overflow:hidden;margin-bottom:10px}
.survey-progress-fill{height:100%;width:0%;border-radius:999px;background:linear-gradient(90deg,var(--primary),var(--accent));transition:width .45s ease}
.survey-step-count{font-size:.8rem;color:var(--muted);font-weight:600;margin-bottom:24px}
.survey-step{display:none}
.survey-step.active{display:block;animation:step-in .28s ease}
@keyframes step-in{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:none}}
.survey-question{font-size:1.2rem;font-weight:700;color:var(--ink);line-height:1.4;margin-bottom:18px}
.option-grid{display:grid;gap:10px}
.option-btn{display:flex;align-items:center;gap:14px;width:100%;padding:15px 18px;border:1.5px solid var(--border);border-radius:14px;background:#fff;font-family:inherit;font-size:.96rem;font-weight:600;color:var(--ink);text-align:left;cursor:pointer;transition:border-color .15s,background .15s,transform .1s}
.option-btn::before{content:'';flex-shrink:0;width:20px;height:20px;border-radius:50%;border:2px solid #cbd5e1;transition:all .15s}
.option-btn:hover{border-color:var(--accent);background:var(--accent-soft)}
.option-btn:active{transform:scale(.99)}
.option-btn.selected{border-color:var(--accent);background:var(--accent-soft);color:var(--primary)}
.option-btn.selected::before{border-color:var(--accent);background:var(--accent);box-shadow:inset 0 0 0 4px #fff}
.survey-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:26px;padding-top:20px;border-top:1px solid var(--border)}
.survey-nav-right{display:flex;align-items:center;gap:10px}
.survey-voice-row{display:flex;align-items:center}
.btn-voice-call{display:inline-flex;align-items:center;gap:6px;background:transparent;color:var(--primary);border:1.5px solid var(--border);padding:9px 16px;border-radius:999px;font-size:.82rem;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap;transition:border-color .15s,background .15s}
.btn-voice-call:hover{border-color:var(--primary);background:var(--tint)}
.btn-survey-prev{background:transparent;border:1.5px solid var(--border);color:var(--muted);padding:11px 20px;border-radius:999px;font-size:.9rem;font-weight:600;cursor:pointer;font-family:inherit}
.btn-survey-prev:hover{border-color:var(--primary);color:var(--primary)}
.btn-survey-next,.btn-survey-submit{background:var(--accent);color:#fff;padding:12px 26px;border-radius:999px;font-size:.92rem;font-weight:700;border:none;cursor:pointer;font-family:inherit;transition:transform .15s}
.btn-survey-next:hover,.btn-survey-submit:hover{transform:translateY(-1px)}
.btn-survey-next:disabled,.btn-survey-submit:disabled{opacity:.4;cursor:not-allowed}
.survey-result{display:none;text-align:center;padding:4px 0}
.result-card{border-radius:18px;padding:28px 24px;display:none}
.result-card.eligible{background:var(--accent-soft);border:2px solid var(--accent)}
.result-card.ineligible{background:rgba(245,158,11,.08);border:2px solid #f59e0b}
.result-icon{font-size:2.4rem;margin-bottom:10px}
.result-card h3{font-size:1.25rem;font-weight:800;color:var(--ink);margin-bottom:8px}
.result-card p{font-size:.95rem;color:var(--text)}
.result-card.eligible .result-icon::before{content:'✅'}
.result-card.ineligible .result-icon::before{content:'💛'}
.consent-box{margin-top:18px;background:rgba(245,158,11,.06);border:1.5px solid #f59e0b;border-radius:12px;padding:16px 18px;text-align:left}
.consent-label{display:flex;align-items:flex-start;gap:12px;cursor:pointer;font-size:.9rem;color:var(--text);line-height:1.5}
.consent-checkbox{width:18px;height:18px;accent-color:var(--accent);flex-shrink:0;margin-top:2px}
.consent-submit{display:none;margin-top:14px;width:100%;padding:11px;border-radius:999px;background:var(--accent);color:#fff;font-size:.9rem;font-weight:700;border:none;cursor:pointer;font-family:inherit}
.consent-thanks{display:none;margin-top:14px;text-align:center;font-size:.88rem;color:var(--primary);font-weight:700}

/* REGISTRATION (revealed after survey) */
#reg-section{display:none;background:var(--bg);padding:56px 40px}
.reg-card{max-width:560px;margin:0 auto;background:#fff;border:1px solid var(--border);border-radius:28px;padding:40px 44px;box-shadow:var(--shadow)}
.reg-title{font-size:1.45rem;font-weight:800;color:var(--ink);letter-spacing:-.02em;margin-bottom:6px}
.reg-sub{font-size:.92rem;color:var(--muted);margin-bottom:22px}
.reg-note{padding:13px 16px;border-radius:12px;font-size:.88rem;font-weight:600;margin-bottom:22px;line-height:1.5}
.reg-note.eligible{background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);color:var(--ink)}
.reg-note.ineligible{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);color:#92400e}
.reg-field{margin-bottom:16px}
.reg-label{display:block;font-size:.82rem;font-weight:700;color:var(--ink);margin-bottom:6px}
.reg-input,.reg-select{width:100%;padding:12px 14px;border:1.5px solid var(--border);border-radius:12px;font-size:.95rem;font-family:inherit;color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s;background:#fff;appearance:none}
.reg-input:focus,.reg-select:focus{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
.reg-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.reg-btn{width:100%;background:var(--accent);color:#fff;border:none;border-radius:999px;padding:14px;font-size:1rem;font-weight:700;cursor:pointer;font-family:inherit;margin-top:8px;transition:transform .15s}
.reg-btn:hover{transform:translateY(-1px)}
.reg-btn:disabled{opacity:.5;cursor:not-allowed}
.reg-error{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);color:#dc2626;padding:11px 14px;border-radius:10px;font-size:.85rem;margin-bottom:14px;display:none}
.reg-done{background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);color:#16a34a;padding:22px 18px;border-radius:16px;font-size:.95rem;line-height:1.6;display:none;text-align:center;font-weight:600}
.reg-privacy{font-size:.76rem;color:var(--muted);text-align:center;margin-top:12px}

/* INTERACTION (voice options) */
.interaction-section{background:var(--bg);padding:72px 40px;text-align:center}
.interaction-section__title{font-size:clamp(1.6rem,3vw,2.2rem);font-weight:800;letter-spacing:-.025em;color:var(--ink);margin-bottom:10px}
.interaction-section__sub{color:var(--muted);font-size:1.02rem;max-width:52ch;margin:0 auto 40px}
.interaction-cards{display:flex;justify-content:center;gap:20px;flex-wrap:wrap;max-width:780px;margin:0 auto 24px}
.interaction-card{background:#fff;border:1.5px solid var(--border);border-radius:24px;padding:34px 28px;width:340px;display:flex;flex-direction:column;align-items:center;gap:10px;transition:box-shadow .2s,transform .2s}
.interaction-card:hover{box-shadow:var(--shadow);transform:translateY(-3px)}
.interaction-card.featured{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
.interaction-card__icon{width:60px;height:60px;border-radius:18px;background:var(--accent-soft);display:flex;align-items:center;justify-content:center;font-size:1.6rem;margin-bottom:4px}
.interaction-card__title{font-size:1.1rem;font-weight:800;color:var(--ink)}
.interaction-card__desc{font-size:.9rem;color:var(--muted)}
.interaction-card__btn{width:100%;padding:13px 20px;border-radius:999px;font-size:.95rem;font-weight:700;border:none;cursor:pointer;margin-top:6px;color:#fff;font-family:inherit}
.interaction-card.featured .interaction-card__btn{background:var(--accent)}
.interaction-card:not(.featured) .interaction-card__btn{background:var(--secondary)}
.interaction-card__meta{font-size:.78rem;color:var(--muted)}
.interaction-note{font-size:.82rem;color:var(--muted);max-width:520px;margin:0 auto}

/* SECTIONS */
.section{padding:88px 40px;max-width:1180px;margin:0 auto}
.section-title{font-size:clamp(1.7rem,3.2vw,2.4rem);font-weight:800;letter-spacing:-.03em;color:var(--ink);margin-bottom:10px}
.section-sub{color:var(--muted);font-size:1.05rem;max-width:56ch;margin-bottom:44px}

/* WHAT HAPPENS NEXT (a real sequence, so numbered) */
.steps-wrap{background:#fff;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:28px}
.step{position:relative;padding-top:4px}
.step-num{width:42px;height:42px;border-radius:50%;background:var(--primary);color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center;margin-bottom:16px}
.step:not(:last-child)::after{content:'';position:absolute;top:21px;left:56px;right:-14px;height:2px;background:repeating-linear-gradient(90deg,var(--border) 0 6px,transparent 6px 12px)}
.step h3{font-size:1.05rem;font-weight:800;color:var(--ink);margin-bottom:6px}
.step p{font-size:.93rem;color:var(--muted)}

/* BENEFITS (bento: first card is the lead) */
.bento{display:grid;grid-template-columns:1.3fr 1fr;grid-template-rows:auto auto;gap:18px}
.card{background:#fff;border:1px solid var(--border);border-radius:24px;padding:28px}
.card:first-child{grid-row:span 2;background:linear-gradient(160deg,var(--tint),#fff 70%);display:flex;flex-direction:column;justify-content:flex-end;min-height:320px;padding:36px}
.card-icon{width:48px;height:48px;border-radius:14px;background:var(--accent-soft);display:flex;align-items:center;justify-content:center;font-size:1.35rem;margin-bottom:16px}
.card:first-child .card-icon{width:64px;height:64px;font-size:1.8rem;margin-bottom:auto}
.card-title{font-size:1.02rem;font-weight:800;color:var(--ink);margin-bottom:6px}
.card:first-child .card-title{font-size:1.45rem;letter-spacing:-.02em}
.card-desc{font-size:.93rem;color:var(--muted)}

/* FAQ */
.faq{max-width:760px}
.faq details{background:#fff;border:1px solid var(--border);border-radius:16px;margin-bottom:10px;transition:border-color .15s}
.faq details[open]{border-color:var(--accent)}
.faq summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 22px;font-weight:700;color:var(--ink)}
.faq summary::-webkit-details-marker{display:none}
.faq summary::after{content:'+';font-size:1.4rem;font-weight:500;color:var(--primary);transition:transform .2s}
.faq details[open] summary::after{transform:rotate(45deg)}
.faq p{padding:0 22px 20px;color:var(--text);font-size:.95rem}

/* CLOSING CTA (inset card; also used by the chatbot reveal section) */
.cta-section{padding:24px 40px 88px}
.cta-inner{max-width:1100px;margin:0 auto;text-align:center;color:#fff;padding:72px 40px;border-radius:36px;background:
  radial-gradient(60% 80% at 100% 0%,color-mix(in srgb,var(--accent) 45%,transparent),transparent 70%),
  linear-gradient(135deg,var(--primary),var(--secondary))}
.cta-title{font-size:clamp(1.8rem,4vw,2.7rem);font-weight:800;line-height:1.12;letter-spacing:-.03em;margin:0 auto 14px;max-width:20ch}
.cta-sub{font-size:1.05rem;color:rgba(255,255,255,.8);max-width:52ch;margin:0 auto 30px}

/* FOOTER */
footer{background:var(--secondary);padding:32px 40px}
.trust-bar{display:flex;justify-content:center;gap:36px;flex-wrap:wrap;margin-bottom:16px}
.trust-item{font-size:.84rem;color:rgba(255,255,255,.62)}
.trust-item strong{color:rgba(255,255,255,.92)}
.footer-copy{text-align:center;font-size:.76rem;color:rgba(255,255,255,.4)}

/* CHAT WIDGET */
#chat-float{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;align-items:flex-end;gap:12px}
.chat-toggle-btn{width:60px;height:60px;border-radius:50%;background:var(--accent);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1.5rem;box-shadow:0 8px 28px rgba(15,23,42,.28);transition:transform .2s;flex-shrink:0}
.chat-toggle-btn:hover{transform:scale(1.08)}
.chat-unread{position:absolute;top:0;right:0;width:16px;height:16px;background:#ef4444;border-radius:50%;border:2px solid #fff;display:flex;align-items:center;justify-content:center;font-size:.6rem;color:#fff;font-weight:700}
.chat-panel{background:#fff;border-radius:22px;box-shadow:0 16px 56px rgba(15,23,42,.24);width:350px;overflow:hidden;flex-direction:column;display:none;transform-origin:bottom right}
.chat-panel.open{display:flex;animation:chat-pop .22s ease}
@keyframes chat-pop{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.chat-panel__header{background:linear-gradient(135deg,var(--primary),var(--secondary));padding:15px 16px;display:flex;align-items:center;gap:10px}
.chat-avatar{width:38px;height:38px;border-radius:50%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0}
.chat-agent-info{flex:1}
.chat-agent-name{color:#fff;font-size:.93rem;font-weight:700}
.chat-agent-status{color:rgba(255,255,255,.75);font-size:.72rem;display:flex;align-items:center;gap:4px}
.chat-agent-status::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block}
.chat-close-btn{background:rgba(255,255,255,.15);border:none;color:#fff;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:.85rem;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.chat-close-btn:hover{background:rgba(255,255,255,.28)}
.chat-messages{height:270px;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:var(--bg)}
.chat-bubble{max-width:82%;padding:10px 14px;border-radius:16px;font-size:.88rem;line-height:1.5}
.chat-bubble.bot{background:#fff;border:1px solid var(--border);color:var(--ink);align-self:flex-start;border-bottom-left-radius:4px}
.chat-bubble.user{background:var(--accent);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.chat-footer{padding:10px 12px;border-top:1px solid var(--border);display:flex;gap:8px;background:#fff}
.chat-input{flex:1;border:1px solid var(--border);border-radius:999px;padding:9px 15px;font-size:.88rem;font-family:inherit;outline:none;color:var(--ink);background:var(--bg)}
.chat-input:focus{border-color:var(--accent);background:#fff}
.chat-send-btn{background:var(--accent);color:#fff;border:none;border-radius:50%;width:36px;height:36px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.95rem;flex-shrink:0;font-family:inherit}
.chat-send-btn:hover{opacity:.85}

/* ONE ENTRANCE: hero copy rises in once on load */
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.hero-copy>*{animation:rise .6s ease both}
.hero-copy>*:nth-child(2){animation-delay:.07s}
.hero-copy>*:nth-child(3){animation-delay:.14s}
.hero-copy>*:nth-child(4){animation-delay:.21s}
.hero-copy>*:nth-child(5){animation-delay:.28s}
.survey-card{animation:rise .7s .15s ease both}

/* RESPONSIVE */
@media(max-width:960px){
  .hero-grid{grid-template-columns:1fr;gap:36px}
  .hero-copy{padding-top:8px}
  .bento{grid-template-columns:1fr}
  .card:first-child{grid-row:auto;min-height:0}
  .card:first-child .card-icon{margin-bottom:16px}
  .steps{grid-template-columns:1fr}
  .step:not(:last-child)::after{display:none}
}
@media(max-width:768px){
  nav{padding:12px 18px}
  .hero{padding:36px 18px 56px}
  .survey-card{padding:26px 20px;border-radius:22px}
  .section{padding:56px 20px}
  .interaction-section{padding:52px 20px}
  .interaction-card{width:100%}
  #reg-section{padding:32px 16px}
  .reg-card{padding:28px 20px}
  .reg-row{grid-template-columns:1fr}
  .cta-section{padding:12px 16px 56px}
  .cta-inner{padding:48px 22px;border-radius:26px}
  footer{padding:26px 20px}
  #chat-float{bottom:16px;right:16px}
  .chat-panel{width:300px}
}
@media(prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{animation:none!important;transition:none!important}
}
"""

# ── Fallback content (used by mock mode AND whenever the LLM omits a block) ───
_DEFAULT_CONTENT = {
    "survey_questions": [
        {
            "question": "Are you 18 years of age or older?",
            "eligible": ["yes"],
            "options": [
                {"label": "Yes", "value": "yes"},
                {"label": "No, I'm under 18", "value": "no"},
            ],
        },
        {
            "question": "Have you been diagnosed with a relevant condition in the past 12 months?",
            "eligible": ["yes"],
            "options": [
                {"label": "Yes", "value": "yes"},
                {"label": "No", "value": "no"},
                {"label": "I'm not sure", "value": "unsure"},
            ],
        },
        {
            "question": "Are you available for follow-up appointments over the next 6 months?",
            "eligible": ["yes", "maybe"],
            "options": [
                {"label": "Yes, fully available", "value": "yes"},
                {"label": "Possibly, it depends on my schedule", "value": "maybe"},
                {"label": "No, I'm not available", "value": "no"},
            ],
        },
    ],
    "features": [
        {"icon": "🩺", "title": "Expert, attentive care", "description": "Specialists monitor your health closely throughout the study."},
        {"icon": "🔒", "title": "Private by design", "description": "Your details go only to the study team, never to third parties."},
        {"icon": "🤝", "title": "A real person guides you", "description": "If you look like a match, the team walks you through every next step."},
    ],
    "faq": [
        {"q": "How long does the eligibility check take?", "a": "About two minutes. You can go back and change an answer before you see your result."},
        {"q": "Who sees my answers?", "a": "Only the study team. Your details are used to contact you about this trial and are not shared with third parties."},
        {"q": "Am I committing to anything?", "a": "No. The check and the first conversation are there to help you decide. You choose whether to take part."},
        {"q": "What happens if I appear eligible?", "a": "You leave your contact details and the study team reaches out to explain the trial and answer your questions."},
    ],
}

_STEPS = [
    ("Answer a few questions", "A short check against the study's basic criteria. Nothing to prepare."),
    ("Share your contact details", "So the study team knows who to reach and how."),
    ("Talk to the study team", "They explain the trial, answer questions and book your first visit if it's right for you."),
]


class WebsiteUIMixin:
    # ── CSS ───────────────────────────────────────────────────────────────────
    def _build_css(self, brand_kit: BrandKit | None) -> str:
        primary = (brand_kit.primary_color if brand_kit else None) or "#1e3a5f"
        secondary = (brand_kit.secondary_color if brand_kit else None) or "#0f172a"
        accent = (brand_kit.accent_color if brand_kit else None) or "#10b981"
        font = (brand_kit.primary_font if brand_kit else None) or "Plus Jakarta Sans"
        return (
            _CSS_TEMPLATE.replace("__PRIMARY__", primary)
            .replace("__SECONDARY__", secondary)
            .replace("__ACCENT__", accent)
            .replace("__FONT_URL__", font.replace(" ", "+"))
            .replace("__FONT__", font)
        )

    # ── Body skeleton ─────────────────────────────────────────────────────────
    def _render_body(self, content: dict, ad: Advertisement, company: Company | None) -> str:
        esc = _html_escape
        content = content if isinstance(content, dict) else {}

        company_raw = company.name if company else "Our Company"
        company_name = esc(company_raw, quote=True)
        logo_url = esc(media_url(company.logo_url), quote=True) if (company and company.logo_url) else None
        logo_html = f'<img src="{logo_url}" alt="{company_name}" />' if logo_url else company_name
        title = esc(ad.title, quote=True)
        ad_types = [t.lower() for t in (ad.ad_type or [])]
        bot_config = ad.bot_config if isinstance(ad.bot_config, dict) else {}
        bot_raw = str(bot_config.get("name", "Assistant"))

        def text(key: str, fallback: str) -> str:
            return esc(str(content.get(key) or fallback), quote=False)

        # chips
        chips = content.get("benefit_chips")
        chips = [str(c).strip() for c in chips if str(c).strip()][:3] if isinstance(chips, list) else []
        chips = chips or ["Confidential", "Guided by a real team", "Quick response"]
        chips_html = "\n".join(f'    <span class="chip">{esc(c, quote=False)}</span>' for c in chips)

        # survey
        def build_steps(questions) -> list[str]:
            blocks: list[str] = []
            for i, q in enumerate((questions or [])[:6], start=1):
                if not isinstance(q, dict):
                    continue
                q_text = esc(str(q.get("question") or f"Question {i}"), quote=False)
                elig = q.get("eligible") if isinstance(q.get("eligible"), list) else []
                elig_csv = ",".join(str(v).strip().lower() for v in elig if str(v).strip()) or "yes"
                opts = q.get("options") if isinstance(q.get("options"), list) else []
                opt_html = []
                for opt in opts[:4]:
                    if not isinstance(opt, dict):
                        continue
                    label = esc(str(opt.get("label") or "Option"), quote=False)
                    value = esc(str(opt.get("value") or "option").strip().lower(), quote=True)
                    opt_html.append(f'        <button class="option-btn" data-value="{value}">{label}</button>')
                if not opt_html:
                    opt_html = [
                        '        <button class="option-btn" data-value="yes">Yes</button>',
                        '        <button class="option-btn" data-value="no">No</button>',
                    ]
                active = " active" if not blocks else ""
                joined = "\n".join(opt_html)
                blocks.append(
                    f'''    <div class="survey-step{active}" data-step="{i}" data-eligible="{esc(elig_csv, quote=True)}">
      <p class="survey-question">{q_text}</p>
      <div class="option-grid">
{joined}
      </div>
    </div>'''
                )
            return blocks

        question_blocks = build_steps(content.get("survey_questions")) or build_steps(
            _DEFAULT_CONTENT["survey_questions"]
        )
        steps_html = "\n\n".join(question_blocks)

        # benefits (exactly 3, first one renders large)
        raw_features = content.get("features") if isinstance(content.get("features"), list) else []
        cards = []
        for item in raw_features[:3]:
            if not isinstance(item, dict):
                continue
            cards.append(
                '<div class="card"><div class="card-icon">{}</div><p class="card-title">{}</p>'
                '<p class="card-desc">{}</p></div>'.format(
                    esc(str(item.get("icon") or "✨"), quote=False),
                    esc(str(item.get("title") or "Feature"), quote=False),
                    esc(str(item.get("description") or "Description"), quote=False),
                )
            )
        if len(cards) < 3:
            cards = [
                '<div class="card"><div class="card-icon">{}</div><p class="card-title">{}</p>'
                '<p class="card-desc">{}</p></div>'.format(
                    f["icon"], esc(f["title"], quote=False), esc(f["description"], quote=False)
                )
                for f in _DEFAULT_CONTENT["features"]
            ]
        cards_html = "\n      ".join(cards)

        # faq
        raw_faq = content.get("faq") if isinstance(content.get("faq"), list) else []
        faq = [
            (str(f.get("q") or "").strip(), str(f.get("a") or "").strip())
            for f in raw_faq[:4]
            if isinstance(f, dict)
        ]
        faq = [(q, a) for q, a in faq if q and a] or [(f["q"], f["a"]) for f in _DEFAULT_CONTENT["faq"]]
        faq_html = "\n".join(
            f"      <details><summary>{esc(q, quote=False)}</summary><p>{esc(a, quote=False)}</p></details>"
            for q, a in faq
        )

        # how it works
        steps_strip = "\n".join(
            f'      <div class="step"><div class="step-num">{n}</div><h3>{esc(h, quote=False)}</h3>'
            f"<p>{esc(p, quote=False)}</p></div>"
            for n, (h, p) in enumerate(_STEPS, start=1)
        )

        # post-survey interaction (id="interaction-reveal" is hidden/revealed by _wrap_html + JS)
        interaction_html = ""
        if "voicebot" in ad_types:
            interaction_html = f"""
<section class="interaction-section" id="interaction-reveal">
  <h2 class="interaction-section__title">{text('interaction_title', f'How would you like to speak with {bot_raw}?')}</h2>
  <p class="interaction-section__sub">{text('interaction_sub', 'Choose the way that suits you best. Both go straight to our team.')}</p>
  <div class="interaction-cards">
    <div class="interaction-card featured">
      <div class="interaction-card__icon">📞</div>
      <p class="interaction-card__title">Call me now</p>
      <p class="interaction-card__desc">Enter your number and your phone rings within seconds.</p>
      <button class="interaction-card__btn" id="voice-call-btn">Call me now</button>
      <p class="interaction-card__meta">Instant and free</p>
    </div>
    <div class="interaction-card">
      <div class="interaction-card__icon">📅</div>
      <p class="interaction-card__title">Book an appointment</p>
      <p class="interaction-card__desc">Pick a time that works for you and reserve your first visit.</p>
      <button class="interaction-card__btn" id="schedule-call-btn">Book appointment</button>
      <p class="interaction-card__meta">You choose the time</p>
    </div>
  </div>
  <p class="interaction-note">Your responses are confidential and used only to assess eligibility.</p>
</section>"""
        elif "chatbot" in ad_types:
            interaction_html = f"""
<section class="cta-section" id="interaction-reveal">
  <div class="cta-inner">
    <h2 class="cta-title">{text('interaction_title', f'Chat with {bot_raw} now')}</h2>
    <p class="cta-sub">{text('interaction_sub', f'Open the chat button in the bottom-right corner. {bot_raw} is ready to help.')}</p>
  </div>
</section>"""

        headline = text("hero_headline", title)
        accent = text("hero_headline_accent", "")
        if accent and accent in headline:
            headline = headline.replace(accent, f"<span>{accent}</span>", 1)

        cta_label = text("hero_cta", "Check my eligibility")

        return f"""<nav>
  <a href="#" class="logo">{logo_html}</a>
  <a href="#survey-section" class="nav-cta">{cta_label}</a>
</nav>

<section class="hero">
  <div class="hero-grid">
    <div class="hero-copy">
      <div class="hero-badge">{text('hero_badge', 'Now enrolling')}</div>
      <h1 class="hero-headline">{headline}</h1>
      <p class="hero-sub">{text('hero_sub', 'Answer a few questions to see whether this study may be right for you.')}</p>
      <div class="benefit-chips">
{chips_html}
      </div>
      <div class="hero-actions">
        <a href="#survey-section" class="btn btn-primary">{cta_label}</a>
        <span class="hero-note">About 2 minutes. No obligation.</span>
      </div>
    </div>

    <div class="survey-card" id="survey-section">
      <h2 class="survey-title">{text('survey_title', 'Check your eligibility')}</h2>
      <p class="survey-sub">{text('survey_sub', 'Answer a few quick questions. It takes about 2 minutes.')}</p>
      <div class="survey-progress-bar"><div class="survey-progress-fill" id="survey-progress"></div></div>
      <p class="survey-step-count" id="survey-step-count">Question 1 of {len(question_blocks)}</p>

{steps_html}

      <div class="survey-result" id="survey-result">
        <div class="result-card eligible" id="result-eligible">
          <div class="result-icon"></div>
          <h3>{text('eligible_title', 'You may be eligible')}</h3>
          <p>{text('eligible_text', 'Your answers meet the initial criteria. Leave your details below and the study team will be in touch.')}</p>
        </div>
        <div class="result-card ineligible" id="result-ineligible">
          <div class="result-icon"></div>
          <h3>{text('ineligible_title', 'Not a match for this study')}</h3>
          <p>{text('ineligible_text', 'You may not meet every current criterion, but other studies may open soon.')}</p>
          <div class="consent-box">
            <label class="consent-label">
              <input type="checkbox" class="consent-checkbox" id="consent-check" />
              You don't meet the criteria for this trial, but you might qualify for upcoming ones. May we keep your details to notify you?
            </label>
            <button class="consent-submit" id="consent-submit">Yes, keep me updated</button>
            <p class="consent-thanks" id="consent-thanks">&#10003; Thank you. We'll be in touch when a matching trial opens.</p>
          </div>
        </div>
      </div>

      <div class="survey-nav">
        <div class="survey-voice-row">
          <button class="btn-voice-call" id="survey-voice-btn">&#128222; Speak to us</button>
        </div>
        <div class="survey-nav-right">
          <button class="btn-survey-prev" id="survey-prev">&#8592; Back</button>
          <button class="btn-survey-next" id="survey-next">Next</button>
          <button class="btn-survey-submit" id="survey-submit">See my result</button>
        </div>
      </div>
    </div>
  </div>
</section>

{interaction_html}

<div class="steps-wrap">
  <div class="section">
    <h2 class="section-title">What happens next</h2>
    <p class="section-sub">Three short steps. You stay in control the whole way.</p>
    <div class="steps">
{steps_strip}
    </div>
  </div>
</div>

<div class="section">
  <h2 class="section-title">{text('features_title', 'Why take part')}</h2>
  <p class="section-sub">{text('features_sub', 'What you can expect when you take the next step.')}</p>
  <div class="bento">
      {cards_html}
  </div>
</div>

<div class="section" style="padding-top:0">
  <h2 class="section-title">Questions people ask</h2>
  <p class="section-sub">Straight answers before you decide.</p>
  <div class="faq">
{faq_html}
  </div>
</div>

<div id="cta" class="cta-section">
  <div class="cta-inner">
    <h2 class="cta-title">{text('cta_title', 'Ready to see if you qualify?')}</h2>
    <p class="cta-sub">{text('cta_sub', 'The eligibility check takes about two minutes and our team follows up personally.')}</p>
    <a href="#survey-section" class="btn btn-light">{cta_label}</a>
  </div>
</div>

<footer>
  <div class="trust-bar">
    <span class="trust-item"><strong>{company_name}</strong> &mdash; guided screening</span>
    <span class="trust-item"><strong>Privacy protected</strong> &mdash; your data stays confidential</span>
    <span class="trust-item"><strong>Fast follow-up</strong> from the study team</span>
  </div>
  <p class="footer-copy">&copy; {company_name} &middot; {title}</p>
</footer>"""

    # ── Mock (no API key): same skeleton, default copy ───────────────────────
    def _mock_body(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        return self._render_body({}, ad, company)


class WebsiteAgentService(WebsiteUIMixin):
    def __init__(self, company_id: str, ai=None):
        self.company_id = company_id
        self.ai = ai or get_ai_client()

    async def generate_website(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        from datetime import date, timedelta

        bot_config = ad.bot_config if isinstance(ad.bot_config, dict) else {}
        bot_name = str(bot_config.get("name") or "Assistant")
        bot_welcome = str(
            bot_config.get("welcome_message") or f"Hi! I'm {bot_name}. How can I help you today?"
        )
        today = date.today()
        win_start = max(ad.trial_start_date, today) if ad.trial_start_date else today
        win_end = ad.trial_end_date if ad.trial_end_date else today + timedelta(days=30)

        body = await self._generate_body(ad, brand_kit, company)
        css = self._build_css(brand_kit)
        html = self._wrap_html(
            ad.title,
            css,
            body,
            [t.lower() for t in (ad.ad_type or [])],
            bot_name,
            bot_welcome,
            ad_id=ad.id,
            booking_window_start=str(win_start),
            booking_window_end=str(win_end),
        )
        # Claude's country-flag emoji arrive as lone surrogates — invalid UTF-8.
        html = html.encode("utf-8", errors="ignore").decode("utf-8")
        filename = f"website_{ad.id}.html"
        return file_storage.save_bytes(html.encode("utf-8"), f"outputs/{self.company_id}/{ad.id}", filename)

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
        # HTML-text context escaping for tenant-supplied strings (title, bot
        # name/welcome). The AI-generated body is markup by design and must
        # NOT be escaped; booking windows are server-generated dates.
        title = _html_escape(title or "", quote=True)
        bot_name = _html_escape(bot_name or "Assistant", quote=True)
        bot_welcome = _html_escape(bot_welcome or "", quote=False)
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
  var API_BASE     = '{settings.backend_base}';
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
  var API_BASE = '{settings.backend_base}';

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
  var API    = '{settings.backend_base}';
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

  /* Book Appointment buttons (registration + interaction card) open it */
  if (apptSec) {{
    [bookBtn, document.getElementById('schedule-call-btn')].forEach(function (b) {{
      if (!b) return;
      b.addEventListener('click', function() {{
        apptSec.style.display = '';
        apptSec.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }});
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
/* Divider */
.cta-div{{display:flex;align-items:center;gap:10px;margin:12px 0;color:#94a3b8;font-size:.75rem;}}
.cta-div::before,.cta-div::after{{content:'';flex:1;height:1px;background:#e2e8f0;}}
/* Label */
.cta-lbl{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;display:block;margin-bottom:6px;}}
/* Date input */
/* Time slots */
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
}}
</style>
<script>
/* ── CTA Agent Modal — country picker + call now + schedule ─────────────── */
(function () {{

  var AD_ID    = '{ad_id}';
  var API_BASE = '{settings.backend_base}';

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
          '<button class="cta-btn-outline" id="cta-sched">&#128197;&ensp;Book an Appointment</button>' +
        '</div>' +
        /* ── Phase 3: success call ── */
        '<div class="cta-phase" id="cta-ph3" style="text-align:center;padding:38px 24px">' +
          '<div class="cta-ring cta-ring-green">&#9989;</div>' +
          '<p style="font-size:1.08rem;font-weight:800;color:#111;margin:0 0 8px">Calling you now!</p>' +
          '<p style="font-size:.85rem;color:#64748b;line-height:1.6;margin:0 0 6px">Your phone will ring in a few seconds. Pick up to speak with our agent.</p>' +
          '<p id="cta-ph3-hint" style="font-size:.75rem;color:#94a3b8;line-height:1.5;margin:0 0 22px;display:none">Didn’t get a call? The number you entered was <strong id="cta-ph3-num"></strong>. Check it’s correct and try again.</p>' +
          '<button class="cta-btn-outline" id="cta-ok1" style="max-width:180px;margin:0 auto">Done</button>' +
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
  var ph3Hint  = document.getElementById('cta-ph3-hint');
  var ph3Num   = document.getElementById('cta-ph3-num');
  var _callHintTimer = null;

  /* ── Phase helpers ──────────────────────────────────────────────────────── */
  var PH = ['cta-ph1','cta-ph3'];
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
  function apiCall(phone) {{
    var payload = {{phone_number: phone}};
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

  /* ── Book an appointment ──────────────────────────────────────── */
  schedBtn.addEventListener('click', function() {{
    closeModal();
    var appt = document.getElementById('appt-section');
    if (appt) {{
      appt.style.display = '';
      appt.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}
  }});

  /* ── Open on button click ───────────────────────────────────────────────── */
  document.addEventListener('click', function(e) {{
    var btn = e.target.closest('#survey-voice-btn, .btn-voice-call, #voice-call-btn');
    if (btn) openModal();
  }});

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
</body>
</html>"""

    def _build_body_prompt(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        company_name = company.name if company else "Our Company"
        industry = company.industry if company else ""
        adjectives = (brand_kit.adjectives if brand_kit else None) or "professional, trustworthy"
        strategy = ad.strategy_json or {}
        messaging = strategy.get("messaging", {}) if isinstance(strategy, dict) else {}
        core_msg = messaging.get("core_message", ad.title) if isinstance(messaging, dict) else ad.title
        cta_text = messaging.get("cta", "Get Started") if isinstance(messaging, dict) else "Get Started"
        key_phrases = messaging.get("key_phrases", []) if isinstance(messaging, dict) else []
        exec_sum = strategy.get("executive_summary", "") if isinstance(strategy, dict) else ""
        target_aud = strategy.get("target_audience", {}) if isinstance(strategy, dict) else {}
        primary_aud = target_aud.get("primary", "") if isinstance(target_aud, dict) else str(target_aud)
        kpis = strategy.get("kpis", []) if isinstance(strategy, dict) else []
        website_reqs = ad.website_reqs if isinstance(ad.website_reqs, dict) else {}
        must_have = website_reqs.get("must_have", []) if isinstance(website_reqs, dict) else []
        must_avoid = website_reqs.get("must_avoid", []) if isinstance(website_reqs, dict) else []
        ad_types = [t.lower() for t in (ad.ad_type or [])]

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

## Benefits / KPIs
{json.dumps(kpis, indent=2) if kpis else "Derive 3 concrete benefits from context."}

## Eligibility Criteria
{json.dumps(must_have, indent=2) if must_have else "Generate relevant eligibility questions from campaign context."}

## Must Avoid
{json.dumps(must_avoid, indent=2) if must_avoid else "Nothing flagged."}
"""

    async def _generate_body(
        self,
        ad: Advertisement,
        brand_kit: BrandKit | None,
        company: Company | None,
    ) -> str:
        if os.getenv("MOCK_AI") == "1" or not getattr(self.ai, "is_configured", lambda: False)():
            return self._mock_body(ad, brand_kit, company)

        raw = await self.ai.complete(
            system=_BODY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._build_body_prompt(ad, brand_kit, company)}],
            max_tokens=8192,
        )
        try:
            content = json.loads(raw.strip())
        except Exception:
            logger.warning("Website content JSON parse failed; falling back to mock shell", exc_info=True)
            return self._mock_body(ad, brand_kit, company)
        return self._render_body(content if isinstance(content, dict) else {}, ad, company)
