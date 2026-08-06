import pathlib, sys
ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
def edit(path, subs):
    p = ROOT / path; src = p.read_text()
    for i,(old,new) in enumerate(subs):
        n = src.count(old)
        assert n == 1, f"{path} anchor #{i} matched {n}x:\n{old[:200]}"
        src = src.replace(old, new)
    p.write_text(src); print(f"  patched {path} ({len(subs)} edits)")
def replace_block(path, start_marker, end_marker, new_text):
    p = ROOT / path; src = p.read_text()
    i, j = src.index(start_marker), src.index(end_marker)
    assert i < j, path
    p.write_text(src[:i] + new_text + src[j:]); print(f"  block-replaced in {path}")

# ===== batch3a_catalog.py =====

print("FIX B — section-aware catalog (services must not answer with products)")

replace_block('backend/integrations/catalog_cache.py',
'"""In-memory catalog cache',
'def schedule_warmup(',
'''"""
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
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# tenant_id -> cache entry
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCKS: Dict[str, asyncio.Lock] = {}
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
    if tenant_id not in _LOCKS:
        _LOCKS[tenant_id] = asyncio.Lock()
    return _LOCKS[tenant_id]


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
        mapped = await load_inventory_mappings(tenant_id)
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
                text = text[:_MAX_SECTION_CHARS] + "\\n…(truncated)"
            if text not in sections.values():
                sections[label] = text

        plan = await _probe_plan(tenant_id)
        await asyncio.gather(*(_fetch(label, query) for label, query in plan))

        if not sections:
            return {"ok": False, "error": "No catalog data returned from inventory sources"}

        parts = []
        for label, body in sections.items():
            parts.append(body if label == "all" else f"[{label}]\\n{body}")
        text = "\\n\\n".join(parts).strip()
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\\n…(truncated)"

        _CACHE[tenant_id] = {
            "sections": sections,
            "text": text,
            "expires_at": time.time() + _TTL_SECONDS,
            "fetched_at": time.time(),
            "org_name": ctx.org_name,
        }
        logger.info(
            "Warmed catalog for %s: %d sections (%s), %d chars",
            tenant_id, len(sections), ", ".join(sections), len(text),
        )
        return {"ok": True, "cached": False, "chars": len(text), "sections": list(sections)}


''')
print("catalog_cache rewritten")

# ===== batch3b_wiring.py =====

print("FIX B wiring + FIX A (timeout fallback)")

# ---------------- voice fast path: speak the RIGHT section ----------------
edit('backend/integrations/voice_fastpath.py', [
("""from backend.integrations.catalog_cache import get_cached_catalog""",
 """from backend.integrations.catalog_cache import (get_cached_catalog,
                                                 get_catalog_sections,
                                                 select_catalog_section)"""),

('''    if inventory_intent or (has_sql and vague_offer):
        catalog = get_cached_catalog(tenant_id)
        if catalog:
            spoken = await _speak_catalog_naturally(tenant_id, user_text, catalog)
            if spoken:
                return spoken
        return None''',
'''    if inventory_intent or (has_sql and vague_offer):
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
        return None'''),

('''async def _speak_catalog_naturally(
    tenant_id: str,
    user_text: str,
    catalog: str,
) -> Optional[str]:''',
'''async def _speak_catalog_naturally(
    tenant_id: str,
    user_text: str,
    catalog: str,
    section: Optional[str] = None,
) -> Optional[str]:'''),

('''    focus_rules = (
        f"- The caller interrupted or asked specifically about **{specific}**. "
        "Answer ONLY about that item in 1–2 sentences. Do not restart a full catalog list.\\n"
        if specific
        else
        "- If they asked for a list/overview, mention 3–5 item names. "
        "If they asked about one item, talk only about that item.\\n"
    )''',
'''    focus_rules = (
        f"- The caller interrupted or asked specifically about **{specific}**. "
        "Answer ONLY about that item in 1–2 sentences. Do not restart a full catalog list.\\n"
        if specific
        else
        "- If they asked for a list/overview, mention 3–5 item names. "
        "If they asked about one item, talk only about that item.\\n"
    )

    # Name the category explicitly and name the others as off-limits, so the model
    # cannot answer a services question with product names.
    section_rules = ""
    if section:
        others = [s for s in get_catalog_sections(tenant_id) if s not in (section, "all")]
        section_rules = (
            f"- The caller asked about **{section}**. Every item below is a {section}. "
            f"Describe them as {section}.\\n"
        )
        if others:
            section_rules += (
                f"- Do NOT mention anything from these other categories: {', '.join(others)}. "
                "They are different offerings and naming them here would be wrong.\\n"
            )'''),

('''        f"{focus_rules}"
        "- Do not continue a previous list if they changed the subject.\\n"''',
 '''        f"{focus_rules}"
        f"{section_rules}"
        "- Do not continue a previous list if they changed the subject.\\n"'''),
])

