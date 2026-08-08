"""
Voice FAQ / catalog speaking helpers.

Never TTS raw SQL dumps (name=Foo). Always natural spoken sentences.
Inventory answers use this tenant's mapped catalog, phrased for a phone call.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agent.llm import get_chat_llm
from backend.integrations.catalog_cache import (get_cached_catalog,
                                                 get_catalog_sections,
                                                 select_catalog_section)
from backend.integrations.knowledge_cache import get_cached_knowledge, warmup_knowledge
from backend.integrations.tenant_inventory import (
    is_inventory_question_for_tenant,
    tenant_has_sql_inventory,
)
from backend.tenant.registry import get_tenant_by_id

logger = logging.getLogger(__name__)

_STALL_PHRASES = re.compile(
    r"\b(let me check|one moment|pull that up|checking that|give me (just )?a second|look into|"
    r"from our live data|name equals|name is equal)\b",
    re.I,
)

# "Who are you" is a question about the ASSISTANT, not about the company. It
# used to fall into the company-FAQ path and get answered with the company
# description, so the agent replied "Alpha Devs is a high-performance software
# engineering firm…" when the caller just wanted to know who they were talking to.
_IDENTITY_PATTERNS = (
    "who are you",
    "who am i talking to",
    "who am i speaking to",
    "what are you",
    "are you a bot",
    "are you a robot",
    "are you human",
    "are you real",
    "are you an ai",
    "am i talking to a human",
    "am i speaking to a real person",
    "your name",
    "what should i call you",
)

_FAQ_KEYWORDS = (
    "about your company",
    "about the company",
    "what do you do",
    "consultancy",
    "consulting",
)


def _is_identity_question(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(p in text for p in _IDENTITY_PATTERNS)


async def _identity_answer(tenant_id: str) -> str:
    ctx = await get_tenant_by_id(tenant_id)
    org = (ctx.org_name if ctx else None) or "this company"
    return (
        f"I'm an AI assistant for {org}. I can answer questions about what we offer, "
        "book appointments, take orders, or put you through to someone on the team. "
        "What can I help you with?"
    )

_NAME_KEYS = (
    "name",
    "title",
    "product_name",
    "set_name",
    "production_name",
    "project_name",
    "item_name",
    "service_name",
    "company",
)


def _is_pure_company_faq(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(k in text for k in _FAQ_KEYWORDS)


def _extract_entity_names(catalog: str, limit: int = 8) -> List[str]:
    """Pull human names from cached SQL text — ignore column labels."""
    names: List[str] = []
    seen = set()
    # name=Mentore  OR  name: Mentore
    key_alt = "|".join(_NAME_KEYS)
    for m in re.finditer(
        rf"\b(?:{key_alt})\s*[=:]\s*([^\n,;|]+)",
        catalog or "",
        flags=re.I,
    ):
        val = m.group(1).strip().strip("\"'")
        # Drop junk / ids-only
        if not val or val.lower() in seen:
            continue
        if re.fullmatch(r"\d+(\.\d+)?", val):
            continue
        if len(val) < 2 or len(val) > 80:
            continue
        seen.add(val.lower())
        names.append(val)
        if len(names) >= limit:
            break

    # Human-readable bullets: "• Mentore (status: Active)" or "• Mentore"
    for line in (catalog or "").splitlines():
        if len(names) >= limit:
            break
        raw = line.strip().lstrip("•-").strip()
        if not raw or raw.startswith("["):
            continue
        # Skip pure key=value dumps already handled above
        # V12: was {2,80?} — not a valid quantifier, so Python matched it as a
        # literal and this branch never fired, killing bullet-format name extraction.
        m = re.match(r"^([^(=\n]{2,80}?)(?:\s*\(|$)", raw)
        if not m:
            continue
        val = m.group(1).strip().strip("\"'")
        if not val or val.lower() in seen:
            continue
        if "=" in val or re.fullmatch(r"\d+(\.\d+)?", val):
            continue
        # Avoid treating section prose as a name
        if val.lower().startswith("table ") or " returned " in val.lower():
            continue
        seen.add(val.lower())
        names.append(val[:80])
    return names


def _section_labels(catalog: str) -> List[str]:
    return re.findall(r"\[([^\]]+)\]", catalog or "")


def _match_mentioned_entity(user_text: str, catalog: str) -> Optional[str]:
    """If caller names a specific catalog item (e.g. after interrupting), return that name."""
    q = (user_text or "").lower()
    if not q.strip():
        return None
    # Prefer longer names first so "Forest Set" beats "Forest"
    names = sorted(_extract_entity_names(catalog, limit=40), key=len, reverse=True)
    for name in names:
        n = name.lower()
        if len(n) < 3:
            continue
        if n in q or all(tok in q for tok in n.split() if len(tok) > 2):
            return name
    return None


def _snippet_for_entity(catalog: str, entity: str) -> str:
    """Grab the catalog line/block that mentions this entity."""
    ent = entity.lower()
    blocks = re.split(r"\n\s*\n", catalog or "")
    for block in blocks:
        if ent in block.lower():
            return block.strip()[:800]
    for line in (catalog or "").splitlines():
        if ent in line.lower():
            return line.strip()[:400]
    return entity


def _natural_catalog_fallback(catalog: str, user_text: str = "") -> str:
    """Deterministic spoken line — no column names, no 'equals'."""
    specific = _match_mentioned_entity(user_text, catalog)
    if specific:
        snippet = _snippet_for_entity(catalog, specific)
        # Pull a couple of extra facts without saying raw keys awkwardly
        extras = []
        for key in ("status", "type", "description", "price", "client"):
            m = re.search(rf"{key}\s*[:=]\s*([^,\n|;]+)", snippet, flags=re.I)
            if m:
                extras.append(m.group(1).strip())
        if extras:
            return f"About {specific}: {', '.join(extras[:2])}."
        return f"Yes — {specific} is in our catalog. Want details or availability next?"

    names = _extract_entity_names(catalog, limit=20)
    sections = _section_labels(catalog)
    if not names:
        return ""

    if len(names) == 1:
        listed = names[0]
    elif len(names) == 2:
        listed = f"{names[0]} and {names[1]}"
    else:
        listed = ", ".join(names[:-1]) + f", and {names[-1]}"

    topic = "items"
    q = (user_text or "").lower()
    for label in sections:
        lw = label.lower()
        if any(k in q for k in (lw, lw.rstrip("s"))):
            topic = label.lower()
            break
    if topic == "items" and sections:
        topic = sections[0].lower()

    return f"We currently have several {topic}, including {listed}."


async def _speak_catalog_naturally(
    tenant_id: str,
    user_text: str,
    catalog: str,
    section: Optional[str] = None,
) -> Optional[str]:
    """LLM polish of catalog into phone-friendly speech; fallback to name list."""
    ctx = await get_tenant_by_id(tenant_id)
    org = (ctx.org_name if ctx else None) or "our company"
    sample = (catalog or "")[:3500]
    names_hint = ", ".join(_extract_entity_names(sample, limit=40))
    specific = _match_mentioned_entity(user_text, sample)
    focus_block = _snippet_for_entity(sample, specific) if specific else sample

    focus_rules = (
        f"- The caller interrupted or asked specifically about **{specific}**. "
        "Answer ONLY about that item in 1–2 sentences. Do not restart a full catalog list.\n"
        if specific
        else
        # Was "mention 3–5 item names", which is why a caller asking about
        # products only ever heard three of them.
        "- If they asked for a list or an overview, name EVERY item in the data below, "
        "not a sample. If there are more than about eight, group them by their category "
        "and name the categories plus the items in each.\n"
        "- If the rows carry a category/type value, use it to organise the answer "
        "(\"we do X and Y under <category>, and A and B under <other category>\").\n"
        "- If they asked about one item, talk only about that item.\n"
    )

    # Name the category explicitly and name the others as off-limits, so the model
    # cannot answer a services question with product names.
    section_rules = ""
    if section:
        others = [s for s in get_catalog_sections(tenant_id) if s not in (section, "all")]
        section_rules = (
            f"- The caller asked about **{section}**. Every item below is a {section}. "
            f"Describe them as {section}.\n"
        )
        if others:
            section_rules += (
                f"- Do NOT mention anything from these other categories: {', '.join(others)}. "
                "They are different offerings and naming them here would be wrong.\n"
            )

    system = (
        f"You are the voice sales assistant for {org} on a phone call.\n"
        "The caller may have interrupted you — answer their LATEST question only.\n"
        "Use ONLY the catalog data below.\n"
        "STRICT RULES:\n"
        "- Speak naturally. Keep it tight, but a complete list may take 2–4 sentences.\n"
        "- NEVER say column names (name, status, id, price, category, etc.).\n"
        "- The bracketed heading (for example [Product catalog]) is an internal table "
        "label. NEVER read it out, and never call an item by it.\n"
        "- NEVER say 'equals', 'name is', 'from live data', or read key=value pairs.\n"
        "- NEVER spell words letter-by-letter.\n"
        f"{focus_rules}"
        f"{section_rules}"
        "- Do not continue a previous list if they changed the subject.\n"
        f"Known item names: {names_hint or '(see catalog)'}\n\n"
        f"--- CATALOG ---\n{focus_block}"
    )
    try:
        llm = get_chat_llm(streaming=False, temperature=0.2, max_retries=0)
        msg = await asyncio.wait_for(
            llm.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user_text)]
            ),
            timeout=3.5,
        )
        text = str(getattr(msg, "content", "") or "").strip()
        if isinstance(getattr(msg, "content", None), list):
            text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in msg.content
            ).strip()
        if text and not _STALL_PHRASES.search(text) and "=" not in text:
            # Strip any accidental key=value fragments
            text = re.sub(r"\b\w+\s*=\s*", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 20:
                return text[:320]
    except Exception:
        logger.debug("catalog speak polish failed", exc_info=True)

    return _natural_catalog_fallback(catalog, user_text) or None


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
            if spoken and "=" not in spoken:
                return spoken[:240]
    cleaned = re.split(r"\n\s*---", knowledge, maxsplit=1)[0]
    return re.sub(r"\s+", " ", cleaned).strip()[:240]


async def try_voice_faq_answer(tenant_id: str, user_text: str) -> Optional[str]:
    """
    Fast spoken answers:
    - Inventory / offer questions with warm catalog → natural speech (never raw SQL)
    - Pure company FAQ → knowledge blurb
    """
    # Identity first: this is about the assistant, not the business.
    if _is_identity_question(user_text):
        return await _identity_answer(tenant_id)

    inventory_intent = await is_inventory_question_for_tenant(tenant_id, user_text)
    has_sql = await tenant_has_sql_inventory(tenant_id)
    vague_offer = any(
        k in (user_text or "").lower()
        for k in (
            "service",
            "services",
            "package",
            "packages",
            "what do you offer",
            "capabilities",
            "solutions",
            "product",
            "products",
        )
    )

    if inventory_intent or (has_sql and vague_offer):
        # Answer from the section that matches the question. Asking about
        # services used to hand the model the whole catalog — broad probe first —
        # so it answered with products.
        section = select_catalog_section(tenant_id, user_text)
        catalog = get_cached_catalog(tenant_id, section=section) if section else None
        if not catalog:
            catalog = get_cached_catalog(tenant_id)
            section = None
        if catalog:
            spoken = await _speak_catalog_naturally(
                tenant_id, user_text, catalog, section=section
            )
            if spoken:
                return spoken
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

    if has_sql and "[Services]" not in knowledge:
        if knowledge.startswith("About ") or "[About]" in knowledge:
            return None

    text = _spoken_from_knowledge(knowledge, user_text)
    if text and not _STALL_PHRASES.search(text) and "=" not in text:
        return text

    ctx = await get_tenant_by_id(tenant_id)
    org = (ctx.org_name if ctx else None) or "our company"
    system = (
        f"You are the voice sales assistant for {org}. "
        "Answer ONLY from CACHED KNOWLEDGE below. "
        "Speak 1–2 short sentences. No tools. "
        "Never say column names or 'equals'.\n\n"
        f"--- CACHED KNOWLEDGE ---\n{knowledge}"
    )
    try:
        llm = get_chat_llm(streaming=False, temperature=0.1, max_retries=0)
        msg = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_text)]),
            timeout=3.0,
        )
        polished = str(getattr(msg, "content", "") or "").strip()
        if polished and not _STALL_PHRASES.search(polished) and "=" not in polished:
            return polished[:280]
    except Exception:
        logger.debug("voice FAQ LLM polish skipped", exc_info=True)

    return text if text and "=" not in text else None
