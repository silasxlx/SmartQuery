"""Embedding provider registry used by the task-scoped knowledge boundary.

Stage 2 keeps a deterministic local embedding implementation for the offline
demo.  The registry is still explicit so a later provider can be injected
without changing the RAG or application contracts.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from ..config import EmbeddingConfig

_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalHashEmbeddingProvider:
    name = "local-hash"

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = max(8, int(dimensions))

    def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _TOKEN.findall(text or ""):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / norm for value in vector])
        return result


@dataclass(frozen=True)
class EmbeddingProviderDescriptor:
    name: str
    model: str
    dimensions: int
    api_url: str
    configured: bool


class EmbeddingProviderRegistry:
    def __init__(self, providers: dict[str, EmbeddingProvider], active: str) -> None:
        self.providers = providers
        self.active = active

    @classmethod
    def from_config(cls, config: EmbeddingConfig) -> "EmbeddingProviderRegistry":
        providers: dict[str, EmbeddingProvider] = {}
        for name, provider_config in config.providers.items():
            # External embedding calls are intentionally not made in Stage 2;
            # the local adapter preserves deterministic offline behavior.
            providers[name] = LocalHashEmbeddingProvider(provider_config.dims)
        if not providers:
            providers["default"] = LocalHashEmbeddingProvider()
        return cls(providers, config.active)

    def active_provider(self) -> EmbeddingProvider:
        return self.providers.get(self.active) or next(iter(self.providers.values()))

    def descriptor(self, name: str | None = None) -> EmbeddingProviderDescriptor:
        provider_name = name or self.active
        provider = self.providers[provider_name]
        return EmbeddingProviderDescriptor(
            name=provider_name,
            model=provider_name,
            dimensions=provider.dimensions,
            api_url="",
            configured=True,
        )

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()


__all__ = [
    "EmbeddingProvider",
    "EmbeddingProviderDescriptor",
    "EmbeddingProviderRegistry",
    "LocalHashEmbeddingProvider",
]