# ---------------- sdr_node: inject only the relevant section ----------------
edit('backend/agent/graph.py', [
("""    from backend.integrations.catalog_cache import get_cached_catalog, schedule_warmup""",
 """    from backend.integrations.catalog_cache import (get_cached_catalog,
                                                    get_catalog_sections,
                                                    schedule_warmup,
                                                    select_catalog_section)"""),

('''    catalog = get_cached_catalog(tenant_id)
    if catalog:
        system_prompt += (
            "\\n\\n--- CACHED CATALOG (this tenant's approved SQL tables — prefer over tools) ---\\n"
            + catalog
        )
    elif not is_voice:
        schedule_warmup(tenant_id)''',
'''    # Inject the matching section only. Handing the model every category at once
    # is why a services question came back with product names.
    catalog_section = select_catalog_section(tenant_id, user_text)
    catalog = get_cached_catalog(tenant_id, section=catalog_section) if catalog_section else None
    if not catalog:
        catalog = get_cached_catalog(tenant_id)
        catalog_section = None

    if catalog:
        if catalog_section:
            others = [s for s in get_catalog_sections(tenant_id)
                      if s not in (catalog_section, "all")]
            header = (
                f"\\n\\n--- CACHED CATALOG · {catalog_section.upper()} "
                "(this tenant's approved SQL tables — prefer over tools) ---\\n"
            )
            system_prompt += header + catalog
            if others:
                system_prompt += (
                    f"\\n\\nEvery row above is a {catalog_section}. This tenant also has separate "
                    f"{', '.join(others)} — those are DIFFERENT offerings. Never answer a "
                    f"{catalog_section} question with items from them; call query_pos_database "
                    "if the caller asks about another category."
                )
        else:
            system_prompt += (
                "\\n\\n--- CACHED CATALOG (this tenant's approved SQL tables — prefer over tools) ---\\n"
                + catalog
            )
    elif not is_voice:
        schedule_warmup(tenant_id)'''),
])

# ---------------- FIX A: the timeout fallback must not recite the blurb ----------------
edit('backend/main.py', [
('''    async def _timeout_fallback() -> str:
        """
        V09: this must never name a specific industry. The previous hardcoded
        "AI ERP, computer vision, SaaS, ed-tech..." line was Alpha's service list
        and was being spoken to every tenant whose knowledge cache was cold.
        """
        knowledge = get_cached_knowledge(tenant_id) or ""
        if knowledge:
            snippet = " ".join(knowledge.split())[:220]
            return f"Here's a quick overview: {snippet}"
        try:
            ctx = await get_tenant_by_id(tenant_id)
            org = (ctx.org_name if ctx else None) or "our team"
        except Exception:
            org = "our team"
        return (
            "Let me make sure I get that exactly right for you. "
            f"Would you like someone from {org} to follow up, or can I help with something else?"
        )''',
'''    async def _timeout_fallback() -> str:
        """
        Spoken when a turn misses its deadline.

        This must NOT read out the company blurb. Doing so made the agent appear
        to "break" mid-booking: the caller gave a date and time, the turn ran long,
        and instead of confirming the appointment the agent started describing what
        the company does. A timeout is a *stall*, so the reply has to keep the
        caller in whatever they were already doing.

        It must also never name a specific industry (V09) — the old hardcoded
        "AI ERP, computer vision, SaaS, ed-tech" line was one tenant's service
        list being spoken to all of them.
        """
        return (
            "Sorry, I didn't quite catch that — could you say that one more time?"
        )''',),

# empty-response guard should not reach for the blurb either
('''        if not (assistant_msg or "").strip():
            assistant_msg = await _timeout_fallback()

        # Strip stall phrases if the model still produced one
        low = assistant_msg.lower()
        if "let me check" in low or "one moment" in low or "pull that up" in low:
            assistant_msg = await _timeout_fallback()''',
'''        if not (assistant_msg or "").strip():
            assistant_msg = await _timeout_fallback()

        # Strip stall phrases if the model still produced one. Never swap in the
        # company blurb here — mid-booking that reads as the agent losing the plot.
        low = assistant_msg.lower()
        if "let me check" in low or "one moment" in low or "pull that up" in low:
            assistant_msg = await _timeout_fallback()'''),
])
print("wiring applied")

