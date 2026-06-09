"""Minimal SMTP sender for notification emails. Configuration comes from the
environment (see config.py). A no-op when SMTP isn't configured, so the app runs
perfectly well without email set up."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..config import get_settings


def send_email(to: list[str], subject: str, body: str) -> bool:
    """Send a plain-text email. Returns False (without raising) if email is not
    configured or there are no recipients."""
    settings = get_settings()
    if not settings.smtp_configured or not to:
        return False

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)

    if settings.smtp_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, context=context, timeout=20
        ) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            if settings.smtp_tls:
                s.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    return True
