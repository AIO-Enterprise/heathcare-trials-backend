"""
Email Service
Sends transactional emails (OTP codes, notifications) via SMTP.

If SMTP_HOST is not configured in settings, the email body is printed to the
server log instead — useful for local development without a mail server.
"""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def _build_otp_email(to_email: str, user_name: str, code: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ALT Trials — Password Change Verification Code"
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = to_email

    plain = (
        f"Hi {user_name},\n\n"
        f"Your password change verification code is:\n\n"
        f"  {code}\n\n"
        f"This code expires in 10 minutes. "
        f"If you didn't request a password change, you can safely ignore this email.\n\n"
        f"— ALT Trials"
    )
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;
                background:#f9fafb;border-radius:12px;border:1px solid #e5e7eb">
      <h2 style="margin:0 0 8px;font-size:1.1rem;color:#111827">Password Change Request</h2>
      <p style="margin:0 0 24px;color:#6b7280;font-size:0.875rem">
        Hi {user_name}, use the code below to verify your password change.
      </p>
      <div style="text-align:center;background:#fff;border:1px solid #e5e7eb;border-radius:10px;
                  padding:24px;letter-spacing:0.35em;font-size:2rem;font-weight:700;color:#111827">
        {code}
      </div>
      <p style="margin:20px 0 0;color:#9ca3af;font-size:0.75rem;text-align:center">
        Expires in 10 minutes &nbsp;·&nbsp; ALT Trials
      </p>
    </div>
    """
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg


def _send_sync(to_email: str, msg: MIMEMultipart) -> None:
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


async def send_otp_email(to_email: str, user_name: str, code: str) -> None:
    """
    Send an OTP verification email asynchronously.
    Falls back to console log when SMTP is not configured (dev mode).
    """
    if not settings.SMTP_HOST:
        logger.info(
            "[DEV — no SMTP] Password reset OTP for %s: %s",
            to_email,
            code,
        )
        return

    msg = _build_otp_email(to_email, user_name, code)
    try:
        await asyncio.to_thread(_send_sync, to_email, msg)
        logger.info("OTP email sent to %s", to_email)
    except Exception as exc:
        logger.error("Failed to send OTP email to %s: %s", to_email, exc)
        raise
