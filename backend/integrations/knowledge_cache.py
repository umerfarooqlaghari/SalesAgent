"""In-memory knowledge/FAQ cache — company blurb + RAG chunks for low-latency voice."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A05: bounded + evicted, mirroring the tenant-doc cache in registry.py — an
# unbounded dict here grows forever across every tenant ever warmed.
_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CACHE_MAX = 2000
_TTL_SECONDS = 30 * 60
_MAX_CHARS = 5000

# A01: warmup_knowledge is awaited from several concurrent entry points
# (sdr_node, voice fast path, embed/config) with no lock — each racer would
# insert its own baseline/RAG chunks, duplicating them without bound.
_LOCKS: "OrderedDict[str, asyncio.Lock]" = OrderedDict()


def _lock_for(tenant_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[tenant_id] = lock
    _LOCKS.move_to_end(tenant_id)
    while len(_LOCKS) > _CACHE_MAX:
        _LOCKS.popitem(last=False)
    return lock

# Used only when a tenant has zero knowledge chunks (keeps voice FAQ from going empty)
_ALPHA_DEVS_BASELINE = [
    (
        "Services",
        "Alpha Devs builds high-performance digital solutions across AI-Powered ERP, "
        "Computer Vision & SOPs, SaaS platforms, Ed-Tech, and Sales Intelligence.",
    ),
    (
        "Packages",
        "Engagements typically include consultancy, custom product builds, and ongoing "
        "optimization. Share your use case and we recommend the right package.",
    ),
    (
        "Contact",
        "Callers can schedule a discovery call or speak with the team via the website contact form.",
    ),
]


async def _seed_baseline_knowledge(tenant_id: str, org_name: str) -> list[tuple[str, str]]:
    """
    Insert baseline FAQ chunks only for pure service businesses (e.g. Alpha Devs).
    Never seed generic About blurbs for SQL/inventory tenants — that caused repeating scripts.
    """
    from backend.agent.rag import upsert_knowledge_chunk
    from backend.integrations.tenant_inventory import tenant_has_sql_inventory

    if await tenant_has_sql_inventory(tenant_id):
        return []

    # T03: this used to be an unanchored substring test ("alpha" in org_name),
    # so "Alphabet Logistics", "AlphaCare Dental" and "Alpharetta Motors" all
    # matched — and the baseline chunks are PERSISTED to Mongo, then read back
    # into that tenant's system prompt. Exact tenant match only.
    from backend.config import settings

    chunks: list[tuple[str, str]] = []
    if tenant_id == settings.DEFAULT_TENANT_ID:
        chunks = list(_ALPHA_DEVS_BASELINE)
    for title, text in chunks:
        try:
            await upsert_knowledge_chunk(tenant_id, text, title=title, source="baseline_seed")
        except Exception as e:
            logger.warning("Baseline knowledge seed failed for %s: %s", tenant_id, e)
    return chunks


def get_cached_knowledge(tenant_id: str) -> Optional[str]:
    entry = _CACHE.get(tenant_id)
    if not entry:
        return None
    if time.time() > float(entry.get("expires_at", 0)):
        _CACHE.pop(tenant_id, None)
        return None
    _CACHE.move_to_end(tenant_id)
    text = (entry.get("text") or "").strip()
    return text or None


def invalidate_knowledge(tenant_id: str) -> None:
    _CACHE.pop(tenant_id, None)


async def warmup_knowledge(tenant_id: str, force: bool = False) -> Dict[str, Any]:
    """Prefetch company description + knowledge base into memory (fast Mongo reads)."""
    if not force:
        existing = get_cached_knowledge(tenant_id)
        if existing:
            return {"ok": True, "cached": True, "chars": len(existing)}

    async with _lock_for(tenant_id):
        if not force:
            existing = get_cached_knowledge(tenant_id)
            if existing:
                return {"ok": True, "cached": True, "chars": len(existing)}

        from backend.database import get_db
        from backend.tenant.registry import get_tenant_by_id

        ctx = await get_tenant_by_id(tenant_id)
        if not ctx:
            return {"ok": False, "error": "Tenant not found"}

        parts: list[str] = []
        org = ctx.org_name or tenant_id
        if ctx.settings.company_description:
            parts.append(f"About {org}:\n{ctx.settings.company_description.strip()}")

        db = get_db()
        knowledge_count = 0
        # A26: deterministic ordering, otherwise which 40 chunks reach the
        # prompt varies between warmups and answers become non-reproducible.
        cursor = db.tenant_knowledge.find({"tenant_id": tenant_id}).sort("created_at", -1).limit(40)
        async for doc in cursor:
            knowledge_count += 1
            title = (doc.get("title") or "Knowledge").strip()
            text = (doc.get("text") or "").strip()
            if text:
                parts.append(f"[{title}] {text}")

        # Auto-seed baseline FAQ when tenant has no knowledge chunks yet
        if knowledge_count == 0:
            seeded = await _seed_baseline_knowledge(tenant_id, org)
            for title, body in seeded:
                parts.append(f"[{title}] {body}")

        # Prompt-embedded package facts (if tenant still uses a prompt that lists them)
        prompt = (ctx.settings.system_prompt or "").strip()
        if "package" in prompt.lower() or "SaaS" in prompt or "service" in prompt.lower():
            for marker in ("--- PRODUCTS", "--- SERVICES", "packages", "--- COMPANY"):
                idx = prompt.lower().find(marker.lower())
                if idx >= 0:
                    parts.append(prompt[idx : idx + 1200])
                    break

        text = "\n\n".join(parts).strip()
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n…(truncated)"

        if not text:
            return {"ok": False, "error": "No company knowledge configured"}

        _CACHE[tenant_id] = {
            "text": text,
            "expires_at": time.time() + _TTL_SECONDS,
            "fetched_at": time.time(),
            "org_name": org,
        }
        _CACHE.move_to_end(tenant_id)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
        _LOCKS.pop(tenant_id, None)
        logger.info("Warmed knowledge cache for %s (%d chars)", tenant_id, len(text))
        return {"ok": True, "cached": False, "chars": len(text)}
