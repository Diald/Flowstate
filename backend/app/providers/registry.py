from __future__ import annotations

from .base import LLMProvider
from .gemini import GeminiProvider


class ProviderRegistry:
    """Maps provider identifiers to adapters; new vendors can be registered here."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ValueError(f"Unknown LLM provider '{name}'.") from None

    def names(self) -> list[str]:
        return sorted(self._providers)


providers = ProviderRegistry()
providers.register(GeminiProvider())
