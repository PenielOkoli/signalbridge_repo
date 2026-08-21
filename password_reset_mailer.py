"""SMTP delivery for SignalBridge account-recovery links."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote


class PasswordResetDeliveryError(RuntimeError):
    """Raised when a password-reset email cannot be delivered."""


class PasswordResetMailer:
    """Small environment-configured SMTP sender with no browser-side secrets."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "").strip()
        try:
            self.port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
        except ValueError:
            self.port = 0
        self.username = os.getenv("SMTP_USERNAME", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("SMTP_FROM", "").strip()
        self.app_url = os.getenv("SIGNALBRIDGE_APP_URL", "").strip().rstrip("/")
        self.use_ssl = _env_flag("SMTP_USE_SSL")
        self.use_starttls = not self.use_ssl and _env_flag("SMTP_USE_STARTTLS", default=True)

    def configured(self) -> bool:
        return bool(self.host and self.port > 0 and self.sender and self.app_url)

    def send_password_reset(self, recipient: str, token: str) -> None:
        if not self.configured():
            raise PasswordResetDeliveryError("password reset email is not configured")

        reset_url = f"{self.app_url}/reset-password?token={quote(token, safe='')}"
        message = EmailMessage()
        message["Subject"] = "Reset your SignalBridge password"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            "Use the link below to choose a new SignalBridge password. "
            "It expires in one hour and can only be used once.\n\n"
            f"{reset_url}\n\n"
            "If you did not request this, you can safely ignore this email."
        )

        try:
            context = ssl.create_default_context()
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context) as client:
                    self._authenticate_and_send(client, message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                    client.ehlo()
                    if self.use_starttls:
                        client.starttls(context=context)
                        client.ehlo()
                    self._authenticate_and_send(client, message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            raise PasswordResetDeliveryError("password reset email could not be delivered") from exc

    def _authenticate_and_send(self, client: smtplib.SMTP, message: EmailMessage) -> None:
        if self.username:
            if not self.password:
                raise PasswordResetDeliveryError("SMTP_PASSWORD is required when SMTP_USERNAME is set")
            client.login(self.username, self.password)
        client.send_message(message)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}
