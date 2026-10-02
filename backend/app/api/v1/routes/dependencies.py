import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.dependencies import DependencyInventoryResponse
from app.services.dependencies import dependency_inventory

# Read-only inventory of declared dependencies; requires an authenticated user and goes
# through the existing ownership lookup.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/analysis/dependencies",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=DependencyInventoryResponse)
async def get_dependency_inventory(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> DependencyInventoryResponse:
    # 404 "Repository not found" for missing, wrong-project and other users' repositories.
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    return await dependency_inventory(db, repository)
