"""AnthropicProvider tests: fully offline (stub SDK client, no network, no real key).

The stub records the exact keyword arguments the provider passes to the SDK and returns
real ``anthropic`` 1.11.0 response types, so request construction and response handling
are tested against the installed SDK's own models and exception classes.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage
from pydantic import SecretStr, ValidationError

from app.ai import anthropic_provider
from app.ai.anthropic_provider import (
    API_BASE_URL,
    DECLINED,
    INCOMPLETE,
    UNAVAILABLE,
    AnthropicProvider,
    get_llm_provider,
)
from app.ai.provider import ChatMessage, LLMProviderError
from app.config import Settings, get_settings

TEST_KEY = "test-only-anthropic-key-0000"  # not a real credential
MODEL = "claude-opus-5-5"
MESSAGES = [ChatMessage(role="user", content="Explain <file>x</file>")]
SECRET_MARKERS = ("secret-request-detail", TEST_KEY)


def _message(
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    model: str = MODEL,
    usage: tuple[int, int] = (120, 45),
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        }
    )


class FakeStream:
    def __init__(self, chunks: list[str], final: BetaMessage) -> None:
        self._chunks = chunks
        self._final = final

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            for chunk in self._chunks:
                yield chunk

        return chunks()

    async def get_final_message(self) -> BetaMessage:
        return self._final


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.response = response if response is not None else _message()
        self.error = error
        self.chunks = ["Hel", "lo", "!"]

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(0)  # yield to other tasks (concurrency test)
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(kwargs)
        return self.response

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeStream(self.chunks, self.response)


class FakeClient:
    def __init__(self, messages: FakeMessages) -> None:
        self.beta = type("Beta", (), {"messages": messages})()


def _provider(messages: FakeMessages) -> AnthropicProvider:
    return AnthropicProvider(TEST_KEY, MODEL, client=FakeClient(messages))


@pytest.fixture(autouse=True)
def _fresh_provider_cache() -> Any:
    anthropic_provider.clear_provider_cache()
    yield
    anthropic_provider.clear_provider_cache()


# --- Request construction ----------------------------------------------------------------


async def test_exact_request() -> None:
    messages = FakeMessages()
    await _provider(messages).generate(MESSAGES, system="fixed system", max_tokens=16000)
    assert messages.calls == [
        {
            "model": MODEL,
            "max_tokens": 16000,
            "system": "fixed system",
            "messages": [{"role": "user", "content": "Explain <file>x</file>"}],
            "output_config": {"effort": "medium"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
    ]
    call = messages.calls[0]
    assert "temperature" not in call and "tools" not in call and "tool_choice" not in call


async def test_system_is_omitted_when_not_given() -> None:
    messages = FakeMessages()
    await _provider(messages).generate(MESSAGES, max_tokens=10)
    assert messages.calls[0]["system"] is anthropic.omit


async def test_temperature_is_rejected_without_a_call() -> None:
    messages = FakeMessages()
    with pytest.raises(LLMProviderError):
        await _provider(messages).generate(MESSAGES, max_tokens=10, temperature=0.2)
    with pytest.raises(LLMProviderError):
        async for _ in _provider(messages).generate_stream(
            MESSAGES, max_tokens=10, temperature=0.0
        ):
            pass
    assert messages.calls == [] and messages.stream_calls == []


# --- Response handling -------------------------------------------------------------------


async def test_text_blocks_are_joined_and_other_blocks_ignored() -> None:
    response = _message(
        [
            {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
            {"type": "text", "text": "Part one. "},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "Part two."},
        ],
        model="claude-opus-5-5",
        usage=(321, 54),
    )
    result = await _provider(FakeMessages(response)).generate(MESSAGES, max_tokens=10)
    assert result.text == "Part one. Part two."
    assert "private reasoning" not in result.text
    assert (result.model, result.input_tokens, result.output_tokens) == ("claude-opus-5-5", 321, 54)


async def test_model_reported_by_the_api_is_returned() -> None:
    # e.g. a server-side fallback served the request with another model
    result = await _provider(FakeMessages(_message(model="claude-opus-5"))).generate(
        MESSAGES, max_tokens=10
    )
    assert result.model == "claude-opus-5"


@pytest.mark.parametrize(
    ("stop_reason", "message"), [("refusal", DECLINED), ("max_tokens", INCOMPLETE)]
)
async def test_refusal_and_truncation_are_errors(stop_reason: str, message: str) -> None:
    response = _message([{"type": "text", "text": "partial"}], stop_reason=stop_reason)
    with pytest.raises(LLMProviderError) as excinfo:
        await _provider(FakeMessages(response)).generate(MESSAGES, max_tokens=10)
    assert str(excinfo.value) == message


def _request() -> httpx2.Request:
    return httpx2.Request("POST", f"{API_BASE_URL}/v1/messages", headers={"x-api-key": TEST_KEY})


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=_request(), json={"error": "secret-request-detail"})
    return cls("secret-request-detail", response=response, body={"error": "secret-request-detail"})


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=_request()),
        anthropic.APITimeoutError(request=_request()),
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PermissionDeniedError, 403),
        _status_error(anthropic.BadRequestError, 400),
        _status_error(anthropic.NotFoundError, 404),
        _status_error(anthropic.InternalServerError, 500),
    ],
    ids=["connection", "timeout", "429", "401", "403", "400", "404", "500"],
)
async def test_sdk_errors_become_safe_provider_errors(error: Exception) -> None:
    with pytest.raises(LLMProviderError) as excinfo:
        await _provider(FakeMessages(error=error)).generate(MESSAGES, max_tokens=10)
    assert str(excinfo.value) == UNAVAILABLE
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    rendered = repr(excinfo.value) + str(excinfo.value)
    assert not any(marker in rendered for marker in SECRET_MARKERS)


# --- Streaming ---------------------------------------------------------------------------


async def test_generate_stream_yields_text_with_the_same_request() -> None:
    messages = FakeMessages()
    chunks = [
        c async for c in _provider(messages).generate_stream(MESSAGES, system="s", max_tokens=99)
    ]
    assert chunks == ["Hel", "lo", "!"]
    assert messages.stream_calls == [
        {
            "model": MODEL,
            "max_tokens": 99,
            "system": "s",
            "messages": [{"role": "user", "content": "Explain <file>x</file>"}],
            "output_config": {"effort": "medium"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
    ]


async def test_generate_stream_reports_refusal_and_sdk_errors() -> None:
    refused = FakeMessages(_message(stop_reason="refusal"))
    with pytest.raises(LLMProviderError, match=DECLINED):
        async for _ in _provider(refused).generate_stream(MESSAGES, max_tokens=10):
            pass
    failing = FakeMessages(error=anthropic.APIConnectionError(request=_request()))
    with pytest.raises(LLMProviderError, match=UNAVAILABLE):
        async for _ in _provider(failing).generate_stream(MESSAGES, max_tokens=10):
            pass


# --- Real client construction (no network: constructing a client sends nothing) ---------


def test_real_client_uses_explicit_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token-must-not-be-used")
    provider = AnthropicProvider(TEST_KEY, MODEL)
    client = provider._client
    assert isinstance(client, anthropic.AsyncAnthropic)
    assert str(client.base_url).rstrip("/") == API_BASE_URL == "https://api.anthropic.com"
    assert client.api_key == TEST_KEY
    assert client.auth_token is None  # ambient token ignored
    assert client.max_retries == 2
    assert client.timeout == 60.0


def test_no_key_means_no_provider_and_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)

    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no client may be created without a key")

    monkeypatch.setattr(anthropic_provider, "AsyncAnthropic", no_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key-must-not-enable-the-feature")
    assert get_llm_provider() is None


def test_provider_is_cached_per_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(TEST_KEY))
    first = get_llm_provider()
    assert isinstance(first, AnthropicProvider)
    assert get_llm_provider() is first
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(TEST_KEY + "-rotated"))
    assert get_llm_provider() is not first  # a new key gets a new client


def test_settings_validate_key_and_model() -> None:
    base = {"_env_file": None, "secret_key": "x" * 40}
    assert Settings(**base, anthropic_api_key="").anthropic_api_key is None  # type: ignore[arg-type]
    assert Settings(**base, anthropic_api_key="   ").anthropic_api_key is None  # type: ignore[arg-type]
    configured = Settings(**base, anthropic_api_key=TEST_KEY)  # type: ignore[arg-type]
    assert TEST_KEY not in repr(configured)  # SecretStr
    assert Settings(**base).llm_model == "claude-opus-5-5"  # type: ignore[arg-type]
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            Settings(**base, llm_model=blank)  # type: ignore[arg-type]


# --- Concurrency -------------------------------------------------------------------------


async def test_concurrent_calls_on_one_provider_do_not_interfere() -> None:
    def echo(kwargs: dict[str, Any]) -> BetaMessage:
        content = kwargs["messages"][0]["content"]
        return _message([{"type": "text", "text": f"answer:{content}"}])

    messages = FakeMessages(echo)
    provider = _provider(messages)
    results = await asyncio.gather(
        *(
            provider.generate([ChatMessage(role="user", content=f"q{i}")], max_tokens=10)
            for i in range(25)
        )
    )
    assert [r.text for r in results] == [f"answer:q{i}" for i in range(25)]
    assert len(messages.calls) == 25


async def test_concurrent_dependency_calls_share_one_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(TEST_KEY))
    providers = await asyncio.gather(*(asyncio.to_thread(get_llm_provider) for _ in range(10)))
    assert all(p is providers[0] for p in providers)
