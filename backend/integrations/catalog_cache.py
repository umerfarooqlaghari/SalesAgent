"""In-memory catalog cache — warm on call start so voice turns skip live SQL."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# tenant_id -> cache entry
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCKS: Dict[str, asyncio.Lock] = {}
_TTL_SECONDS = 15 * 60  # 15 minutes
_MAX_CHARS = 6000


def _lock_for(tenant_id: str) -> asyncio.Lock:
    if tenant_id not in _LOCKS:
        _LOCKS[tenant_id] = asyncio.Lock()
    return _LOCKS[tenant_id]


def get_cached_catalog(tenant_id: str) -> Optional[str]:
    entry = _CACHE.get(tenant_id)
    if not entry:
        return None
    if time.time() > float(entry.get("expires_at", 0)):
        _CACHE.pop(tenant_id, None)
        return None
    text = (entry.get("text") or "").strip()
    return text or None


def invalidate_catalog(tenant_id: str) -> None:
    _CACHE.pop(tenant_id, None)
    from backend.integrations.query_cache import invalidate_tenant_query_cache
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(invalidate_tenant_query_cache(tenant_id))
    except Exception:
        pass


async def warmup_catalog(tenant_id: str, force: bool = False) -> Dict[str, Any]:
    """Prefetch mapped inventory tables into memory for low-latency voice answers."""
    if not force:
        existing = get_cached_catalog(tenant_id)
        if existing:
            return {"ok": True, "cached": True, "chars": len(existing)}

    async with _lock_for(tenant_id):
        if not force:
            existing = get_cached_catalog(tenant_id)
            if existing:
                return {"ok": True, "cached": True, "chars": len(existing)}

        from backend.adapters.factory import AdapterFactory
        from backend.tenant.registry import get_tenant_by_id

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            return {"ok": False, "error": "Tenant not found"}

        pos = AdapterFactory.pos(ctx)
        chunks: list[str] = []

        async def _fetch(label: str, query: Optional[str]) -> None:
            try:
                sample = await asyncio.wait_for(pos.list_products(query), timeout=8.0)
                if sample and sample not in chunks:
                    chunks.append(sample if label == "broad" else f"[probe:{label}]\n{sample}")
            except Exception as e:
                logger.warning("Catalog warmup %s failed for %s: %s", label, tenant_id, e)

        await _fetch("broad", None)
        # Parallel probes for mapped categories — pre-warm low-latency memory
        await asyncio.gather(
            _fetch("services", "services"),
            _fetch("products", "products"),
            _fetch("packages", "packages"),
            _fetch("blog", "blog posts"),
            _fetch("faqs", "faqs"),
        )

        text = "\n\n".join(chunks).strip()
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n…(truncated)"

        if not text:
            return {"ok": False, "error": "No catalog data returned from inventory sources"}

        _CACHE[tenant_id] = {
            "text": text,
            "expires_at": time.time() + _TTL_SECONDS,
            "fetched_at": time.time(),
            "org_name": ctx.org_name,
        }
        logger.info(
            "Warmed catalog cache for tenant %s (%d chars, ttl=%ds)",
            tenant_id,
            len(text),
            _TTL_SECONDS,
        )
        return {"ok": True, "cached": False, "chars": len(text)}


def schedule_warmup(tenant_id: str, force: bool = False) -> None:
    """Fire-and-forget warmup (safe to call from request handlers)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(warmup_catalog(tenant_id, force=force))
    except RuntimeError:
        logger.debug("No running loop — skipping background warmup for %s", tenant_id)
