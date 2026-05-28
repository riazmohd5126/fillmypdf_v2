"""
Email notification service for FillMyPDF.

Sends signing-related emails when SMTP is configured via .env.
All methods are no-ops (silent) if SMTP_HOST is not set — safe to call
unconditionally throughout the codebase.

Configure in .env:
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USERNAME=you@gmail.com
    SMTP_PASSWORD=app-password
    SMTP_FROM_ADDRESS=noreply@fillmypdf.app
    APP_BASE_URL=https://your-domain.com
"""

from __future__ import annotations

import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from ..config import settings


def _enabled() -> bool:
    return bool(settings.SMTP_HOST)


def _send(*, to: str, subject: str, html: str, plain: str) -> bool:
    """Send one email. Returns True on success, False on any error."""
    if not _enabled() or not to:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_ADDRESS}>"
        msg["To"] = to
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_FROM_ADDRESS, [to], msg.as_string())
        return True
    except Exception:
        traceback.print_exc()
        return False


def _base() -> str:
    return settings.APP_BASE_URL.rstrip("/")


# ── Signing session notifications ─────────────────────────────────────────

def notify_signer_turn(
    *,
    to_email: str,
    signer_name: str,
    session_title: str,
    session_id: str,
    signer_index: int,
    total_signers: int,
    creator_name: Optional[str] = None,
) -> bool:
    """Email sent to the signer when it becomes their turn to sign."""
    sign_url = f"{_base()}/ui/multisign.html?session_id={session_id}"
    pos = f"Signer {signer_index + 1} of {total_signers}"
    from_line = f" from {creator_name}" if creator_name else ""

    subject = f"Action required: Please sign '{session_title}'"
    plain = (
        f"Hi {signer_name or 'there'},\n\n"
        f"You have been requested{from_line} to sign the document: {session_title}\n"
        f"Your position: {pos}\n\n"
        f"Sign here: {sign_url}\n\n"
        f"This is an automated message from FillMyPDF. Do not reply to this email."
    )
    html = f"""
<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;color:#111827;max-width:560px;margin:auto;padding:24px">
  <div style="background:#4f46e5;border-radius:12px;padding:24px;color:white;margin-bottom:24px">
    <h1 style="margin:0;font-size:20px">✍️ Signature Requested</h1>
    <p style="margin:8px 0 0;opacity:.85;font-size:14px">FillMyPDF</p>
  </div>
  <p>Hi <strong>{signer_name or 'there'}</strong>,</p>
  <p>You have been asked{from_line} to sign the following document:</p>
  <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:16px 0">
    <p style="margin:0;font-weight:600;font-size:16px">{session_title}</p>
    <p style="margin:6px 0 0;color:#6b7280;font-size:13px">{pos}</p>
  </div>
  <a href="{sign_url}" style="display:inline-block;background:#4f46e5;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin:8px 0">
    Sign Document →
  </a>
  <p style="color:#9ca3af;font-size:12px;margin-top:32px">
    This is an automated message from FillMyPDF. If you did not expect this, please ignore it.
  </p>
</body></html>"""
    return _send(to=to_email, subject=subject, html=html, plain=plain)


def notify_session_complete(
    *,
    to_email: str,
    recipient_name: str,
    session_title: str,
    session_id: str,
    total_signers: int,
) -> bool:
    """Email sent to the session creator when all signers have signed."""
    download_url = f"{_base()}/api/v1/signing-sessions/{session_id}/download"
    sessions_url = f"{_base()}/ui/multisign.html?session_id={session_id}"

    subject = f"All parties have signed: '{session_title}'"
    plain = (
        f"Hi {recipient_name or 'there'},\n\n"
        f"All {total_signers} signer(s) have completed signing '{session_title}'.\n\n"
        f"Download the fully-signed PDF: {download_url}\n"
        f"View session: {sessions_url}\n\n"
        f"FillMyPDF"
    )
    html = f"""
<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;color:#111827;max-width:560px;margin:auto;padding:24px">
  <div style="background:#16a34a;border-radius:12px;padding:24px;color:white;margin-bottom:24px">
    <h1 style="margin:0;font-size:20px">✅ Signing Complete</h1>
    <p style="margin:8px 0 0;opacity:.85;font-size:14px">FillMyPDF</p>
  </div>
  <p>Hi <strong>{recipient_name or 'there'}</strong>,</p>
  <p>All <strong>{total_signers}</strong> signer(s) have completed signing:</p>
  <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:16px 0">
    <p style="margin:0;font-weight:600;font-size:16px">{session_title}</p>
  </div>
  <a href="{download_url}" style="display:inline-block;background:#16a34a;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin:8px 0">
    Download Signed PDF →
  </a>
  <p style="margin-top:12px"><a href="{sessions_url}" style="color:#4f46e5;font-size:14px">View signing session →</a></p>
  <p style="color:#9ca3af;font-size:12px;margin-top:32px">FillMyPDF — automated notification.</p>
</body></html>"""
    return _send(to=to_email, subject=subject, html=html, plain=plain)


def notify_signer_complete_step(
    *,
    to_email: str,
    signer_name: str,
    session_title: str,
    session_id: str,
    step_number: int,
    total_signers: int,
    remaining: int,
) -> bool:
    """Confirmation email to a signer after they successfully sign."""
    sessions_url = f"{_base()}/ui/multisign.html?session_id={session_id}"
    subject = f"Your signature was recorded: '{session_title}'"
    plain = (
        f"Hi {signer_name or 'there'},\n\n"
        f"Your signature (step {step_number} of {total_signers}) has been recorded for '{session_title}'.\n"
        f"{'The document is now complete.' if remaining == 0 else f'{remaining} signer(s) still remaining.'}\n\n"
        f"View session: {sessions_url}\n\nFillMyPDF"
    )
    html = f"""
<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;color:#111827;max-width:560px;margin:auto;padding:24px">
  <div style="background:#0ea5e9;border-radius:12px;padding:24px;color:white;margin-bottom:24px">
    <h1 style="margin:0;font-size:20px">✔ Signature Recorded</h1>
    <p style="margin:8px 0 0;opacity:.85;font-size:14px">FillMyPDF</p>
  </div>
  <p>Hi <strong>{signer_name or 'there'}</strong>,</p>
  <p>Your signature (step <strong>{step_number} of {total_signers}</strong>) has been recorded for:</p>
  <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:16px 0">
    <p style="margin:0;font-weight:600">{session_title}</p>
    <p style="margin:6px 0 0;color:#6b7280;font-size:13px">
      {'✅ All signatures complete' if remaining == 0 else f'{remaining} signer(s) remaining'}
    </p>
  </div>
  <a href="{sessions_url}" style="color:#4f46e5;font-size:14px">View session →</a>
  <p style="color:#9ca3af;font-size:12px;margin-top:32px">FillMyPDF — automated notification.</p>
</body></html>"""
    return _send(to=to_email, subject=subject, html=html, plain=plain)
