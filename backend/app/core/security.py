"""Password hashing (Argon2) and JWT access tokens.

Nothing in this module logs or returns passwords, hashes' inputs, or tokens.
"""

import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

# argon2id with argon2-cffi's recommended parameters; a random salt is generated per hash.
_password_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    """The access token is missing, malformed, expired, badly signed or has an invalid subject."""


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True only if the password matches; any verification problem returns False."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache
def dummy_password_hash() -> str:
    """A valid hash of a random value, used to keep login timing similar for unknown emails."""
    return _password_hasher.hash(uuid.uuid4().hex)


def create_access_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {"sub": str(user_id), "exp": expires_at}
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> uuid.UUID:
    """Validate the token and return the user id from its ``sub`` claim."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
        raise InvalidTokenError from None
