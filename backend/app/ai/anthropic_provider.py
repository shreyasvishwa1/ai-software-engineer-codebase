"""Claude implementation of the provider-neutral ``LLMProvider`` interface.

Uses the official ``anthropic`` SDK (async client). Every connection setting is explicit:
the API key comes from Settings, the destination is always API_BASE_URL (an ambient
ANTHROPIC_BASE_URL can never redirect requests, or the key), and timeout/retries are
fixed. Because a key is passed, the SDK does not look for ambient credentials (environment
variables, profiles or workload identity). Operator-set ANTHROPIC_CUSTOM_HEADERS are left
to the SDK; like the key, prompts, responses and request details they are never logged
or returned.

Requests go through the beta Messages API so that the server-side refusal fallback can be
enabled (``fallbacks="default"`` with the FALLBACK_BETA header). Effort is fixed at
EFFORT; ``temperature`` is not supported. Responses that were declined or cut off by
``max_tokens`` are errors, never partial results. SDK failures become ``LLMProviderError``
with a fixed message and no chained details.
"""

from collections.abc import AsyncIterator, Sequence
import threading
from typing import Any, Final

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaMessageParam, BetaTextBlock

from app.ai.provider import ChatMessage, GenerationResult, LLMProvider, LLMProviderError
from app.config import get_settings

API_BASE_URL = "https://api.anthropic.com"
TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2
EFFORT: Final = "medium"
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
FALLBACKS: Final = "default"

UNAVAILABLE = "The AI service is unavailable"
DECLINED = "The AI service declined this request"
INCOMPLETE = "The AI response was incomplete"
TEMPERATURE_UNSUPPORTED = "temperature is not supported by this provider"


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, *, client: Any | None = None) -> None:
        # ``client`` is a test seam only; production always builds the explicit client.
        self._model = model
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            base_url=API_BASE_URL,
            timeout=TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )

    def _messages(self, messages: Sequence[ChatMessage]) -> list[BetaMessageParam]:
        return [{"role": message.role, "content": message.content} for message in messages]

    @staticmethod
    def _check_stop_reason(stop_reason: str | None) -> None:
        if stop_reason == "refusal":
            raise LLMProviderError(DECLINED)
        if stop_reason == "max_tokens":
            raise LLMProviderError(INCOMPLETE)

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
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system if system is not None else anthropic.omit,
                messages=self._messages(messages),
                output_config={"effort": EFFORT},
                betas=[FALLBACK_BETA],
                fallbacks=FALLBACKS,
            )
        except anthropic.AnthropicError:
            raise LLMProviderError(UNAVAILABLE) from None
        self._check_stop_reason(response.stop_reason)
        # Only text blocks form the answer; thinking and other blocks are ignored.
        text = "".join(block.text for block in response.content if isinstance(block, BetaTextBlock))
        return GenerationResult(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    async def generate_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        if temperature is not None:
            raise LLMProviderError(TEMPERATURE_UNSUPPORTED)
        try:
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                system=system if system is not None else anthropic.omit,
                messages=self._messages(messages),
                output_config={"effort": EFFORT},
                betas=[FALLBACK_BETA],
                fallbacks=FALLBACKS,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except anthropic.AnthropicError:
            raise LLMProviderError(UNAVAILABLE) from None
        self._check_stop_reason(final.stop_reason)


_providers: dict[tuple[str, str], AnthropicProvider] = {}
_providers_lock = threading.Lock()


def _cached_provider(api_key: str, model: str) -> AnthropicProvider:
    # Sync dependencies run in a thread pool: the lock guarantees exactly one provider
    # (one SDK client and connection pool) per key/model, even on concurrent first use.
    with _providers_lock:
        provider = _providers.get((api_key, model))
        if provider is None:
            provider = _providers[(api_key, model)] = AnthropicProvider(api_key, model)
        return provider


def clear_provider_cache() -> None:
    with _providers_lock:
        _providers.clear()


def get_llm_provider() -> LLMProvider | None:
    """FastAPI dependency: the shared provider, or None while no key is configured (no
    client is created then). One provider per key/model is reused across requests; the
    async SDK client is safe for concurrent use."""
    settings = get_settings()
    if settings.anthropic_api_key is None:
        return None
    return _cached_provider(settings.anthropic_api_key.get_secret_value(), settings.llm_model)
