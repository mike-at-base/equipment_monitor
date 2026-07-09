from __future__ import annotations

import threading
import time
from typing import Any, Callable


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, Any]] = {}


def ttl_get_or_set(key: tuple, ttl_s: int, factory: Callable[[], Any]) -> Any:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            exp, value = hit
            if exp > now:
                return value
            _CACHE.pop(key, None)
    value = factory()
    with _CACHE_LOCK:
        _CACHE[key] = (now + max(1, int(ttl_s)), value)
    return value


def cache_invalidate_prefix(prefix: tuple) -> None:
    with _CACHE_LOCK:
        keys = [k for k in _CACHE if k[: len(prefix)] == prefix]
        for k in keys:
            _CACHE.pop(k, None)
