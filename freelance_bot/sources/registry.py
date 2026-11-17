"""Adapter registry used by the scheduler and future marketplace plugins."""

from freelance_bot.sources.base import SourceAdapter


class SourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.platform_id] = adapter

    def get(self, platform_id: str) -> SourceAdapter:
        try:
            return self._adapters[platform_id]
        except KeyError as exc:
            raise KeyError(f"No adapter registered for platform: {platform_id}") from exc

    def platforms(self) -> tuple[str, ...]:
        return tuple(self._adapters)
