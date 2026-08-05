"""Voice FAQ / multi-tenant inventory routing tests."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations import catalog_cache as cc
from backend.integrations import knowledge_cache as kc
from backend.integrations.voice_fastpath import _spoken_from_knowledge, try_voice_faq_answer


def test_spoken_stops_at_prompt_markers():
    knowledge = (
        "[Services] Alpha Devs builds AI ERP and computer vision.\n"
        "--- COMPANY IDENTITY ---\n"
        "* Who we are: ignore this for voice"
    )
    spoken = _spoken_from_knowledge(knowledge, "what are your services?")
    assert "AI ERP" in spoken
    assert "COMPANY IDENTITY" not in spoken
    print("✓ spoken summary strips prompt markers")


async def test_faq_fastpath_for_non_sql_tenant():
    tid = "alpha_devs_test_fp"
    kc._CACHE[tid] = {
        "text": "[Services] We offer AI ERP, SaaS, and ed-tech.\n[Packages] Custom builds.",
        "expires_at": 9e12,
        "fetched_at": 0,
        "org_name": "Alpha Devs",
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
    assert ans is not None
    assert "AI ERP" in ans or "ed-tech" in ans or "SaaS" in ans
    kc.invalidate_knowledge(tid)
    print("✓ company FAQ uses knowledge for non-SQL tenant")


async def test_mapped_inventory_uses_catalog_not_services_script():
    tid = "construct_test"
    kc._CACHE[tid] = {
        "text": "[Services] Generic company blurb that should NOT answer productions.",
        "expires_at": 9e12,
        "fetched_at": 0,
        "org_name": "Construct",
    }
    cc._CACHE[tid] = {
        "text": "Productions: Hamlet, Macbeth. Sets: Forest, Castle.",
        "expires_at": time.time() + 600,
        "fetched_at": time.time(),
        "org_name": "Construct",
    }
    with patch(
        "backend.integrations.voice_fastpath.is_inventory_question_for_tenant",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.integrations.voice_fastpath.tenant_has_sql_inventory",
        new=AsyncMock(return_value=True),
    ):
        ans = await try_voice_faq_answer(tid, "what productions do you have?")
    assert ans is not None
    assert "Hamlet" in ans or "live data" in ans.lower()
    assert "Generic company blurb" not in ans
    kc.invalidate_knowledge(tid)
    cc.invalidate_catalog(tid)
    print("✓ tenant-mapped inventory answered from catalog, not services script")


async def test_cold_catalog_falls_through_for_sql_tenant():
    tid = "construct_cold"
    kc._CACHE[tid] = {
        "text": "[Services] Same script every time.",
        "expires_at": 9e12,
        "fetched_at": 0,
        "org_name": "Construct",
    }
    cc.invalidate_catalog(tid)
    with patch(
        "backend.integrations.voice_fastpath.is_inventory_question_for_tenant",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.integrations.voice_fastpath.tenant_has_sql_inventory",
        new=AsyncMock(return_value=True),
    ):
        ans = await try_voice_faq_answer(tid, "tell me about your sets")
    assert ans is None  # full agent + this tenant's mapped tables
    kc.invalidate_knowledge(tid)
    print("✓ cold catalog → agent/tools path for SQL tenant")


async def test_non_faq_returns_none():
    with patch(
        "backend.integrations.voice_fastpath.is_inventory_question_for_tenant",
        new=AsyncMock(return_value=False),
    ), patch(
        "backend.integrations.voice_fastpath.tenant_has_sql_inventory",
        new=AsyncMock(return_value=False),
    ):
        ans = await try_voice_faq_answer("t", "book an appointment tomorrow at 3")
    assert ans is None
    print("✓ non-FAQ falls through to full agent")


async def main():
    test_spoken_stops_at_prompt_markers()
    await test_faq_fastpath_for_non_sql_tenant()
    await test_mapped_inventory_uses_catalog_not_services_script()
    await test_cold_catalog_falls_through_for_sql_tenant()
    await test_non_faq_returns_none()
    print("\nAll voice fast-path tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
