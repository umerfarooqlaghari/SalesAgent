"""
Voice FAQ fast-path — company FAQ only.

Inventory / catalog questions are tenant-specific (mapped tables may be productions,
sets, SKUs, services, etc.) and must use catalog cache or SQL tools — never a
one-size-fits-all "products" script.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agent.llm import get_chat_llm
from backend.integrations.catalog_cache import get_cached_catalog
from backend.integrations.knowledge_cache import get_cached_knowledge, warmup_knowledge
from backend.integrations.tenant_inventory import (
    is_inventory_question_for_tenant,
    tenant_has_sql_inventory,
)
from backend.tenant.registry import get_tenant_by_id

logger = logging.getLogger(__name__)

_STALL_PHRASES = re.compile(
    r"\b(let me check|one moment|pull that up|checking that|give me (just )?a second|look into)\b",
    re.I,
)

# Company identity FAQ — only when the question is NOT about this tenant's mapped data
_FAQ_KEYWORDS = (
    "who are you",
    "about your company",
    "about the company",
    "what do you do",
    "consultancy",
    "consulting",
)


def _is_pure_company_faq(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(k in text for k in _FAQ_KEYWORDS)


def _spoken_from_knowledge(knowledge: str, user_text: str = "") -> str:
    text = (user_text or "").lower()
    preferred = []
    wants_pkg = any(k in text for k in ("package", "packages", "pricing", "price"))
    wants_svc = any(k in text for k in ("service", "services", "solution", "capabilities"))
    if wants_svc and wants_pkg:
        preferred = ["Services", "Packages", "About", "Product Catalog"]
    elif wants_pkg:
        preferred = ["Packages", "Services", "Product Catalog", "About"]
    elif wants_svc:
        preferred = ["Services", "Packages", "About", "Product Catalog"]
    else:
        preferred = ["Services", "About", "Packages", "Product Catalog"]

    for label in preferred:
        marker = f"[{label}]"
        if marker in knowledge:
            idx = knowledge.index(marker)
            chunk = knowledge[idx + len(marker) : idx + len(marker) + 320].strip()
            nxt = re.search(r"(\n\s*\[|\n\s*---)", chunk)
            if nxt:
                chunk = chunk[: nxt.start()]
            spoken = re.sub(r"\s+", " ", chunk).strip(" -*")
            if spoken:
                return spoken[:240]
    cleaned = re.split(r"\n\s*---", knowledge, maxsplit=1)[0]
    return re.sub(r"\s+", " ", cleaned).strip()[:240]


def _spoken_from_catalog(catalog: str) -> str:
    snippet = re.sub(r"\s+", " ", (catalog or "").strip())
    return snippet[:240] if snippet else ""


async def try_voice_faq_answer(tenant_id: str, user_text: str) -> Optional[str]:
    """
    Fast answers:
    - Mapped SQL / inventory intent → catalog sample (if warm) or None (full agent + tools)
    - Pure company FAQ → knowledge blurb (service businesses without SQL)
    """
    inventory_intent = await is_inventory_question_for_tenant(tenant_id, user_text)
    has_sql = await tenant_has_sql_inventory(tenant_id)

    if inventory_intent:
        catalog = get_cached_catalog(tenant_id)
        if catalog:
            spoken = _spoken_from_catalog(catalog)
            if spoken:
                return f"From our live data: {spoken}"
        # Cold catalog → agent must query this tenant's mapped tables
        return None

    # Vague "services/packages/what do you offer" WITH SQL inventory → prefer live data
    vague_offer = any(
        k in (user_text or "").lower()
        for k in ("service", "services", "package", "packages", "what do you offer", "capabilities", "solutions")
    )
    if has_sql and vague_offer:
        catalog = get_cached_catalog(tenant_id)
        if catalog:
            spoken = _spoken_from_catalog(catalog)
            if spoken:
                return f"From our live data: {spoken}"
        return None

    if not (_is_pure_company_faq(user_text) or (vague_offer and not has_sql)):
        return None

    knowledge = get_cached_knowledge(tenant_id)
    if not knowledge:
        try:
            await warmup_knowledge(tenant_id)
        except Exception:
            logger.debug("voice FAQ knowledge warmup failed", exc_info=True)
        knowledge = get_cached_knowledge(tenant_id)
    if not knowledge:
        return None

    # Don't lock SQL tenants onto a generic About seed
    if has_sql and "[Services]" not in knowledge:
        if knowledge.startswith("About ") or "[About]" in knowledge:
            return None

    text = _spoken_from_knowledge(knowledge, user_text)
    if text and not _STALL_PHRASES.search(text):
        return text

    ctx = await get_tenant_by_id(tenant_id)
    org = (ctx.org_name if ctx else None) or "our company"
    system = (
        f"You are the voice sales assistant for {org}. "
        "Answer ONLY from CACHED KNOWLEDGE below. "
        "Speak 1–2 short sentences. No tools. "
        "NEVER say 'let me check' or invent inventory that isn't listed.\n\n"
        f"--- CACHED KNOWLEDGE ---\n{knowledge}"
    )
    try:
        import asyncio

        llm = get_chat_llm(streaming=False, temperature=0.1, max_retries=0)
        msg = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_text)]),
            timeout=3.0,
        )
        polished = str(getattr(msg, "content", "") or "").strip()
        if polished and not _STALL_PHRASES.search(polished):
            return polished[:280]
    except Exception:
        logger.debug("voice FAQ LLM polish skipped", exc_info=True)

    return text or _spoken_from_knowledge(knowledge, user_text)
