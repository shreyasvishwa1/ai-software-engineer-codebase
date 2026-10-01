"""LLM provider interface tests (no database, no Redis, no network).

A test-only fake provider exercises the contract; no real provider exists yet.
"""

import dataclasses
import inspect
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.ai.provider import ChatMessage, GenerationResult, LLMProvider, LLMProviderError

BACKEND_DIR = Path(__file__).resolve().parent.parent
FORBIDDEN_MODULES = ("anthropic", "openai", "ollama", "httpx", "requests")


class FakeProvider(LLMProvider):
    """Records each call; returns canned output."""

    def __init__(self, chunks: Sequence[str] = ("Hel", "lo", "!")) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> GenerationResult:
        self.calls.append(
            {
                "method": "generate",
                "messages": messages,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return GenerationResult(
            text="".join(self.chunks), model="fake-model", input_tokens=12, output_tokens=3
        )

    async def generate_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "method": "generate_stream",
                "messages": messages,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        for chunk in self.chunks:
            yield chunk


MESSAGES = [
    ChatMessage(role="user", content="What does app.py do?"),
    ChatMessage(role="assistant", content="It starts the server."),
    ChatMessage(role="user", content="Thanks"),
]


def test_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    class OnlyGenerate(LLMProvider):
        async def generate(
            self,
            messages: Sequence[ChatMessage],
            *,
            system: str | None = None,
            max_tokens: int,
            temperature: float | None = None,
        ) -> GenerationResult:
            return GenerationResult(text="", model="m")

    class OnlyStream(LLMProvider):
        async def generate_stream(
            self,
            messages: Sequence[ChatMessage],
            *,
            system: str | None = None,
            max_tokens: int,
            temperature: float | None = None,
        ) -> AsyncIterator[str]:
            yield ""

    for incomplete in (OnlyGenerate, OnlyStream):
        with pytest.raises(TypeError):
            incomplete()  # type: ignore[abstract]


def test_complete_subclass_can_be_instantiated() -> None:
    provider = FakeProvider()
    assert isinstance(provider, LLMProvider)


async def test_generate_receives_arguments_and_returns_result() -> None:
    provider = FakeProvider()
    result = await provider.generate(
        MESSAGES, system="You are a code assistant.", max_tokens=256, temperature=0.2
    )

    assert provider.calls == [
        {
            "method": "generate",
            "messages": MESSAGES,
            "system": "You are a code assistant.",
            "max_tokens": 256,
            "temperature": 0.2,
        }
    ]
    assert provider.calls[0]["messages"] is MESSAGES  # passed through unchanged
    assert result == GenerationResult(
        text="Hello!", model="fake-model", input_tokens=12, output_tokens=3
    )


async def test_generate_optional_arguments_default_to_none() -> None:
    provider = FakeProvider()
    await provider.generate(MESSAGES, max_tokens=10)
    assert (provider.calls[0]["system"], provider.calls[0]["temperature"]) == (None, None)


async def test_generate_stream_yields_chunks_in_order() -> None:
    provider = FakeProvider(chunks=["def ", "main", "():", " ..."])
    stream = provider.generate_stream(MESSAGES, system="s", max_tokens=64, temperature=None)

    received = [chunk async for chunk in stream]

    assert received == ["def ", "main", "():", " ..."]
    assert "".join(received) == "def main(): ..."
    assert provider.calls[0]["method"] == "generate_stream"
    assert (provider.calls[0]["system"], provider.calls[0]["max_tokens"]) == ("s", 64)


def test_chat_message_is_immutable() -> None:
    message = ChatMessage(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.content = "changed"  # type: ignore[misc]
    assert (message.role, message.content) == ("user", "hi")


def test_generation_result_is_immutable_and_usage_defaults_to_none() -> None:
    result = GenerationResult(text="answer", model="m")
    assert (result.input_tokens, result.output_tokens) == (None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.text = "changed"  # type: ignore[misc]


async def test_provider_errors_are_caught_as_llm_provider_error() -> None:
    class RateLimited(LLMProviderError):
        pass

    class FailingProvider(FakeProvider):
        async def generate(
            self, messages: Sequence[ChatMessage], **kwargs: Any
        ) -> GenerationResult:
            raise RateLimited("provider unavailable")

    assert issubclass(LLMProviderError, Exception)
    with pytest.raises(LLMProviderError, match="provider unavailable"):
        await FailingProvider().generate(MESSAGES, max_tokens=1)


def test_import_loads_no_sdk_or_http_client() -> None:
    code = (
        "import sys, app.ai.provider; "
        f"print(','.join(m for m in {FORBIDDEN_MODULES!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("name", ["generate", "generate_stream"])
def test_method_signatures_do_not_drift(name: str) -> None:
    method = getattr(LLMProvider, name)
    assert getattr(method, "__isabstractmethod__", False)
    params = inspect.signature(method).parameters
    described = [(p.name, p.kind, p.default, p.annotation) for p in params.values()]
    empty = inspect.Parameter.empty
    keyword = inspect.Parameter.KEYWORD_ONLY
    assert described == [
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, empty, empty),
        ("messages", inspect.Parameter.POSITIONAL_OR_KEYWORD, empty, Sequence[ChatMessage]),
        ("system", keyword, None, str | None),
        ("max_tokens", keyword, empty, int),
        ("temperature", keyword, None, float | None),
    ]
    expected_return = GenerationResult if name == "generate" else AsyncIterator[str]
    assert inspect.signature(method).return_annotation == expected_return
    assert inspect.iscoroutinefunction(method) is (name == "generate")
    assert LLMProvider.__abstractmethods__ == frozenset({"generate", "generate_stream"})
