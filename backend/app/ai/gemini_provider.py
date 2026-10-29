"""Google Gemini implementation of the provider-neutral ``LLMProvider`` interface.

Calls the Gemini API's ``generateContent`` REST endpoint with ``httpx`` (no extra SDK).
Every connection setting is explicit: the destination is always API_BASE_URL, the key
travels only in the ``x-goog-api-key`` header (never in the URL, so it cannot reach access
logs), redirects are not followed, and timeout/retries are fixed. The model id is validated
in Settings because it is part of the request path.

Only a complete answer is returned: a blocked prompt or a candidate stopped for safety or
any other non-``STOP`` reason is "declined", a token-limit stop is "incomplete", and every
transport, status or response-shape failure is "unavailable". Messages are fixed and no
vendor details are chained. Prompts, responses and the key are never logged.
"""

from collections.abc import AsyncIterator, Sequence
import threading
from typing import Any

import httpx

from app.ai.provider import ChatMessage, GenerationResult, LLMProvider, LLMProviderError
from app.config import get_settings

API_BASE_URL = "https://generativelanguage.googleapis.com"
TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2  # connection failures only (httpx transport retries)

UNAVAILABLE = "The AI service is unavailable"
DECLINED = "The AI service declined this request"
INCOMPLETE = "The AI response was incomplete"
TEMPERATURE_UNSUPPORTED = "temperature is not supported by this provider"

_INCOMPLETE_REASONS = frozenset({"MAX_TOKENS", "CONTINUATION"})


class GeminiProvider(LLMProvider):
    def __init__(
        self, api_key: str, model: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        # ``client`` is a test seam only; production always builds the explicit client.
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"x-goog-api-key": api_key},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=MAX_RETRIES),
        )

    def _body(
        self, messages: Sequence[ChatMessage], system: str | None, max_tokens: int
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    @staticmethod
    def _parse(data: Any, fallback_model: str) -> GenerationResult:
        if not isinstance(data, dict):
            raise LLMProviderError(UNAVAILABLE)
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise LLMProviderError(DECLINED)
        candidates = data.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            raise LLMProviderError(UNAVAILABLE)
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if reason in _INCOMPLETE_REASONS:
            raise LLMProviderError(INCOMPLETE)
        if reason != "STOP":
            raise LLMProviderError(DECLINED)
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise LLMProviderError(UNAVAILABLE)
        # Only answer text counts: thought summaries and non-text parts are ignored.
        text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and not part.get("thought")
        )
        usage = data.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        model = data.get("modelVersion")

        def count(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return GenerationResult(
            text=text,
            model=model if isinstance(model, str) and model else fallback_model,
            input_tokens=count("promptTokenCount"),
            output_tokens=count("candidatesTokenCount"),
        )

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> GenerationResult:
        if temperature is not None:
            raise LLMProviderError(TEMPERATURE_UNSUPPORTED)
        try:
            response = await self._client.post(
                f"/v1beta/models/{self._model}:generateContent",
                json=self._body(messages, system, max_tokens),
            )
        except httpx.HTTPError:
            raise LLMProviderError(UNAVAILABLE) from None
        if response.status_code != 200:
            raise LLMProviderError(UNAVAILABLE)
        try:
            data = response.json()
        except ValueError:
            raise LLMProviderError(UNAVAILABLE) from None
        return self._parse(data, self._model)

    async def generate_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        # No caller streams yet; the complete, validated answer is yielded as one piece.
        result = await self.generate(
            messages, system=system, max_tokens=max_tokens, temperature=temperature
        )
        yield result.text


_providers: dict[tuple[str, str], GeminiProvider] = {}
_providers_lock = threading.Lock()


def _cached_provider(api_key: str, model: str) -> GeminiProvider:
    # Sync dependencies run in a thread pool: one provider (one HTTP client) per key/model.
    with _providers_lock:
        provider = _providers.get((api_key, model))
        if provider is None:
            provider = _providers[(api_key, model)] = GeminiProvider(api_key, model)
        return provider


def clear_provider_cache() -> None:
    with _providers_lock:
        _providers.clear()


def get_gemini_provider() -> GeminiProvider | None:
    """The shared Gemini provider, or None while GEMINI_API_KEY is not configured."""
    settings = get_settings()
    if settings.gemini_api_key is None:
        return None
    return _cached_provider(settings.gemini_api_key.get_secret_value(), settings.gemini_model)
