"""
Generate a tenant's system prompt from their actual connected data.

`build_tenant_system_prompt` is a static template: it knows the org name and the
free-text company description, and nothing else. Tenants therefore hand-wrote
their own prompts, and those prompts embedded a snapshot of the catalogue — a
literal list of three products — which the model then preferred over the live
tables. The catalogue grows, the prompt does not, and the agent keeps answering
with the stale three.

So the generated prompt is grounded in the tenant's *schema* rather than their
rows: which tables are mapped, what each one holds, and which columns carry
descriptions. It is explicitly forbidden from naming individual items, so it
cannot go stale.

The invariant parts — placeholders, tool rules, catalogue precedence — are never
LLM-written. Only the company-orientation paragraph is generated, and if
generation fails for any reason the static template is used unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GENERATION_TIMEOUT_S = 20.0
_MAX_SAMPLE_CHARS = 2500

# If the model names specific items anyway, the prompt would go stale the moment
# the tenant adds a row. These are the shapes that indicate it did.
_ITEM_LIST_HINTS = re.compile(
    r"(^\s*[-*•]\s+\S+.*$\n?){3,}|(\$\d+\s*/\s*(mo|month|yr|year))",
    re.MULTILINE | re.IGNORECASE,
)


def _describe_schema(mapped: List[Dict[str, Any]]) -> str:
    """A compact, readable description of what the agent can actually read."""
    if not mapped:
        return "(no database tables are mapped yet)"
    lines = []
    for m in mapped[:20]:
        label = m.get("label") or m.get("table") or "table"
        role = m.get("role") or ""
        cols = m.get("columns") or {}
        if isinstance(cols, list):
            col_names = [str(c) for c in cols]
        else:
            col_names = [str(k) for k in cols.keys()]
        searchable = m.get("search_columns") or []
        bits = [f'- "{label}" (table: {m.get("table")}']
        if role:
            bits.append(f", role: {role}")
        bits.append(")")
        line = "".join(bits)
        if col_names:
            line += f"\n    columns: {', '.join(col_names[:18])}"
        if searchable:
            line += f"\n    searchable: {', '.join(str(s) for s in searchable[:8])}"
        lines.append(line)
    return "\n".join(lines)


async def _catalog_sample(tenant_id: str) -> str:
    """A few real rows, so the model can see the shape of the data (not memorise it)."""
    try:
        from backend.integrations.catalog_cache import (get_catalog_sections,
                                                        warmup_catalog)

        sections = get_catalog_sections(tenant_id)
        if not sections:
            await asyncio.wait_for(warmup_catalog(tenant_id), timeout=10.0)
            sections = get_catalog_sections(tenant_id)
    except Exception:
        logger.debug("Could not warm catalog for prompt generation", exc_info=True)
        return ""

    out = []
    for label, text in list(sections.items())[:6]:
        head = "\n".join((text or "").splitlines()[:6])
        out.append(f"[{label}]\n{head}")
    return "\n\n".join(out)[:_MAX_SAMPLE_CHARS]


_GENERATION_INSTRUCTIONS = """You write the orientation section of a system prompt for a company's AI sales assistant.

Write 6-12 lines covering:
- who the company is and what it does, in the company's own terms
- what kinds of things live in its connected database, using the table labels below
  as the vocabulary the assistant should use for each kind
- which categories are DISTINCT from each other, so the assistant never answers a
  question about one with items from another

HARD RULES — breaking any of these makes the output unusable:
1. NEVER list, name, count or price individual items. The database changes; this text
   does not. Refer to categories and where to look them up, never to specific rows.
2. NEVER invent a service, product or capability that is not evidenced below.
3. Do not write greetings, tool instructions, formatting rules or placeholders —
   those are appended separately.
4. Do not use markdown headings or bullet-point lists of items.
5. Plain prose and short lines only. No preamble, no sign-off. Output only the section.
"""


async def generate_company_section(
    org_name: str,
    company_description: str,
    mapped: List[Dict[str, Any]],
    catalog_sample: str = "",
) -> Optional[str]:
    """LLM-written orientation paragraph, or None if it could not be produced safely."""
    from backend.agent.llm import get_chat_llm
    from langchain_core.messages import HumanMessage, SystemMessage

    schema = _describe_schema(mapped)
    user = (
        f"Company name: {org_name}\n\n"
        f"What the company says it does:\n{(company_description or '(not provided)').strip()}\n\n"
        f"Tables the assistant is allowed to read:\n{schema}\n"
    )
    if catalog_sample:
        user += (
            "\nA few sample rows, ONLY so you understand the shape of each table. "
            "Do not reproduce any of these values:\n" + catalog_sample
        )

    try:
        llm = get_chat_llm(streaming=False, temperature=0.2, max_retries=1)
        msg = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=_GENERATION_INSTRUCTIONS),
                         HumanMessage(content=user)]),
            timeout=GENERATION_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("System prompt generation failed for %s: %s", org_name, e)
        return None

    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    text = str(content or "").strip()
    if len(text) < 60:
        logger.warning("Generated prompt section too short — falling back to the template")
        return None

    if _ITEM_LIST_HINTS.search(text):
        # It listed items despite being told not to. That is exactly the failure
        # mode being fixed, so the static template is safer.
        logger.warning("Generated prompt section enumerated catalogue items — rejecting")
        return None

    # Placeholders are substituted later; a stray brace from the model would show
    # up verbatim in the prompt.
    return text.replace("{", "(").replace("}", ")")


async def generate_tenant_system_prompt(tenant_id: str) -> Dict[str, Any]:
    """
    Build a complete system prompt for `tenant_id` from its connected data.

    Returns {"prompt": str, "generated": bool, "reason": str}. `generated` is
    False when the static template was used, with `reason` explaining why.
    """
    from backend.agent.prompts import (SHARED_RULES_TEXT,
                                       build_tenant_system_prompt,
                                       compose_tenant_prompt)
    from backend.integrations.tenant_inventory import load_inventory_mappings
    from backend.tenant.registry import get_tenant_by_id

    ctx = await get_tenant_by_id(tenant_id)
    if not ctx:
        raise ValueError("Tenant not found")

    org = ctx.org_name or tenant_id
    description = (getattr(ctx.settings, "company_description", "") or "").strip()

    try:
        mapped = await load_inventory_mappings(tenant_id)
    except Exception:
        logger.debug("Could not load mappings for prompt generation", exc_info=True)
        mapped = []

    sample = await _catalog_sample(tenant_id) if mapped else ""
    section = await generate_company_section(org, description, mapped, sample)

    if not section:
        return {
            "prompt": build_tenant_system_prompt(org, description),
            "generated": False,
            "reason": "Used the standard template (generation unavailable or rejected).",
            "tables": [m.get("label") or m.get("table") for m in mapped],
        }

    return {
        "prompt": compose_tenant_prompt(org, section, mapped),
        "generated": True,
        "reason": f"Written from {len(mapped)} connected table(s).",
        "tables": [m.get("label") or m.get("table") for m in mapped],
    }
