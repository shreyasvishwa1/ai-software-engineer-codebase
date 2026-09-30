import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.config import get_settings
from app.database import get_db
from app.models import RepositorySourceType, RepositoryStatus, User
from app.schemas.ingestion import IngestionResponse
from app.services.archive import MIB
from app.services.ingestion import IngestionError, ingest_github_repository, ingest_zip_archive

# Every route requires an authenticated user.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["repositories"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

UPLOAD_FIELD = "file"
# Allowance for multipart boundaries and part headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

# The body is parsed manually (after authentication and ownership checks), so it is
# documented here instead of through an UploadFile parameter. It is required only for
# source_type=zip; GitHub repositories are downloaded from their validated source_url.
_OPENAPI_REQUEST_BODY: dict[str, Any] = {
    "description": (
        "source_type=zip: upload the ZIP archive in the 'file' field. "
        "source_type=github: send no body; the public archive for the repository's "
        "source_url (https://github.com/<owner>/<repository>) is downloaded."
    ),
    "requestBody": {
        "required": False,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {
                        UPLOAD_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": "ZIP archive of the repository",
                        }
                    },
                }
            }
        },
    }
}


def _error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


@router.post("/ingest", response_model=IngestionResponse, openapi_extra=_OPENAPI_REQUEST_BODY)
async def ingest_repository(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> IngestionResponse:
    # Ownership and source checks happen before any of the request body is read.
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    if repository.source_type == RepositorySourceType.GITHUB:
        # The archive comes from the repository's validated source_url; no body is read.
        try:
            file_count = await ingest_github_repository(db, repository)
        except IngestionError as exc:
            raise _error(exc.status_code, exc.detail) from None
        return IngestionResponse(
            repository_id=repository_id, status=RepositoryStatus.READY, file_count=file_count
        )

    max_upload_bytes = get_settings().max_upload_size_mb * MIB
    content_length = request.headers.get("content-length")
    if content_length is None:
        raise _error(status.HTTP_411_LENGTH_REQUIRED, "Content-Length header is required")
    if not content_length.isdigit():
        raise _error(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length header")
    if int(content_length) > max_upload_bytes + MULTIPART_OVERHEAD_BYTES:
        raise _error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise _error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload must be multipart/form-data"
        )

    # Exactly one file part and no other fields are accepted.
    form = await request.form(max_files=1, max_fields=0)
    try:
        items = form.multi_items()
        upload = form.get(UPLOAD_FIELD)
        if len(items) != 1 or not isinstance(upload, UploadFile):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Expected a single ZIP file in the '{UPLOAD_FIELD}' field",
            )
        upload.file.seek(0, 2)
        if upload.file.tell() > max_upload_bytes:
            raise _error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit"
            )
        try:
            file_count = await ingest_zip_archive(db, repository, upload.file)
        except IngestionError as exc:
            raise _error(exc.status_code, exc.detail) from None
    finally:
        await form.close()

    return IngestionResponse(
        repository_id=repository_id, status=RepositoryStatus.READY, file_count=file_count
    )
