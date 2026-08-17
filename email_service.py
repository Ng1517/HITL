"""
email_service.py
-----------------
Email is sent through a small provider abstraction so that swapping SMTP
for a transactional email API (SendGrid, Resend, Mailgun, ...) means adding
one class, not touching the rest of the codebase.

Credentials are read only from config.settings (i.e. environment variables).
Nothing here ever logs a password, API key, or the raw approval token.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import settings

logger = logging.getLogger("approval_service.email")


class EmailProvider(ABC):
    @abstractmethod
    def send(self, to_email: str, subject: str, html_body: str, text_body: str) -> None:
        ...


class SMTPEmailProvider(EmailProvider):
    def send(self, to_email: str, subject: str, html_body: str, text_body: str) -> None:
        if not settings.smtp_host or not settings.sender_email:
            raise RuntimeError("SMTP_HOST and SENDER_EMAIL must be configured")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.sender_name} <{settings.sender_email}>"
        msg["To"] = to_email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        if settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)

        try:
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(settings.sender_email, [to_email], msg.as_string())
        finally:
            server.quit()


class ConsoleEmailProvider(EmailProvider):
    """Useful for local dev / tests: prints the email instead of sending it."""

    def send(self, to_email: str, subject: str, html_body: str, text_body: str) -> None:
        logger.info("---- [ConsoleEmailProvider] Would send email ----")
        logger.info("To: %s", to_email)
        logger.info("Subject: %s", subject)
        logger.info("Text body:\n%s", text_body)
        logger.info("--------------------------------------------------")


class ResendEmailProvider(EmailProvider):
    """Example of how to plug in an API-based provider instead of SMTP.
    Requires the `resend` package and RESEND_API_KEY to be set."""

    def send(self, to_email: str, subject: str, html_body: str, text_body: str) -> None:
        import resend  # imported lazily so it's not a hard dependency

        if not settings.resend_api_key or not settings.sender_email:
            raise RuntimeError("RESEND_API_KEY and SENDER_EMAIL must be configured")
        resend.api_key = settings.resend_api_key
        resend.Emails.send(
            {
                "from": f"{settings.sender_name} <{settings.sender_email}>",
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            }
        )


def get_email_provider() -> EmailProvider:
    provider = settings.email_provider.lower()
    if provider == "smtp":
        return SMTPEmailProvider()
    if provider == "resend":
        return ResendEmailProvider()
    if provider == "console":
        return ConsoleEmailProvider()
    raise ValueError(
        f"Unknown EMAIL_PROVIDER '{settings.email_provider}'. "
        "Supported out of the box: smtp, resend, console. "
        "Add a new EmailProvider subclass for others (SendGrid, Mailgun, ...)."
    )


def send_approval_email(to_email: str, subject: str, html_body: str, text_body: str) -> None:
    provider = get_email_provider()
    try:
        provider.send(to_email, subject, html_body, text_body)
    except Exception:
        logger.exception("Failed to send approval email to %s", to_email)
        raise
