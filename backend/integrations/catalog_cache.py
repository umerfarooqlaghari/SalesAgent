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
import re
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
_MAX_CHARS = 14000
_MAX_SECTION_CHARS = 6000
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
    "table is connected but returned no rows",
)


def _fit_rows(body: str, budget: int) -> str:
    """
    Shrink a section to `budget` chars while keeping EVERY row item name visible.
    Trims line descriptions so no item is dropped from the prompt.
    """
    body = body or ""
    if len(body) <= budget:
        return body

    lines = body.splitlines()
    if len(lines) <= 1:
        return body[:budget] + "…"

    notice = "\n…(each entry shortened to fit)"
    room = max(0, budget - len(notice))
    per_line = max(40, room // max(1, len(lines)))

    kept, used = [], 0
    for line in lines:
        allowance = per_line
        if len(line) <= allowance:
            kept.append(line)
            used += len(line) + 1
            continue
        trimmed = line[:allowance].rstrip() + "…"
        kept.append(trimmed)
        used += len(trimmed) + 1

    out = "\n".join(kept)
    if len(out) > room:
        out = out[:room].rstrip()
    return out + notice


def _fit_sections(sections: Dict[str, str], budget: int) -> Dict[str, str]:
    """
    Share the overall budget across sections instead of truncating the tail.

    Their catalogue was 14013 chars against a 14000 cap, so the LAST section was
    cut — and the sections that matter to a customer (7 products, 9 services)
    were competing for room with 82 rows of CMS content blocks. Every section now
    gets a fair share; sections under their share donate the surplus.
    """
    if not sections:
        return sections
    overhead = sum(len(label) + 4 for label in sections) + 2 * len(sections)
    room = max(1000, budget - overhead)

    fair = room // len(sections)
    # Sections already under their share keep everything and free up the rest.
    small = {k: v for k, v in sections.items() if len(v) <= fair}
    large = {k: v for k, v in sections.items() if len(v) > fair}
    if not large:
        return sections

    freed = sum(fair - len(v) for v in small.values())
    per_large = fair + (freed // len(large))

    out = dict(sections)
    for key in large:
        out[key] = _fit_rows(sections[key], per_large)
    return out


def _looks_like_error(sample: str) -> bool:
    low = (sample or "").strip().lower()
    if not low:
        return True
    return any(marker in low for marker in _ERROR_MARKERS)


# The adapter also emits "[Label] — query error: ..." and
# "[Label] — table is connected but returned no rows.", so the header must be
# allowed trailing text. Without that, those lines were swallowed into the
# PREVIOUS section's body and made _looks_like_error discard a perfectly good
# section.
_SECTION_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")


def split_sections(text: str) -> Dict[str, str]:
    """
    Split a catalog dump into {label: rows} using the adapter's own [Label] headers.

    Sectioning used to be done by issuing one probe per mapped table label. That
    does not work: SqlPOSAdapter treats a generic-sounding query like
    "Product catalog" as a request for *every* mapped table, so that probe
    returned byte-identical text to the broad probe, got de-duplicated away, and
    the tenant ended up with a single "all" section. Every question then fell
    back to the whole catalogue — which is why asking about services came back
    with product names.

    The adapter already labels each table's block, so one broad probe carries all
    the structure needed.
    """
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []

    def flush():
        if current and buf:
            body = "\n".join(buf).strip()
            if body and not _looks_like_error(body):
                sections[current.strip().lower()] = body

    for line in (text or "").splitlines():
        m = _SECTION_HEADER.match(line)
        if m:
            flush()
            current, buf = m.group(1), []
            trailing = (m.group(2) or "").strip()
            if trailing:
                buf.append(trailing)
            continue
        if current is not None:
            buf.append(line)
    flush()
    return sections


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


_CATEGORY_VALUE = re.compile(r"category:\s*([^,)\n]+)", re.I)


def section_data_terms(body: str) -> set:
    """
    Vocabulary drawn from the rows themselves — currently the category values.

    A production company labels its table "Productions" with role "products", so
    neither the label nor the role synonyms match a caller asking about "films".
    The rows do: their categories are "Feature Film", "Documentary", "Drama
    Series". Taking the vocabulary from the data keeps this working for domains
    nobody enumerated in advance.
    """
    terms = set()
    for match in _CATEGORY_VALUE.finditer(body or ""):
        for word in re.findall(r"[a-z0-9]+", match.group(1).lower()):
            if len(word) > 2:
                terms.add(word)
                terms.add(word[:-1] if word.endswith("s") else word + "s")
    return terms


def get_section_roles(tenant_id: str) -> Dict[str, str]:
    """{section label -> declared role}, used to widen section matching."""
    entry = _live_entry(tenant_id)
    return dict(entry.get("roles") or {}) if entry else {}


def _infer_role_from_label(label: str) -> Optional[str]:
    """
    Guess a role from the section label when the mapping declares none.

    Their tables are mapped with role="-" for everything except one, so role
    synonyms were doing nothing: "what packages do you have" matched no label
    and no role, scored zero everywhere, and fell through to the whole
    catalogue — which is why a question about packages came back about products.
    A label of "Services" plainly means the services role; use it.
    """
    words = _tokens(label)
    try:
        from backend.adapters.sql_pos import _ROLE_SYNONYMS
    except Exception:      # pragma: no cover - defensive
        return None
    for role_key, syns in _ROLE_SYNONYMS.items():
        if role_key in words or (words & syns):
            return role_key
    return None


def _role_synonyms(role: Optional[str]) -> set:
    if not role:
        return set()
    try:
        from backend.adapters.sql_pos import _ROLE_SYNONYMS
    except Exception:      # pragma: no cover - defensive
        return set()
    role = str(role).lower()
    out = set()
    for key, syns in _ROLE_SYNONYMS.items():
        if key in role:
            out |= syns
    return out


def is_catalog_warm(tenant_id: str) -> bool:
    """
    True when this process holds a live (unexpired) catalog for the tenant.

    Recorded per turn by the telemetry: a cold catalog makes the model answer
    from the system prompt alone — fluent but factually thin, which is precisely
    what "the agent got dumb" looks like from the outside.
    """
    return _live_entry(tenant_id) is not None


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

    roles = get_section_roles(tenant_id)

    # Weighted, because the previous rule gave a label hit, a role hit and a
    # stray category value in some row EQUAL weight — and then broke ties by
    # dict insertion order. On a real catalogue with 8 sections, four of which
    # are CMS blocks ("service info cards", "product content blocks", …), that
    # is a coin toss: asking about services could land on a product section
    # because one product row happened to carry a "Service" category.
    LABEL_HIT, COVERAGE, ROLE_HIT, DATA_HIT = 10.0, 5.0, 4.0, 1.0

    scored = []
    for label in sections:
        if label == "all":
            continue
        label_tokens = _tokens(label)
        # singular/plural tolerance: "service" should match a "services" section
        expanded = set(label_tokens)
        for t in label_tokens:
            expanded.add(t[:-1] if t.endswith("s") else t + "s")

        label_hits = len(q & expanded)
        score = LABEL_HIT * label_hits
        if label_hits and label_tokens:
            # Prefer the label the question actually NAMES. "services" matches
            # all of itself; "service info cards" matches a third of itself, so
            # a question about services should not land there.
            score += COVERAGE * (label_hits / len(label_tokens))

        # Role synonyms, so everyday words reach the right section whatever the
        # tenant called it — a dental clinic's "Treatments" table answering
        # "what services do you offer". Roles are often missing from the mapping,
        # so fall back to inferring one from the label itself.
        role = roles.get(label) or _infer_role_from_label(label)
        score += ROLE_HIT * len(q & _role_synonyms(role))

        # Values drawn from the rows. Deliberately the weakest signal: it exists
        # so a production company's "films" reaches a table nobody labelled
        # "films", NOT so a stray category can outvote an explicit label.
        score += DATA_HIT * min(len(q & section_data_terms(sections.get(label, ""))), 3)

        if score > 0:
            # Tie-break on the shorter (more specific) label, then the name, so
            # the result is deterministic rather than dependent on dict order.
            scored.append((-score, len(label), label))

    if scored:
        scored.sort()
        return scored[0][2]

    # No category word in the question — but "tell me about Sentrix" should still
    # narrow to the section that actually contains Sentrix, rather than handing
    # the model every category at once.
    return _section_containing_item(sections, user_text)


_ITEM_LINE = re.compile(r"^\s*[•\-*]\s*([^(\n]{2,120}?)\s*(?:\(|$)")


def _section_containing_item(sections: Dict[str, str], user_text: str) -> Optional[str]:
    q = (user_text or "").lower()
    q_toks = _tokens(user_text)
    best, best_score = None, 0
    for label, body in sections.items():
        if label == "all":
            continue
        for line in (body or "").splitlines():
            m = _ITEM_LINE.match(line)
            if not m:
                continue
            name = m.group(1).strip().lower()
            if len(name) < 3:
                continue
            # Match if full name in q, or q in name, or token overlap
            if name in q or q in name:
                score = len(name) * 10
            else:
                toks = _tokens(name)
                hit = len(q_toks & toks)
                if hit < 2 and not (hit == 1 and any(len(t) >= 5 for t in (q_toks & toks))):
                    continue
                score = hit * 5
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
            found = split_sections(sample)
            if found:
                # A "targeted" probe can still return several tables; file each
                # under its own label rather than all of them under this one.
                for key, body in found.items():
                    sections.setdefault(key, body)
            else:
                text = sample.strip()
                if text and text not in sections.values():
                    sections[label] = text

        # One broad probe carries every mapped table, each under its own [Label]
        # header, so the sections come from the data rather than from guessing a
        # query per label.
        broad = ""
        try:
            broad = await asyncio.wait_for(pos.list_products(None), timeout=8.0)
        except Exception as e:
            logger.warning("Broad catalog probe failed for %s: %s", tenant_id, e)

        if broad and not _looks_like_error(broad):
            sections.update(split_sections(broad))
            if not sections:
                # Adapter emitted no [Label] headers (stub / legacy path).
                sections["all"] = broad.strip()

        # Only probe individually for tables the broad pass did not surface.
        plan = await _probe_plan(tenant_id)
        missing = [(label, query) for label, query in plan
                   if label != "all" and label not in sections]
        if missing:
            await asyncio.gather(*(_fetch(label, query) for label, query in missing))

        for key, body in list(sections.items()):
            sections[key] = _fit_rows(body, _MAX_SECTION_CHARS)

        if not sections:
            return {"ok": False, "error": "No catalog data returned from inventory sources"}

        sections = _fit_sections(sections, _MAX_CHARS)

        parts = []
        for label, body in sections.items():
            parts.append(body if label == "all" else f"[{label}]\n{body}")
        text = "\n\n".join(parts).strip()

        roles = {}
        try:
            from backend.integrations.tenant_inventory import load_inventory_mappings

            for m in await load_inventory_mappings(tenant_id):
                label = (m.get("label") or m.get("role") or m.get("table") or "").strip().lower()
                if label:
                    roles[label] = (m.get("role") or "").lower()
        except Exception:
            logger.debug("Could not record section roles for %s", tenant_id, exc_info=True)

        _CACHE[tenant_id] = {
            "sections": sections,
            "roles": roles,
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