# ===== batch3c_latency.py =====

print("LATENCY — P01, P02, P04, P07, P08, P09..P12 (pulled forward: this is what trips the deadline)")

# ---------------- P01: cache get_tenant_by_id ----------------
edit('backend/tenant/registry.py', [
('''async def get_tenant_by_id(tenant_id: str) -> Optional[TenantContext]:
    db = get_db()
    doc = await db.tenants.find_one({"tenant_id": tenant_id, "status": "active"})
    if not doc:
        return None
    return TenantContext.from_document(doc)''',
'''# P01: this used to be an uncached find_one for the FULL tenant document — the
# entire system prompt plus every integration config — and it is called 6+ times
# per voice turn (voice greeting, sdr_node, get_tenant_system_prompt, once per
# tool via _load_tenant_context, twice in the voice fast path). At Atlas latency
# that alone accounted for several hundred ms of every spoken turn.
_TENANT_CACHE: "OrderedDict[str, tuple[float, Optional[TenantContext]]]" = OrderedDict()
_TENANT_CACHE_TTL = 60.0
_TENANT_CACHE_MAX = 2000
_TENANT_LOCKS: Dict[str, asyncio.Lock] = {}


def invalidate_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Call after any write to a tenant document."""
    if tenant_id is None:
        _TENANT_CACHE.clear()
    else:
        _TENANT_CACHE.pop(tenant_id, None)


def _cached_tenant(tenant_id: str) -> Optional[TenantContext]:
    hit = _TENANT_CACHE.get(tenant_id)
    if not hit:
        return None
    expires_at, ctx = hit
    if time.monotonic() > expires_at:
        _TENANT_CACHE.pop(tenant_id, None)
        return None
    _TENANT_CACHE.move_to_end(tenant_id)
    return ctx


async def get_tenant_by_id(tenant_id: str) -> Optional[TenantContext]:
    if not tenant_id:
        return None

    cached = _cached_tenant(tenant_id)
    if cached is not None:
        return cached

    lock = _TENANT_LOCKS.setdefault(tenant_id, asyncio.Lock())
    async with lock:
        # Double-check: a concurrent turn may have populated it while we queued.
        cached = _cached_tenant(tenant_id)
        if cached is not None:
            return cached

        db = get_db()
        doc = await db.tenants.find_one(
            {"tenant_id": tenant_id, "status": "active"}, max_time_ms=2000
        )
        ctx = TenantContext.from_document(doc) if doc else None
        if ctx is not None:
            _TENANT_CACHE[tenant_id] = (time.monotonic() + _TENANT_CACHE_TTL, ctx)
            _TENANT_CACHE.move_to_end(tenant_id)
            while len(_TENANT_CACHE) > _TENANT_CACHE_MAX:
                _TENANT_CACHE.popitem(last=False)
        _TENANT_LOCKS.pop(tenant_id, None)
        return ctx'''),
])

