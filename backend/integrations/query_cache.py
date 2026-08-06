"""
Hybrid Query Cache (Redis + In-Memory Fallback).
Caches POS database lookups and tenant catalog data to minimize database latency.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from backend.config import settings

logger = logging.getLogger(__name__)

# Fallback in-memory cache when Redis is unavailable or unconfigured.
# A05: bounded + evicted on every write, not just lazily on a read of an
# expired key — every query gets memoized here too (even when Redis already
# has it), so with no cap this mirrors the entire cross-tenant keyspace forever.
# Structure: { f"{tenant_id}:{query_key}": {"value": str, "expires_at": float} }
_IN_MEMORY_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_IN_MEMORY_MAX = 5000

_redis_client: Any = None
_redis_lock = asyncio.Lock()
_last_redis_check_time: float = 0
_REDIS_RETRY_INTERVAL: float = 30.0  # Retry connecting every 30s if failed


def normalize_query_key(query: str) -> str:
    """Normalize query strings to maximize cache hits across synonymous caller questions."""
    if not query:
        return ""
    q = query.strip().lower()
    if q.startswith("q:"):
        q = q[2:].strip()
    # Strip common question prefixes to unify cache keys
    prefixes = [
        "what is ", "what are ", "tell me about ", "show me ", "details for ",
        "details on ", "information about ", "info on ", "how does ", "describe ", "explain "
    ]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p):].strip()
            break
    return f"q:{q}"


async def _get_redis_client() -> Any:
    global _redis_client, _last_redis_check_time

    if _redis_client is not None:
        return _redis_client

    # A06: without a lock, N concurrent callers each raced past the None check
    # and built their own client/connection pool; only the last one assigned to
    # the global was ever closed, the rest leaked.
    async with _redis_lock:
        if _redis_client is not None:
            return _redis_client

        now = time.time()
        if now - _last_redis_check_time < _REDIS_RETRY_INTERVAL:
            return None
        _last_redis_check_time = now

        url = (settings.REDIS_URL or "").strip()
        if not url:
            logger.info("REDIS_URL is empty — using in-memory query cache.")
            return None

        client = None
        try:
            import redis.asyncio as redis
            client = redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=3.0,
                socket_timeout=2.0,
            )
            await client.ping()
            _redis_client = client
            logger.info("Connected to Redis for query caching at %s", url)
        except Exception as e:
            logger.warning("Failed to connect to Redis (%s) — falling back to in-memory query cache.", e)
            _redis_client = None
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

        return _redis_client


def _make_key(tenant_id: str, query_key: str) -> str:
    cleaned_key = normalize_query_key(query_key)
    return f"pos_query:{tenant_id}:{cleaned_key}"


async def get_query_cache(tenant_id: str, query_key: str) -> Optional[str]:
    """Retrieve cached response for a tenant query."""
    full_key = _make_key(tenant_id, query_key)

    # 1. Try Redis
    redis_cli = await _get_redis_client()
    if redis_cli:
        try:
            val = await redis_cli.get(full_key)
            if val is not None:
                logger.debug("Redis query cache HIT for key %s", full_key)
                return val
        except Exception as e:
            logger.warning("Redis get error for %s: %s — checking in-memory fallback", full_key, e)

    # 2. Fallback in-memory check
    entry = _IN_MEMORY_CACHE.get(full_key)
    if entry:
        if time.time() <= float(entry.get("expires_at", 0)):
            logger.debug("In-memory query cache HIT for key %s", full_key)
            _IN_MEMORY_CACHE.move_to_end(full_key)
            return str(entry.get("value", ""))
        else:
            _IN_MEMORY_CACHE.pop(full_key, None)

    return None


async def set_query_cache(tenant_id: str, query_key: str, value: str, ttl: int = 900) -> None:
    """Cache a query response for a given tenant with a TTL (default 15 minutes)."""
    full_key = _make_key(tenant_id, query_key)

    # 1. Try Redis
    redis_cli = await _get_redis_client()
    if redis_cli:
        try:
            await redis_cli.setex(full_key, ttl, value)
            logger.debug("Redis query cache SET for key %s (ttl=%ds)", full_key, ttl)
        except Exception as e:
            logger.warning("Redis set error for %s: %s", full_key, e)

    # 2. Always set in-memory cache as secondary fallback
    _IN_MEMORY_CACHE[full_key] = {
        "value": value,
        "expires_at": time.time() + ttl
    }
    _IN_MEMORY_CACHE.move_to_end(full_key)
    while len(_IN_MEMORY_CACHE) > _IN_MEMORY_MAX:
        _IN_MEMORY_CACHE.popitem(last=False)


async def invalidate_tenant_query_cache(tenant_id: str, query_key: Optional[str] = None) -> None:
    """Invalidate cache entries for a given tenant."""
    prefix = f"pos_query:{tenant_id}:"

    # Invalidate in-memory
    keys_to_del = [k for k in _IN_MEMORY_CACHE if k.startswith(prefix)]
    for k in keys_to_del:
        _IN_MEMORY_CACHE.pop(k, None)

    # Invalidate Redis
    redis_cli = await _get_redis_client()
    if redis_cli:
        try:
            if query_key:
                full_key = _make_key(tenant_id, query_key)
                await redis_cli.delete(full_key)
            else:
                # A07: KEYS blocks the single-threaded Redis server for every
                # tenant until it has scanned the whole keyspace. scan_iter
                # cursors through it in batches instead.
                keys = [k async for k in redis_cli.scan_iter(match=f"{prefix}*", count=500)]
                if keys:
                    await redis_cli.delete(*keys)
        except Exception as e:
            logger.warning("Redis cache invalidation failed for tenant %s: %s", tenant_id, e)
