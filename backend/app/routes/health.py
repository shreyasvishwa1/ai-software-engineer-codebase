from fastapi import APIRouter
from datetime import datetime, timezone
from app.config import get_settings

router = APIRouter(tags=["health"])
settings = get_settings()

@router.get("/health")
def health_check():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/api/health")
def api_health_check():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Backend is connected",
    }
