"""Website Agent - static landing page generator with one fixed shell."""

import json
import logging
import os
from html import escape as _html_escape

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
        body = await self._generate_body(ad, brand_kit, company)
        css = self._build_css(brand_kit)
        html = self._wrap_html(body, css)
        filename = f"website_{ad.id}.html"
        return file_storage.save_bytes(html.encode("utf-8"), f"outputs/{self.company_id}/{ad.id}", filename)

    def _wrap_html(self, body: str, css: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Landing Page</title>
  <style>{css}</style>
</head>
<body>
{body}
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
