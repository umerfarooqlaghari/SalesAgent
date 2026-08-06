"""Voice FAQ / natural catalog speaking tests."""
from __future__ import annotations

import asyncio
import os
import sys
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations import catalog_cache as cc
from backend.integrations import knowledge_cache as kc
from backend.integrations.voice_fastpath import (
    _extract_entity_names,
    _natural_catalog_fallback,
    _spoken_from_knowledge,
    try_voice_faq_answer,
)


def test_extract_names_not_columns():
    catalog = (
        "[Productions]\n"
        "  • name=Mentore, status=Active\n"
        "  • name=Hamlet, status=Active\n"
        "  • name=Macbeth, status=Draft\n"
    )
    names = _extract_entity_names(catalog)
    assert names[:3] == ["Mentore", "Hamlet", "Macbeth"]
    spoken = _natural_catalog_fallback(catalog, "what productions do you have?")
    assert "Mentore" in spoken and "Hamlet" in spoken
    assert "name=" not in spoken
    assert "equals" not in spoken.lower()
    assert "live data" not in spoken.lower()
    print("✓ natural fallback lists names, not columns")


def test_interrupt_specific_product_pivots():
    catalog = (
        "[Productions]\n"
        "  • name=Mentore, status=Active, type=Feature\n"
        "  • name=Hamlet, status=Active\n"
    )
    spoken = _natural_catalog_fallback(catalog, "wait tell me about Mentore")
    assert "Mentore" in spoken
    assert "Hamlet" not in spoken  # must not restart full list
    assert "name=" not in spoken
    print("✓ specific follow-up pivots off the list")


def test_spoken_knowledge_clean():
    knowledge = "[Services] Alpha Devs builds AI ERP and computer vision.\n--- X ---\n"
    spoken = _spoken_from_knowledge(knowledge, "what are your services?")
    assert "AI ERP" in spoken
    assert "---" not in spoken
    print("✓ knowledge spoken clean")


@pytest.mark.asyncio
async def test_inventory_does_not_dump_raw_sql():
    tid = "construct_test"
    catalog = (
        "[Productions]\n"
        "  • name=Mentore, status=Active\n"
        "  • name=Hamlet, status=Active\n"
    )
    cc._CACHE[tid] = {
        "text": catalog,
        "expires_at": time.time() + 600,
        "fetched_at": time.time(),
        "org_name": "Construct",
    }
    # Force LLM failure → natural fallback
    with patch(
        "backend.integrations.voice_fastpath.is_inventory_question_for_tenant",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.integrations.voice_fastpath.tenant_has_sql_inventory",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.integrations.voice_fastpath.get_tenant_by_id",
        new=AsyncMock(return_value=MagicMock(org_name="Construct")),
    ), patch(
        "backend.integrations.voice_fastpath.get_chat_llm",
        side_effect=RuntimeError("skip llm"),
    ):
        ans = await try_voice_faq_answer(tid, "what products do you have?")
    assert ans is not None
    assert "Mentore" in ans
    assert "name=" not in ans
    assert "From our live data" not in ans
    assert "equals" not in ans.lower()
    cc.invalidate_catalog(tid)
    print("✓ inventory answer is spoken English, not SQL dump")


@pytest.mark.asyncio
async def test_company_faq_non_sql():
    tid = "alpha_faq"
    kc._CACHE[tid] = {
        "text": "[Services] We offer AI ERP and ed-tech.",
        "expires_at": 9e12,
        "fetched_at": 0,
        "org_name": "Alpha",
    }
    with patch(
        "backend.integrations.voice_fastpath.tenant_has_sql_inventory",
        new=AsyncMock(return_value=False),
    ), patch(
        "backend.integrations.voice_fastpath.is_inventory_question_for_tenant",
        new=AsyncMock(return_value=False),
    ), patch("backend.integrations.voice_fastpath.get_chat_llm") as llm:
        ans = await try_voice_faq_answer(tid, "who are you?")
    llm.assert_not_called()
    assert ans and "AI ERP" in ans
    kc.invalidate_knowledge(tid)
    print("✓ company FAQ still works")


async def main():
    test_extract_names_not_columns()
    test_interrupt_specific_product_pivots()
    test_spoken_knowledge_clean()
    await test_inventory_does_not_dump_raw_sql()
    await test_company_faq_non_sql()
    print("\nAll voice fast-path tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
