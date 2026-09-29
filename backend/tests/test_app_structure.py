import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "name": "AI Software Engineer",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "health": "/health",
    }


@pytest.mark.parametrize(
    ("path", "has_message"),
    [("/health", False), ("/api/health", True), ("/api/v1/health", True)],
)
def test_health_endpoints(client: TestClient, path: str, has_message: bool) -> None:
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    expected_keys = {"status", "app", "version", "timestamp"} | ({"message"} if has_message else set())
    assert set(body) == expected_keys
    assert body["status"] == "ok"


def test_openapi_generation(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
        "/",
        "/health",
        "/api/health",
        "/api/v1/health",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
    }


def test_v1_router_imports_without_cycles() -> None:
    # A fresh interpreter importing the v1 router before app.main surfaces circular imports.
    code = "import app.api.v1.router, app.main; print(app.api.v1.router.api_router.prefix)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/v1"
