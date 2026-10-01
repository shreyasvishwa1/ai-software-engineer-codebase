"""Provider-neutral interface for large language models.

Application code depends only on ``LLMProvider``; concrete providers (Claude as the
primary, OpenAI and Ollama as alternatives) implement it later. No provider, SDK or
network access exists here.

Contract for implementations:

- Never log prompts, responses or credentials (prompts may contain repository code).
- Translate vendor-specific failures into ``LLMProviderError`` so callers never handle
  SDK exceptions.
- Perform no network calls at import time.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ChatMessage:
    """One conversation turn. The system prompt is passed separately (``system``)."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProviderError(Exception):
    """Base error for all provider failures."""


class LLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> GenerationResult:
        """Generate a complete response."""

    @abstractmethod
    def generate_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield the response text in pieces as they are produced."""
