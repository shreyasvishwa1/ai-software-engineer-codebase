"""Password reset by email, without storing reset tokens.

A reset token is a JWT signed with a key derived from SECRET_KEY for this purpose only, so a
reset token is never a valid access token and an access token is never a valid reset token.
It carries the user id, an expiry and a fingerprint of the user's current password hash:
once the password changes, every earlier token stops working, which makes each token
single-use. Tokens travel only in the URL fragment of the reset link (``#token=``), which
browsers do not send to servers. Nothing here logs addresses, tokens or passwords.
"""

import hashlib
import hmac
import logging
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import jwt

from app.config import Settings

logger = logging.getLogger(__name__)

PURPOSE = "password-reset"
ALGORITHM = "HS256"
SMTP_TIMEOUT_SECONDS = 20


class InvalidResetToken(Exception):
    """The reset token is malformed, expired, badly signed or already used."""


def _signing_key(settings: Settings) -> bytes:
    return hmac.new(
        settings.secret_key.get_secret_value().encode(), PURPOSE.encode(), hashlib.sha256
    ).digest()


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:32]


def create_reset_token(settings: Settings, user_id: uuid.UUID, password_hash: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_token_minutes
    )
    payload = {
        "sub": str(user_id),
        "exp": expires_at,
        "purpose": PURPOSE,
        "pwd": password_fingerprint(password_hash),
    }
    return jwt.encode(payload, _signing_key(settings), algorithm=ALGORITHM)


def read_reset_token(settings: Settings, token: str) -> tuple[uuid.UUID, str]:
    """The user id and password fingerprint in a valid token; raises InvalidResetToken."""
    try:
        payload = jwt.decode(
            token,
            _signing_key(settings),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "purpose", "pwd"]},
        )
        if payload["purpose"] != PURPOSE or not isinstance(payload["pwd"], str):
            raise InvalidResetToken
        return uuid.UUID(payload["sub"]), payload["pwd"]
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError, KeyError):
        raise InvalidResetToken from None


def reset_link(settings: Settings, token: str) -> str:
    return f"{settings.app_base_url}/reset-password#token={token}"


def _message(settings: Settings, recipient: str, link: str) -> EmailMessage:
    minutes = settings.password_reset_token_minutes
    message = EmailMessage()
    message["Subject"] = "Reset your AI Software Engineer password"
    message["From"] = f"AI Software Engineer <{settings.smtp_from}>"
    message["To"] = recipient
    message.set_content(
        "Someone asked to reset the password for your AI Software Engineer account.\n\n"
        f"Reset it here (the link works once and expires in {minutes} minutes):\n{link}\n\n"
        "If you didn't ask for this, ignore this email; your password stays the same.\n"
    )
    message.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;background:#0b1020;font-family:Segoe UI,Arial,sans-serif;color:#e2e8f0">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 12px"><tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;background:#111827;border-radius:16px;overflow:hidden;border:1px solid #1f2937">
<tr><td style="padding:24px 28px;background:linear-gradient(135deg,#6366f1,#8b5cf6,#06b6d4);color:#fff;font-size:20px;font-weight:700">AI Software Engineer</td></tr>
<tr><td style="padding:28px">
<p style="margin:0 0 12px;font-size:18px;font-weight:600;color:#fff">Reset your password</p>
<p style="margin:0 0 24px;line-height:1.6;color:#cbd5e1">Someone asked to reset the password for your account. The link works once and expires in {minutes} minutes.</p>
<p style="margin:0 0 24px"><a href="{link}" style="display:inline-block;padding:12px 22px;border-radius:10px;background:#6366f1;color:#fff;text-decoration:none;font-weight:600">Reset password</a></p>
<p style="margin:0;font-size:13px;line-height:1.6;color:#94a3b8">If you didn't ask for this, ignore this email; your password stays the same.</p>
</td></tr></table></td></tr></table></body></html>""",
        subtype="html",
    )
    return message


def send_reset_email(settings: Settings, recipient: str, link: str) -> None:
    """Send the reset email over SMTP with STARTTLS. Failures are logged without details
    that identify the recipient, and never reach the client."""
    if not settings.password_reset_enabled or settings.smtp_password is None:
        return
    try:
        with smtplib.SMTP(
            settings.smtp_host or "", settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
        ) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(settings.smtp_username or "", settings.smtp_password.get_secret_value())
            smtp.send_message(_message(settings, recipient, link))
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Password reset email could not be sent (%s)", type(exc).__name__)
