"""
Send appointment confirmation emails through the Gmail API (OAuth2, not SMTP).

Uses the same token.json as calendar_service.py (generate_token.py requests both
Calendar and Gmail send scopes). If credentials are missing, emails are logged
instead of sent so development can continue.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger("chatbot.gmail")


def _load_creds() -> Optional[Credentials]:
    if not config.google_ready():
        return None
    try:
        creds = Credentials.from_authorized_user_file(
            str(config.GOOGLE_TOKEN_FILE), config.GOOGLE_SCOPES
        )
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            config.GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds
    except Exception as exc:
        logger.error("Failed to load Gmail credentials: %s", exc)
        return None


def _gmail_service():
    creds = _load_creds()
    if creds is None:
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_message(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> dict:
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw}


def _send(to: str, subject: str, html_body: str, text_body: str) -> bool:
    service = _gmail_service()
    if service is None:
        logger.warning("Gmail not configured. Would send to %s: %s", to, subject)
        logger.info("Email body:\n%s", text_body)
        return False
    body = _build_message(to=to, subject=subject, html_body=html_body, text_body=text_body)
    try:
        service.users().messages().send(userId="me", body=body).execute()
        logger.info("Sent email to %s (%s)", to, subject)
        return True
    except HttpError as exc:
        logger.error("Gmail send failed for %s: %s", to, exc)
        return False


def _format_when(start_iso: str, end_iso: str) -> str:
    tz = config.TIMEZONE
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    start = start.astimezone(tz)
    end = end.astimezone(tz)
    return (
        f"{start.strftime('%A, %B %d, %Y')} "
        f"{start.strftime('%I:%M %p').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')} "
        f"({config.TIMEZONE_NAME})"
    )


def send_booking_emails(
    *,
    name: str,
    email: str,
    phone: str,
    reason: str,
    start_iso: str,
    end_iso: str,
) -> dict:
    """
    Send two emails: one to the visitor, one to the admin.

    Returns {user_sent: bool, admin_sent: bool}.
    """
    when = _format_when(start_iso, end_iso)
    business = config.BUSINESS_NAME
    phone_line = phone or "Not provided"
    reason_line = reason or "Not specified"

    user_subject = f"Appointment confirmed with {business}"
    user_text = (
        f"Hi {name},\n\n"
        f"Your appointment with {business} is confirmed.\n\n"
        f"When: {when}\n"
        f"Reason / service: {reason_line}\n\n"
        f"If you need to reschedule, please reply to this email.\n\n"
        f"Thank you,\n{business}\n"
    )
    user_html = f"""
    <div style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.5">
      <h2 style="color:#0f766e">Appointment confirmed</h2>
      <p>Hi {name},</p>
      <p>Your appointment with <strong>{business}</strong> is confirmed.</p>
      <table style="border-collapse:collapse">
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">When</td><td>{when}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">Service</td><td>{reason_line}</td></tr>
      </table>
      <p>If you need to reschedule, please reply to this email.</p>
      <p>Thank you,<br>{business}</p>
    </div>
    """

    admin_subject = f"New appointment: {name} -- {when}"
    admin_text = (
        f"A new appointment was booked via the website chatbot.\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone_line}\n"
        f"When: {when}\n"
        f"Reason / service: {reason_line}\n"
    )
    admin_html = f"""
    <div style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.5">
      <h2 style="color:#0f766e">New appointment booked</h2>
      <table style="border-collapse:collapse">
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">Name</td><td>{name}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">Email</td><td>{email}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">Phone</td><td>{phone_line}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">When</td><td>{when}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;font-weight:bold">Service</td><td>{reason_line}</td></tr>
      </table>
    </div>
    """

    user_sent = _send(email, user_subject, user_html, user_text)
    admin_sent = False
    if config.ADMIN_EMAIL:
        admin_sent = _send(config.ADMIN_EMAIL, admin_subject, admin_html, admin_text)
    else:
        logger.warning("ADMIN_EMAIL is not set; skipping admin confirmation.")

    return {"user_sent": user_sent, "admin_sent": admin_sent}
