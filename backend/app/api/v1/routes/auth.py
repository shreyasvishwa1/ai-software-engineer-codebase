from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from app.database import get_db
from app.models import User
from app.schemas.auth import (
    MIN_PASSWORD_LENGTH,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.rate_limit import RateLimit, client_ip

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

DUPLICATE_ACCOUNT = "An account with this email already exists"
INVALID_CREDENTIALS = "Invalid email or password"


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


@router.get("/me", response_model=UserResponse)
async def me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    return current_user
