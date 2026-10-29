"""Chooses the LLM provider for AI features from Settings.

LLM_PROVIDER names one provider explicitly. Left unset, Anthropic is used when
ANTHROPIC_API_KEY is set, otherwise Gemini when GEMINI_API_KEY is set. A chosen provider
without its key means "not configured" (None); there is no silent fallback to the other one.
"""

from app.ai import anthropic_provider, gemini_provider
from app.ai.provider import LLMProvider
from app.config import get_settings


def get_llm_provider() -> LLMProvider | None:
    """FastAPI dependency: the configured provider, or None while none is configured."""
    settings = get_settings()
    choice = settings.llm_provider
    if choice is None:
        choice = "anthropic" if settings.anthropic_api_key is not None else "gemini"
    if choice == "gemini":
        return gemini_provider.get_gemini_provider()
    return anthropic_provider.get_llm_provider()
