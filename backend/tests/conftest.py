import os

# Test-only configuration, set before the app (and its settings) is imported.
# Environment variables take precedence over backend/.env, so tests do not
# depend on a developer's local values. This key is not a real secret.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-real-use-000000")
os.environ.setdefault("API_PREFIX", "/api")
# Placeholder URL without credentials; the engine is lazy, so unit tests never connect.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/ai_se_unit_tests")