# ---------------- P12: no Mongo WRITE on the hot path ----------------
edit('backend/tenant/registry.py', [
('''        if is_alpha_default_prompt(prompt):
            desc = ctx.settings.company_description or ""
            prompt = build_tenant_system_prompt(ctx.org_name, desc)
            db = get_db()
            await db.tenants.update_one(
                {"tenant_id": tenant_id},
                {"$set": {"settings.system_prompt": prompt}},
            )
            logger.info("Auto-fixed stale Alpha prompt for tenant %s", tenant_id)''',
'''        if is_alpha_default_prompt(prompt):
            desc = ctx.settings.company_description or ""
            prompt = build_tenant_system_prompt(ctx.org_name, desc)
            # P12: the repair used to be written back inline, putting a Mongo
            # update on every spoken turn until it succeeded. The startup
            # migration (migrate_stale_tenant_prompts) owns persistence; here we
            # just use the corrected prompt for this turn.
            logger.info("Using repaired prompt for tenant %s (persisted at startup)", tenant_id)'''),
])

# imports for the cache
edit('backend/tenant/registry.py', [
("""import logging""", """import asyncio
import logging
import time
from collections import OrderedDict"""),
])

# ---------------- P02: cache the inventory mappings ----------------
edit('backend/integrations/tenant_inventory.py', [
('''async def load_inventory_mappings(tenant_id: str) -> List[Dict[str, Any]]:
    """Return enabled inventory mapped_tables for all SQL sources on this tenant."""
    from backend.database import get_db

    doc = await get_db().tenants.find_one({"tenant_id": tenant_id}, {"integration_configs": 1})
    cfg = normalize_integrations((doc or {}).get("integration_configs"))''',
'''# P02: called 3-4x per voice turn (is_inventory_question_for_tenant,
# tenant_has_sql_inventory, sdr_node, the catalog probe plan), each time doing a
# Mongo read plus two deepcopies inside normalize_integrations.
_MAPPING_CACHE: "OrderedDict[str, tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
_MAPPING_TTL = 60.0
_MAPPING_MAX = 2000


def invalidate_inventory_mappings(tenant_id: Optional[str] = None) -> None:
    if tenant_id is None:
        _MAPPING_CACHE.clear()
    else:
        _MAPPING_CACHE.pop(tenant_id, None)


async def load_inventory_mappings(tenant_id: str) -> List[Dict[str, Any]]:
    """Return enabled inventory mapped_tables for all SQL sources on this tenant."""
    from backend.database import get_db

    hit = _MAPPING_CACHE.get(tenant_id)
    if hit and time.monotonic() <= hit[0]:
        _MAPPING_CACHE.move_to_end(tenant_id)
        return list(hit[1])
    _MAPPING_CACHE.pop(tenant_id, None)

    doc = await get_db().tenants.find_one(
        {"tenant_id": tenant_id}, {"integration_configs": 1}, max_time_ms=2000
    )
    cfg = normalize_integrations((doc or {}).get("integration_configs"))'''),

('''        out.extend(get_mapped_tables(tm, "inventory"))
    return out''',
'''        out.extend(get_mapped_tables(tm, "inventory"))

    _MAPPING_CACHE[tenant_id] = (time.monotonic() + _MAPPING_TTL, list(out))
    _MAPPING_CACHE.move_to_end(tenant_id)
    while len(_MAPPING_CACHE) > _MAPPING_MAX:
        _MAPPING_CACHE.popitem(last=False)
    return out'''),

('''    for src in (cfg.get("inventory") or {}).get("sources") or []:
        if not src.get("enabled", True):
            continue''',
'''    inv = cfg.get("inventory") or {}
    # A18: the block-level flag was ignored, so a tenant who switched Inventory
    # off still routed catalog questions to a dead adapter AND was blocked from
    # FAQ seeding.
    if not inv.get("enabled", True):
        _MAPPING_CACHE[tenant_id] = (time.monotonic() + _MAPPING_TTL, [])
        return []

    for src in inv.get("sources") or []:
        if not src.get("enabled", True):
            continue'''),

("""import re
from typing import Any, Dict, List, Set""",
 """import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set"""),

# ---------------- P15: quadratic vocab expansion ----------------
('''    for m in mapped:
        for field in (m.get("table"), m.get("label"), m.get("role")):
            vocab |= _tokens(str(field or ""))
        # Plural/singular light expand for common labels
        for w in list(vocab):
            if w.endswith("s") and len(w) > 3:
                vocab.add(w[:-1])
            else:
                vocab.add(w + "s")''',
'''    for m in mapped:
        for field in (m.get("table"), m.get("label"), m.get("role")):
            vocab |= _tokens(str(field or ""))
    # P15: this expansion used to sit inside the per-table loop, re-walking the
    # whole accumulated vocabulary for every mapped table (O(N·|vocab|)) and
    # re-pluralising already-plural tokens.
    for w in list(vocab):
        if w.endswith("s") and len(w) > 3:
            vocab.add(w[:-1])
        else:
            vocab.add(w + "s")'''),
])

