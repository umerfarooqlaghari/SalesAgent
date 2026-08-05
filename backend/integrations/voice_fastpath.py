"""Voice FAQ fast-path — answer services/packages from knowledge cache without LangGraph/tools."""
from __future__ import annotations

import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agent.llm import get_chat_llm
from backend.integrations.knowledge_cache import get_cached_knowledge, warmup_knowledge
from backend.tenant.registry import get_tenant_by_id

logger = logging.getLogger(__name__)

_STALL_PHRASES = re.compile(
    r"\b(let me check|one moment|pull that up|checking that|give me (just )?a second|look into)\b",
    re.I,
)

_FAQ_KEYWORDS = (
    "service", "services", "package", "packages", "pricing", "price", "offer",
    "product", "products", "what do you", "who are you", "about", "capabilities",
    "solutions", "how does", "consultancy", "consulting",
)


def _is_faq_question(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(k in text for k in _FAQ_KEYWORDS)


def _spoken_from_knowledge(knowledge: str) -> str:
    """Deterministic spoken summary — no LLM, no stall phrases."""
    for label in ("Services", "Packages", "Product Catalog", "About"):
        marker = f"[{label}]"
        if marker in knowledge:
            idx = knowledge.index(marker)
            chunk = knowledge[idx + len(marker) : idx + len(marker) + 320].strip()
            # Stop at next section / prompt marker
            nxt = re.search(r"(\n\s*\[|\n\s*---)", chunk)
            if nxt:
                chunk = chunk[: nxt.start()]
            spoken = re.sub(r"\s+", " ", chunk).strip(" -*")
            if spoken:
                return spoken[:240]
    cleaned = re.split(r"\n\s*---", knowledge, maxsplit=1)[0]
    return re.sub(r"\s+", " ", cleaned).strip()[:240]


async def try_voice_faq_answer(tenant_id: str, user_text: str) -> Optional[str]:
    """
    Services/packages FAQ: answer from knowledge cache without LangGraph/tools.
    Prefer a deterministic spoken summary (instant). Optional short LLM polish.
    """
    if not _is_faq_question(user_text):
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

    # Instant path — voice must never wait on Gemini for FAQ
    # (Gemini latency / 429 was dropping Vapi calls after "let me check")
    text = _spoken_from_knowledge(knowledge)
    if text and not _STALL_PHRASES.search(text):
        return text

    ctx = await get_tenant_by_id(tenant_id)
    org = (ctx.org_name if ctx else None) or "our company"
    system = (
        f"You are the voice sales assistant for {org}. "
        "Answer ONLY from CACHED KNOWLEDGE below. "
        "Speak 1–2 short sentences. No tools. "
        "NEVER say 'let me check', 'one moment', or that you need to look something up.\n\n"
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

    return text or _spoken_from_knowledge(knowledge)
