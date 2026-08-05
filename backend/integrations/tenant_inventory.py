"""
Per-tenant inventory vocabulary from mapped SQL tables.

Multi-tenant rule: never assume every org has a "products" table.
Each tenant maps whatever tables they approve (productions, sets, SKUs, services…).
Routing / FAQ decisions should use those labels when present.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from backend.integrations.normalize import normalize_integrations
from backend.integrations.table_map_util import get_mapped_tables, parse_table_map_raw

# Broad fallbacks only when we cannot load mapped labels (still not "products-only")
_GENERIC_INVENTORY_HINTS = (
    "product",
    "products",
    "production",
    "productions",
    "inventory",
    "catalog",
    "sku",
    "stock",
    "scenery",
    "sets",
    "purchase order",
    "availability",
)


def _tokens(text: str) -> Set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


async def load_inventory_mappings(tenant_id: str) -> List[Dict[str, Any]]:
    """Return enabled inventory mapped_tables for all SQL sources on this tenant."""
    from backend.database import get_db

    doc = await get_db().tenants.find_one({"tenant_id": tenant_id}, {"integration_configs": 1})
    cfg = normalize_integrations((doc or {}).get("integration_configs"))
    out: List[Dict[str, Any]] = []
    for src in (cfg.get("inventory") or {}).get("sources") or []:
        if not src.get("enabled", True):
            continue
        provider = (src.get("provider") or "").lower()
        if provider not in {"postgres", "sqlserver", "mysql"}:
            continue
        conf = src.get("config") or {}
        tm = parse_table_map_raw(conf)
        # Also accept mapped_tables nested on config (admin UI shape)
        if not tm.get("mapped_tables") and isinstance(conf.get("mapped_tables"), list):
            tm = {**tm, "mapped_tables": conf["mapped_tables"]}
        out.extend(get_mapped_tables(tm, "inventory"))
    return out


async def tenant_has_sql_inventory(tenant_id: str) -> bool:
    return bool(await load_inventory_mappings(tenant_id))


async def inventory_vocab(tenant_id: str) -> Set[str]:
    """
    Words that mean 'ask the live SQL catalog' for this tenant.
    Built from mapped table names, labels, and roles — not a global products list.
    """
    mapped = await load_inventory_mappings(tenant_id)
    vocab: Set[str] = set()
    for m in mapped:
        for field in (m.get("table"), m.get("label"), m.get("role")):
            vocab |= _tokens(str(field or ""))
        # Plural/singular light expand for common labels
        for w in list(vocab):
            if w.endswith("s") and len(w) > 3:
                vocab.add(w[:-1])
            else:
                vocab.add(w + "s")
    if mapped:
        # Always treat these as catalog-intent when SQL inventory exists
        vocab.update({"catalog", "inventory", "stock", "availability", "offer", "offers"})
    else:
        vocab.update(_GENERIC_INVENTORY_HINTS)
    return vocab


async def is_inventory_question_for_tenant(tenant_id: str, user_text: str) -> bool:
    """True if the utterance refers to this tenant's mapped inventory entities."""
    text = f" {(user_text or '').lower()} "
    q_tokens = _tokens(user_text)
    vocab = await inventory_vocab(tenant_id)

    if q_tokens & vocab:
        return True
    # Phrase fallbacks that aren't in table names but still mean "list what you sell/do from data"
    if any(
        p in text
        for p in (
            " what do you have",
            " what have you",
            "show me your",
            "list your",
            "in stock",
            "purchase order",
        )
    ):
        # Only if tenant actually has SQL inventory — otherwise it's company FAQ
        return await tenant_has_sql_inventory(tenant_id)
    return False


def format_mapped_entities_for_prompt(mapped: List[Dict[str, Any]]) -> str:
    """Short prompt hint listing this tenant's approved tables."""
    if not mapped:
        return ""
    bits = []
    for m in mapped[:12]:
        label = m.get("label") or m.get("table")
        role = m.get("role") or "data"
        table = m.get("table")
        bits.append(f"{label} (role={role}, table={table})")
    return (
        "This tenant's approved inventory tables (answer from CACHED CATALOG / query_pos_database; "
        "do not invent other entity types): "
        + "; ".join(bits)
    )
