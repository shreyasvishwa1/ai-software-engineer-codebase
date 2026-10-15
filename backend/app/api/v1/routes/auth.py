import hmac
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.security import (
    create_access_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.schemas.auth import (
    MIN_PASSWORD_LENGTH,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.password_reset import (
    InvalidResetToken,
    create_reset_token,
    password_fingerprint,
    read_reset_token,
    reset_link,
    send_reset_email,
)
from app.services.rate_limit import (
    RESET_CONFIRM_IP,
    RESET_REQUEST_EMAIL,
    RESET_REQUEST_IP,
    RateLimit,
    client_ip,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

DUPLICATE_ACCOUNT = "An account with this email already exists"
INVALID_CREDENTIALS = "Invalid email or password"
RESET_DISABLED = "Password reset by email is not available"
RESET_REQUESTED = "If an account exists for that email, a reset link is on its way"
INVALID_RESET_LINK = "This reset link is invalid or has expired; request a new one"
RESET_DONE = "Your password has been reset; you can now sign in"


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=INVALID_CREDENTIALS,
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, db: DbSession, request: Request, limiter: RateLimit
) -> User:
    await limiter.check_register_ip(client_ip(request))  # every attempt counts
    password = body.password.get_secret_value()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long",
        )

    if await db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE_ACCOUNT)

    user = User(email=body.email, password_hash=hash_password(password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent registration with the same email hit the unique constraint.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE_ACCOUNT
        ) from None
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, db: DbSession, request: Request, limiter: RateLimit
) -> TokenResponse:
    # Rate limits are checked before any database lookup or password hashing.
    await limiter.check_login_ip(client_ip(request))
    async with limiter.login_attempt(body.email) as attempt:
        password = body.password.get_secret_value()
        user = await db.scalar(select(User).where(User.email == body.email))
        if user is None:
            verify_password(password, dummy_password_hash())  # similar timing for unknown emails
            attempt.failed()
            raise _invalid_credentials()
        if not verify_password(password, user.password_hash):
            attempt.failed()
            raise _invalid_credentials()
        return TokenResponse(access_token=create_access_token(user.id))


@router.post(
    "/password-reset/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    body: PasswordResetRequest,
    db: DbSession,
    request: Request,
    limiter: RateLimit,
    background: BackgroundTasks,
) -> MessageResponse:
    settings = get_settings()
    if not settings.password_reset_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=RESET_DISABLED)
    await limiter.hit(RESET_REQUEST_IP, client_ip(request))
    await limiter.hit(RESET_REQUEST_EMAIL, body.email)
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is not None:
        token = create_reset_token(settings, user.id, user.password_hash)
        # Sent after the response, so known and unknown emails take the same time to answer.
        background.add_task(send_reset_email, settings, user.email, reset_link(settings, token))
    return MessageResponse(detail=RESET_REQUESTED)


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(
    body: PasswordResetConfirm, db: DbSession, request: Request, limiter: RateLimit
) -> MessageResponse:
    await limiter.hit(RESET_CONFIRM_IP, client_ip(request))
    password = body.password.get_secret_value()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long",
        )
    invalid = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_RESET_LINK)
    try:
        user_id, fingerprint = read_reset_token(get_settings(), body.token.get_secret_value())
    except InvalidResetToken:
        raise invalid from None
    user = await db.get(User, user_id)
    if user is None or not hmac.compare_digest(
        fingerprint, password_fingerprint(user.password_hash)
    ):
        raise invalid  # unknown user, or the password already changed (link used)
    user.password_hash = hash_password(password)
    await db.commit()
    return MessageResponse(detail=RESET_DONE)


@router.get("/me", response_model=UserResponse)
async def me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    return current_user