# ---------------- P07: stop rebuilding the LLM client every call ----------------
edit('backend/agent/llm.py', [
('''from langchain_google_genai import ChatGoogleGenerativeAI

from backend.config import settings


def get_chat_llm(
    *,
    streaming: bool = False,
    temperature: float = 0.3,
    max_retries: int = 2,
) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_MODEL,
        temperature=temperature,
        streaming=streaming,
        max_retries=max_retries,
    )''',
'''from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI

from backend.config import settings


# P07: a fresh ChatGoogleGenerativeAI (and its HTTP client / connection pool) was
# being constructed on every sdr_node pass and twice more in the voice fast path.
# The instance is stateless for our usage, so memoise per configuration.
@lru_cache(maxsize=16)
def _build_chat_llm(streaming: bool, temperature: float, max_retries: int) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_MODEL,
        temperature=temperature,
        streaming=streaming,
        max_retries=max_retries,
    )


def get_chat_llm(
    *,
    streaming: bool = False,
    temperature: float = 0.3,
    max_retries: int = 2,
) -> ChatGoogleGenerativeAI:
    return _build_chat_llm(streaming, temperature, max_retries)'''),
])

# ---------------- P04: hub retrieval must fit inside the turn budget ----------------
edit('backend/integrations/adapter_hub_client.py', [
('''    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{base}/retrieve",''',
'''    try:
        # P04: this is an optional enrichment on the voice critical path. A 10s
        # timeout here exceeded the entire turn budget on its own.
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.post(
                f"{base}/retrieve",'''),
])

# ---------------- P08: parallelise the independent lookups in sdr_node ----------------
edit('backend/agent/graph.py', [
('''    inventory_intent = await is_inventory_question_for_tenant(tenant_id, user_text)
    mapped = await load_inventory_mappings(tenant_id)
    mapped_hint = format_mapped_entities_for_prompt(mapped)''',
'''    # P08: these two are independent and were awaited back to back.
    inventory_intent, mapped = await asyncio.gather(
        is_inventory_question_for_tenant(tenant_id, user_text),
        load_inventory_mappings(tenant_id),
    )
    mapped_hint = format_mapped_entities_for_prompt(mapped)'''),
])
print("latency patch applied")

# ===== batch3d_invalidation.py =====

print("Cache invalidation — a stale tenant cache is worse than no cache")

# One place that clears every per-tenant cache, so future writers only learn one name.
edit('backend/tenant/registry.py', [
('''def invalidate_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Call after any write to a tenant document."""
    if tenant_id is None:
        _TENANT_CACHE.clear()
    else:
        _TENANT_CACHE.pop(tenant_id, None)''',
'''def invalidate_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Clear ONLY the tenant-document cache. Prefer invalidate_tenant()."""
    if tenant_id is None:
        _TENANT_CACHE.clear()
    else:
        _TENANT_CACHE.pop(tenant_id, None)


def invalidate_tenant(tenant_id: Optional[str] = None) -> None:
    """
    Drop every per-tenant cache after a write to the tenant document.

    Call this from ANY code path that mutates `tenants` — settings, integrations,
    billing tier, prompt repair. Missing a call means an admin saves a change and
    the agent keeps using the old config for up to a minute.
    """
    invalidate_tenant_cache(tenant_id)

    try:
        from backend.integrations.tenant_inventory import invalidate_inventory_mappings

        invalidate_inventory_mappings(tenant_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("inventory mapping invalidation failed", exc_info=True)

    if tenant_id:
        try:
            from backend.integrations.catalog_cache import invalidate_catalog

            invalidate_catalog(tenant_id)
        except Exception:  # pragma: no cover - defensive
            logger.debug("catalog invalidation failed", exc_info=True)'''),
])

