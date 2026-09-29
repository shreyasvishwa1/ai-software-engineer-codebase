from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidTokenError, decode_access_token
from app.database import get_db
from app.models import User

# auto_error=False so every failure below returns the same 401 response.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the user from an ``Authorization: Bearer <token>`` header."""
    if credentials is None:
        raise _unauthorized()
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _unauthorized() from None
    user = await db.get(User, user_id)
    if user is None:
        raise _unauthorized()
    return user
