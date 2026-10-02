import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.secret_scan import SecretScanResponse
from app.services.rate_limit import RateLimit
from app.services.secret_scan import SECRET_SCAN_LIMIT, secret_scan

# Read-only scan for likely committed credentials; requires an authenticated user and goes
# through the existing ownership lookup. Rules and limits are fixed server-side.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/analysis/secrets",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=SecretScanResponse)
async def get_secret_scan(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> SecretScanResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(SECRET_SCAN_LIMIT, str(user.id))
    return await secret_scan(db, repository)