# --- every writer calls it ---
edit('backend/integrations/service.py', [
('''    await db.tenants.update_one(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "settings.system_prompt": build_tenant_system_prompt(org_name, description),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )''',
'''    await db.tenants.update_one(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "settings.system_prompt": build_tenant_system_prompt(org_name, description),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    _invalidate(tenant_id)'''),

('''        from backend.integrations.catalog_cache import invalidate_catalog
        invalidate_catalog(tenant_id)''',
'''        _invalidate(tenant_id)'''),

("""from backend.integrations.providers import get_provider""",
 """from backend.integrations.providers import get_provider


def _invalidate(tenant_id: str) -> None:
    \"\"\"Drop cached tenant state after a write (P01/P02 caches).\"\"\"
    from backend.tenant.registry import invalidate_tenant

    invalidate_tenant(tenant_id)"""),
])
print("invalidation applied")

# ===== batch3e2.py =====

edit('backend/integrations/service.py', [
('''        await db.tenants.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "settings": settings,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        return await IntegrationService.get_admin_view(tenant_id)''',
'''        await db.tenants.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "settings": settings,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        _invalidate(tenant_id)
        return await IntegrationService.get_admin_view(tenant_id)'''),
])
edit('backend/billing/routes.py', [
("""from backend.config import settings""",
 """from backend.config import settings
from backend.tenant.registry import invalidate_tenant"""),
])
print("applied")

# ===== batch3f.py =====

edit('backend/billing/routes.py', [
('''                "settings.rate_limit_per_minute": 300 if price_id == "price_enterprise" else 150
            }}
        )
        return {''',
'''                "settings.rate_limit_per_minute": 300 if price_id == "price_enterprise" else 150
            }}
        )
        invalidate_tenant(tenant.tenant_id)
        return {'''),

('''                    "status": "active"
                }}
            )
            logger.info("Successfully updated tenant %s to tier %s via webhook checkout", tenant_id, plan["name"])''',
'''                    "status": "active"
                }}
            )
            invalidate_tenant(tenant_id)
            logger.info("Successfully updated tenant %s to tier %s via webhook checkout", tenant_id, plan["name"])'''),

('''                    "status": "trial_expired"
                }}
            )''',
'''                    "status": "trial_expired"
                }}
            )
            invalidate_tenant(tenant_doc["tenant_id"])'''),
])

# admin routes: prompt reset also mutates the tenant doc
edit('backend/integrations/service.py', [
('''    async def reset_agent_prompt(tenant_id: str) -> Dict[str, Any]:''',
'''    async def reset_agent_prompt(tenant_id: str) -> Dict[str, Any]:   # noqa: D401'''),
])
print("billing + admin invalidation applied")

# ===== batch3g.py =====

edit('backend/integrations/catalog_cache.py', [
('''    try:
        mapped = await load_inventory_mappings(tenant_id)
    except Exception as e:
        logger.warning("Could not load mappings for probe plan (%s): %s", tenant_id, e)
        mapped = []''',
'''    try:
        # max_time_ms does not cover Mongo *server selection*, so an unreachable
        # replica set would block here for the full 30s selection timeout while
        # holding this tenant's warmup lock.
        mapped = await asyncio.wait_for(load_inventory_mappings(tenant_id), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning("Probe plan for %s timed out loading mappings — using fallback", tenant_id)
        mapped = []
    except Exception as e:
        logger.warning("Could not load mappings for probe plan (%s): %s", tenant_id, e)
        mapped = []'''),
])
print("probe-plan timeout guard added")

