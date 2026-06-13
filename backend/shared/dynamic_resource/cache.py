"""动态资源 TTL 缓存。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from .models import ResourceKey, ResourceSnapshot


class DynamicResourceCache:
    """基于 TTL 的轻量缓存，支持按资源类型或名称失效。"""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[tuple[str, str], tuple[float, ResourceSnapshot]] = {}

    async def get_or_load(
        self,
        key: ResourceKey,
        loader: Callable[[], Awaitable[ResourceSnapshot]],
    ) -> ResourceSnapshot:
        """读取缓存；过期或缺失时调用 loader。"""
        cache_key = key.cache_key()
        now = time.monotonic()
        cached = self._items.get(cache_key)
        if cached and now - cached[0] < self._ttl_seconds:
            return cached[1]

        snapshot = await loader()
        self._items[cache_key] = (now, snapshot)
        return snapshot

    def invalidate(self, resource_type: str, resource_name: str | None = None) -> None:
        """主动失效资源缓存。"""
        if resource_name is not None:
            self._items.pop((resource_type, resource_name), None)
            return

        for key in [key for key in self._items if key[0] == resource_type]:
            self._items.pop(key, None)

    def clear(self) -> None:
        """清空所有缓存。"""
        self._items.clear()
