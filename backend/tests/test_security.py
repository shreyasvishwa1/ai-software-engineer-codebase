import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

PASSWORD = "correct horse battery staple"  # test-only


def _key() -> str:
    return get_settings().secret_key.get_secret_value()


def _token(payload: dict[str, object], key: str | None = None, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, key if key is not None else _key(), algorithm=algorithm)


def _in(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# --- Passwords -------------------------------------------------------------------------


def test_hash_is_not_plaintext_and_is_argon2id() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed != PASSWORD
    assert PASSWORD not in hashed
    assert hashed.startswith("$argon2id$")


def test_correct_password_verifies() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_incorrect_password_fails() -> None:
    assert verify_password("wrong password!", hash_password(PASSWORD)) is False


def test_same_password_produces_different_hashes() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_invalid_hash_returns_false_instead_of_raising() -> None:
    assert verify_password(PASSWORD, "not-an-argon2-hash") is False


# --- JWT -------------------------------------------------------------------------------


def test_valid_token_decodes_to_subject() -> None:
    user_id = uuid.uuid4()
    assert decode_access_token(create_access_token(user_id)) == user_id


def test_token_contains_subject_and_expiration_only() -> None:
    user_id = uuid.uuid4()
    claims = jwt.decode(create_access_token(user_id), _key(), algorithms=["HS256"])
    assert set(claims) == {"sub", "exp"}
    assert claims["sub"] == str(user_id)
    expected_exp = _in(get_settings().jwt_access_token_expire_minutes).timestamp()
    assert abs(claims["exp"] - expected_exp) < 10


def test_expired_token_fails() -> None:
    token = _token({"sub": str(uuid.uuid4()), "exp": _in(-1)})
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "eyJhbGciOiJIUzI1NiJ9.e30"])
def test_malformed_token_fails(token: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_invalid_signature_fails() -> None:
    token = _token({"sub": str(uuid.uuid4()), "exp": _in(5)}, key="a-different-key-" + "x" * 32)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_token_without_subject_fails() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_token({"exp": _in(5)}))


def test_token_without_expiration_fails() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_token({"sub": str(uuid.uuid4())}))


@pytest.mark.parametrize("sub", ["not-a-uuid", "", "12345"])
def test_invalid_uuid_subject_fails(sub: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_token({"sub": sub, "exp": _in(5)}))


def test_unsupported_algorithm_fails() -> None:
    payload = {"sub": str(uuid.uuid4()), "exp": _in(5)}
    for token in (_token(payload, algorithm="HS512"), jwt.encode(payload, None, algorithm="none")):
        with pytest.raises(InvalidTokenError):
            decode_access_token(token)


# --- JWT settings ----------------------------------------------------------------------


def test_jwt_settings_defaults() -> None:
    settings = Settings(_env_file=None, secret_key="x" * 40)  # type: ignore[call-arg]
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_access_token_expire_minutes == 60


@pytest.mark.parametrize(
    "overrides",
    [
        {"jwt_access_token_expire_minutes": 0},
        {"jwt_access_token_expire_minutes": -5},
        {"jwt_algorithm": ""},
        {"jwt_algorithm": "none"},
        {"jwt_algorithm": "RS256"},
    ],
)
def test_invalid_jwt_settings_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key="x" * 40, **overrides)  # type: ignore[arg-type]
