"""
Section-aware, per-tenant catalog cache.

Warmed on call start so voice turns skip live SQL.

Why sections: the previous version stored one flat blob (a broad probe followed
by five hardcoded probes: services/products/packages/blog/faqs) and handed the
first ~3500 characters of it to the LLM for every question. A tenant that sells
both services and products therefore heard about products when they asked about
services, because the broad probe came first. Probes are now driven by the
tenant's OWN mapped table labels and stored per section, and callers ask for the
section that matches the question.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A05: bounded + evicted on every write, not just lazily on a read of an
# expired entry — an unbounded per-tenant cache dict on a multi-tenant
# process is a slow memory leak that only shows up in production traffic.
_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_LOCKS: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
_CACHE_MAX = 2000
_TTL_SECONDS = 15 * 60  # 15 minutes
_MAX_CHARS = 6000
_MAX_SECTION_CHARS = 3000
_MAX_PROBES = 8

# Only used when a tenant has no mapped tables to probe.
_FALLBACK_PROBES: Tuple[Tuple[str, Optional[str]], ...] = (
    ("services", "services"),
    ("products", "products"),
    ("packages", "packages"),
)

# An adapter reports failure as a *string*, not an exception. Caching one would
# pin a SQLAlchemy error (possibly containing the connection URL) into the system
# prompt for the full TTL, under a header telling the model to prefer it. (A03)
_ERROR_MARKERS = (
    "query error",
    "inventory query failed",
    "no matching records found",
    "no products found",
    "no inventory source is connected",
)


def _looks_like_error(sample: str) -> bool:
    low = (sample or "").strip().lower()
    if not low:
        return True
    return any(marker in low for marker in _ERROR_MARKERS)


def _lock_for(tenant_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[tenant_id] = lock
    _LOCKS.move_to_end(tenant_id)
    while len(_LOCKS) > _CACHE_MAX:
        _LOCKS.popitem(last=False)
    return lock


def _live_entry(tenant_id: str) -> Optional[Dict[str, Any]]:
    entry = _CACHE.get(tenant_id)
    if not entry:
        return None
    if time.time() > float(entry.get("expires_at", 0)):
        _CACHE.pop(tenant_id, None)
        return None
    return entry


def get_catalog_sections(tenant_id: str) -> Dict[str, str]:
    """{section label -> catalog text} for this tenant, or {} when cold."""
    entry = _live_entry(tenant_id)
    return dict(entry.get("sections") or {}) if entry else {}


def get_cached_catalog(tenant_id: str, section: Optional[str] = None) -> Optional[str]:
    """
    Cached catalog text.

    `section=None` returns everything (back-compatible). Passing a section label
    returns just that slice, which is what voice answers should use.
    """
    entry = _live_entry(tenant_id)
    if not entry:
        return None
    if section:
        text = ((entry.get("sections") or {}).get(section) or "").strip()
        return text or None
    text = (entry.get("text") or "").strip()
    return text or None


def _tokens(text: str) -> set:
    import re

    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


def select_catalog_section(tenant_id: str, user_text: str) -> Optional[str]:
    """
    Pick the section whose label best matches the question.

    Scored against the tenant's own labels, so "what services do you offer"
    resolves to their services table rather than whatever the broad probe
    happened to return first. Returns None when nothing matches, and the caller
    falls back to the combined text.
    """
    sections = get_catalog_sections(tenant_id)
    if not sections:
        return None

    q = _tokens(user_text)
    if not q:
        return None

    best, best_score = None, 0
    for label in sections:
        if label == "all":
            continue
        label_tokens = _tokens(label)
        # singular/plural tolerance: "service" should match a "services" section
        expanded = set(label_tokens)
        for t in label_tokens:
            expanded.add(t[:-1] if t.endswith("s") else t + "s")
        score = len(q & expanded)
        if score > best_score:
            best, best_score = label, score
    return best


def invalidate_catalog(tenant_id: str) -> None:
    _CACHE.pop(tenant_id, None)
    from backend.integrations.query_cache import invalidate_tenant_query_cache

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(invalidate_tenant_query_cache(tenant_id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set = set()


async def _probe_plan(tenant_id: str) -> List[Tuple[str, Optional[str]]]:
    """
    (section_label, query) pairs to warm, derived from the tenant's mapped tables.

    Falls back to a small generic set only when nothing is mapped.
    """
    from backend.integrations.tenant_inventory import load_inventory_mappings

    plan: List[Tuple[str, Optional[str]]] = [("all", None)]
    seen = {"all"}
    try:
        # max_time_ms does not cover Mongo *server selection*, so an unreachable
        # replica set would block here for the full 30s selection timeout while
        # holding this tenant's warmup lock.
        mapped = await asyncio.wait_for(load_inventory_mappings(tenant_id), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning("Probe plan for %s timed out loading mappings — using fallback", tenant_id)
        mapped = []
    except Exception as e:
        logger.warning("Could not load mappings for probe plan (%s): %s", tenant_id, e)
        mapped = []

    for m in mapped:
        label = (m.get("label") or m.get("role") or m.get("table") or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        plan.append((key, label))
        if len(plan) >= _MAX_PROBES:
            return plan

    if len(plan) == 1:
        for label, query in _FALLBACK_PROBES:
            if label not in seen:
                plan.append((label, query))
    return plan


async def warmup_catalog(tenant_id: str, force: bool = False) -> Dict[str, Any]:
    """Prefetch this tenant's mapped tables into memory for low-latency voice answers."""
    if not force:
        existing = get_cached_catalog(tenant_id)
        if existing:
            return {"ok": True, "cached": True, "chars": len(existing)}

    async with _lock_for(tenant_id):
        entry = _live_entry(tenant_id)
        if entry:
            # A27: a burst of force=True callers would otherwise each run the full
            # probe set back to back while holding the lock.
            if not force or (time.time() - float(entry.get("fetched_at", 0))) < 5.0:
                text = entry.get("text") or ""
                return {"ok": True, "cached": True, "chars": len(text)}

        from backend.adapters.factory import AdapterFactory
        from backend.tenant.registry import get_tenant_by_id

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            return {"ok": False, "error": "Tenant not found"}

        pos = AdapterFactory.pos(ctx)
        sections: Dict[str, str] = {}

        async def _fetch(label: str, query: Optional[str]) -> None:
            try:
                sample = await asyncio.wait_for(pos.list_products(query), timeout=8.0)
            except Exception as e:
                logger.warning("Catalog probe %s failed for %s: %s", label, tenant_id, e)
                return
            if _looks_like_error(sample):
                logger.info("Catalog probe %s for %s returned no usable rows", label, tenant_id)
                return
            text = sample.strip()
            if len(text) > _MAX_SECTION_CHARS:
                text = text[:_MAX_SECTION_CHARS] + "\n…(truncated)"
            if text not in sections.values():
                sections[label] = text

        plan = await _probe_plan(tenant_id)
        await asyncio.gather(*(_fetch(label, query) for label, query in plan))

        if not sections:
            return {"ok": False, "error": "No catalog data returned from inventory sources"}

        parts = []
        for label, body in sections.items():
            parts.append(body if label == "all" else f"[{label}]\n{body}")
        text = "\n\n".join(parts).strip()
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n…(truncated)"

        _CACHE[tenant_id] = {
            "sections": sections,
            "text": text,
            "expires_at": time.time() + _TTL_SECONDS,
            "fetched_at": time.time(),
            "org_name": ctx.org_name,
        }
        _CACHE.move_to_end(tenant_id)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        logger.info(
            "Warmed catalog for %s: %d sections (%s), %d chars",
            tenant_id, len(sections), ", ".join(sections), len(text),
        )
        return {"ok": True, "cached": False, "chars": len(text), "sections": list(sections)}


def schedule_warmup(tenant_id: str, force: bool = False) -> None:
    """Fire-and-forget warmup (safe to call from request handlers)."""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(warmup_catalog(tenant_id, force=force))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    except RuntimeError:
        logger.debug("No running loop — skipping background warmup for %s", tenant_id)