# ===== new file: backend/scripts/__init__.py =====
(ROOT / 'backend/scripts/__init__.py').parent.mkdir(parents=True, exist_ok=True)
(ROOT / 'backend/scripts/__init__.py').write_text('')
print('  created backend/scripts/__init__.py')

# ===== new file: backend/scripts/migrate_checkpoint_namespacing.py =====
(ROOT / 'backend/scripts/migrate_checkpoint_namespacing.py').parent.mkdir(parents=True, exist_ok=True)
(ROOT / 'backend/scripts/migrate_checkpoint_namespacing.py').write_text('#!/usr/bin/env python3\n"""\nOne-off migration for audit item T09 (checkpoint tenant-namespacing).\n\nBackground\n----------\n`MongoDBSaver` keys LangGraph checkpoints on `thread_id`, and `thread_id` was\nentirely client-supplied — a path parameter on `/ws/chat/{thread_id}`, a body\nfield on `/api/query`, `console_thread_id` on the embed session. Two tenants\nusing the same id shared one conversation state, so tenant A could resume tenant\nB\'s message history, extracted lead PII and prior tool results.\n\nCheckpoints are now stored under `"<tenant_id>::<thread_id>"`. Rows written\nbefore that change are keyed on the bare id and are invisible to the new code —\nin-flight dashboard conversations would silently lose their agent memory\n(transcripts in `conversations` are unaffected).\n\nThis script re-keys them.\n\nResolving the owner\n-------------------\nThe checkpoint documents carry no tenant field, so ownership is recovered from\n    conversations   (tenant_id, thread_id)\n    voice_call_links / voice_call_sessions   for vapi_* and embed_* threads\n\nA thread id claimed by more than one tenant is exactly the collision the fix\nprevents. Those are never guessed at: they are reported and left alone, and\n`--purge-ambiguous` can delete them so the affected conversations simply start\nfresh.\n\nUsage\n-----\n    python -m backend.scripts.migrate_checkpoint_namespacing            # dry run\n    python -m backend.scripts.migrate_checkpoint_namespacing --apply\n    python -m backend.scripts.migrate_checkpoint_namespacing --apply --purge-ambiguous\n\nSafe to re-run: already-namespaced rows are skipped.\n"""\nfrom __future__ import annotations\n\nimport argparse\nimport asyncio\nimport logging\nfrom collections import defaultdict\nfrom typing import Dict, List, Optional, Set\n\nfrom backend.database import db_client, get_db\nfrom backend.tenant.thread_scope import SEP, scoped_thread_id\n\nlogging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")\nlogger = logging.getLogger("migrate_checkpoints")\n\nCOLLECTIONS = ("checkpoints", "writes")\n\n\nasync def _owners_by_thread() -> Dict[str, Set[str]]:\n    """thread_id -> {tenant_id}, from every collection that records both."""\n    db = get_db()\n    owners: Dict[str, Set[str]] = defaultdict(set)\n\n    async for doc in db.conversations.find({}, {"tenant_id": 1, "thread_id": 1}):\n        tid, thread = doc.get("tenant_id"), doc.get("thread_id")\n        if tid and thread:\n            owners[str(thread)].add(str(tid))\n\n    for coll, field in (("voice_call_links", "console_thread_id"),\n                        ("voice_call_sessions", "console_thread_id")):\n        async for doc in db[coll].find({}, {"tenant_id": 1, field: 1}):\n            tid, thread = doc.get("tenant_id"), doc.get(field)\n            if tid and thread:\n                owners[str(thread)].add(str(tid))\n\n    # vapi_<call_id> threads are recoverable from the call link\n    async for doc in db.voice_call_links.find({}, {"tenant_id": 1, "call_id": 1}):\n        tid, call_id = doc.get("tenant_id"), doc.get("call_id")\n        if tid and call_id:\n            owners[f"vapi_{call_id}"].add(str(tid))\n\n    return owners\n\n\nasync def migrate(apply: bool, purge_ambiguous: bool) -> int:\n    db = get_db()\n    owners = await _owners_by_thread()\n    logger.info("Recovered owners for %d thread ids", len(owners))\n\n    stats = {"scanned": 0, "already": 0, "migrated": 0,\n             "ambiguous": 0, "orphaned": 0, "purged": 0}\n    ambiguous: List[str] = []\n    orphaned: List[str] = []\n\n    for coll_name in COLLECTIONS:\n        coll = db[coll_name]\n        thread_ids = await coll.distinct("thread_id")\n        logger.info("%s: %d distinct thread ids", coll_name, len(thread_ids))\n\n        for thread_id in thread_ids:\n            if thread_id is None:\n                continue\n            thread_id = str(thread_id)\n            stats["scanned"] += 1\n\n            if SEP in thread_id:\n                stats["already"] += 1\n                continue\n\n            candidates = owners.get(thread_id, set())\n\n            if len(candidates) > 1:\n                stats["ambiguous"] += 1\n                ambiguous.append(f"{coll_name}:{thread_id} -> {sorted(candidates)}")\n                if purge_ambiguous and apply:\n                    res = await coll.delete_many({"thread_id": thread_id})\n                    stats["purged"] += res.deleted_count\n                continue\n\n            if not candidates:\n                stats["orphaned"] += 1\n                orphaned.append(f"{coll_name}:{thread_id}")\n                continue\n\n            tenant_id = next(iter(candidates))\n            new_key = scoped_thread_id(tenant_id, thread_id)\n            n = await coll.count_documents({"thread_id": thread_id})\n            if apply:\n                await coll.update_many({"thread_id": thread_id},\n                                       {"$set": {"thread_id": new_key}})\n            stats["migrated"] += n\n            logger.debug("%s: %s -> %s (%d docs)", coll_name, thread_id, new_key, n)\n\n    verb = "Migrated" if apply else "Would migrate"\n    logger.info("-" * 60)\n    logger.info("%s %d checkpoint/write documents", verb, stats["migrated"])\n    logger.info("Already namespaced : %d thread ids", stats["already"])\n    logger.info("Ambiguous          : %d thread ids", stats["ambiguous"])\n    logger.info("Orphaned (no owner): %d thread ids", stats["orphaned"])\n    if purge_ambiguous:\n        logger.info("Purged             : %d documents", stats["purged"])\n\n    if ambiguous:\n        logger.warning("Ambiguous thread ids (claimed by >1 tenant — this is the "\n                       "cross-tenant collision T09 fixes):")\n        for line in ambiguous[:20]:\n            logger.warning("   %s", line)\n        if len(ambiguous) > 20:\n            logger.warning("   ... and %d more", len(ambiguous) - 20)\n        logger.warning("Re-run with --purge-ambiguous to delete these; the affected "\n                       "conversations will simply start with fresh agent memory.")\n\n    if orphaned:\n        logger.info("Orphaned thread ids (no conversation or voice record; likely "\n                    "already-deleted threads): %d — left untouched.", len(orphaned))\n\n    if not apply:\n        logger.info("DRY RUN — nothing was written. Re-run with --apply.")\n    return 0\n\n\ndef main() -> int:\n    parser = argparse.ArgumentParser(description=__doc__,\n                                     formatter_class=argparse.RawDescriptionHelpFormatter)\n    parser.add_argument("--apply", action="store_true",\n                        help="write the changes (default is a dry run)")\n    parser.add_argument("--purge-ambiguous", action="store_true",\n                        help="delete checkpoints whose thread id is claimed by more than one tenant")\n    args = parser.parse_args()\n\n    db_client.connect()\n    try:\n        return asyncio.run(migrate(args.apply, args.purge_ambiguous))\n    finally:\n        db_client.disconnect()\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
print('  created backend/scripts/migrate_checkpoint_namespacing.py')
print('BATCH 3 (hotfix + latency + migration) applied to', ROOT.resolve())
