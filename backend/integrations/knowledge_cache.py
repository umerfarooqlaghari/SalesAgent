"""In-memory knowledge/FAQ cache — company blurb + RAG chunks for low-latency voice."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Dict[str, Any]] = {}
_TTL_SECONDS = 30 * 60
_MAX_CHARS = 5000

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
    """Insert baseline FAQ chunks once so voice has something instant to speak from."""
    from backend.agent.rag import upsert_knowledge_chunk

    org_l = (org_name or "").lower()
    tid_l = (tenant_id or "").lower()
    chunks: list[tuple[str, str]] = []
    if "alpha" in org_l or "alpha_devs" in tid_l:
        chunks = list(_ALPHA_DEVS_BASELINE)
    elif org_name:
        chunks = [
            (
                "About",
                f"{org_name} helps customers with products and services. "
                f"Ask what they need and offer to book a follow-up.",
            )
        ]
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
    cursor = db.tenant_knowledge.find({"tenant_id": tenant_id}).limit(40)
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
    logger.info("Warmed knowledge cache for %s (%d chars)", tenant_id, len(text))
    return {"ok": True, "cached": False, "chars": len(text)}
