"""
Latency path tests — cache-warm tool skip + embed session does not await SQL.

Run: .venv/bin/python -m backend.tests.test_latency_path
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.agent.graph import _needs_tools
from backend.integrations import catalog_cache as cc
from backend.tenant.context import IntegrationConfigs, TenantContext, TenantSettings


def test_needs_tools_skips_when_cache_warm():
    assert (
        _needs_tools(
            "What productions do you have?",
            has_fact_cache=True,
            has_catalog_cache=True,
            inventory_intent=True,
        )
        is False
    )
    assert _needs_tools("Tell me about your company", has_fact_cache=True) is False
    print("✓ positive: catalog/FAQ warm → skip tools")


def test_needs_tools_required_without_cache():
    # Tenant-mapped inventory with no catalog must still tool-call SQL
    assert (
        _needs_tools(
            "What productions do you have?",
            has_fact_cache=True,
            has_catalog_cache=False,
            inventory_intent=True,
        )
        is True
    )
    print("✓ negative: inventory without catalog → tools required")


def test_needs_tools_actions_always():
    assert _needs_tools("Book a meeting tomorrow", has_fact_cache=True) is True
    assert _needs_tools("I want to buy this package", has_fact_cache=True) is True
    assert _needs_tools("Can I speak to a human?", has_fact_cache=True) is True
    print("✓ positive: action keywords still force tools with cache")


async def test_embed_session_returns_before_slow_warmup():
    """
    create_embed_session must return Vapi keys without waiting for catalog SQL.
    Simulates the Alpha Devs 'Call Now' path.
    """
    from backend.main import create_embed_session
    import backend.main as main_mod

    tenant = TenantContext(
        tenant_id="alpha_devs_test",
        org_name="Alpha Devs",
        settings=TenantSettings(),
        integrations=IntegrationConfigs(),
        status="active",
    )
    cc.invalidate_catalog(tenant.tenant_id)
    main_mod.settings.VAPI_PUBLIC_KEY = "pk_test_vapi"
    main_mod.settings.VAPI_ASSISTANT_ID = "asst_test_123"

    # If the route mistakenly awaited warmup, this 1.5s sleep would blow the budget.
    async def slow_warmup(tenant_id: str, force: bool = False):
        await asyncio.sleep(1.5)
        return {"ok": True, "cached": False, "chars": 10}

    with patch("backend.main.register_voice_session", new=AsyncMock()), patch(
        "backend.integrations.catalog_cache.schedule_warmup"
    ) as scheduled, patch(
        "backend.integrations.catalog_cache.warmup_catalog", side_effect=slow_warmup
    ), patch(
        "backend.integrations.knowledge_cache.warmup_knowledge",
        new=AsyncMock(return_value={"ok": True, "cached": True, "chars": 50}),
    ), patch(
        "backend.integrations.knowledge_cache.get_cached_knowledge",
        return_value="[Services] Fast FAQ",
    ):
        t0 = time.perf_counter()
        body = await create_embed_session(data={}, tenant=tenant)
        elapsed_ms = (time.perf_counter() - t0) * 1000

    scheduled.assert_called()
    assert body["ok"] is True
    assert body["tenant_id"] == "alpha_devs_test"
    assert body["vapi_public_key"] == "pk_test_vapi"
    assert body["vapi_assistant_id"] == "asst_test_123"
    assert body["metadata"]["tenant_id"] == "alpha_devs_test"
    assert body["metadata"]["console_thread_id"]
    # Knowledge is ready; SQL catalog may still be warming in background
    assert body["warmup"]["status"] == "ready"
    assert elapsed_ms < 500, f"embed/session too slow ({elapsed_ms:.0f}ms) — SQL warmup likely blocking"
    print(f"✓ latency: embed/session non-blocking for SQL ({elapsed_ms:.0f}ms)")


async def test_cached_catalog_injected_flags_ready():
    from backend.main import create_embed_session
    import backend.main as main_mod

    tenant = TenantContext(
        tenant_id="tenant_ready",
        org_name="Ready Org",
        settings=TenantSettings(),
        integrations=IntegrationConfigs(),
        status="active",
    )
    cc._CACHE[tenant.tenant_id] = {
        "text": "Catalog: Product A, Product B",
        "expires_at": time.time() + 600,
        "fetched_at": time.time(),
        "org_name": "Ready Org",
    }
    main_mod.settings.VAPI_PUBLIC_KEY = "pk_v"
    main_mod.settings.VAPI_ASSISTANT_ID = "asst_v"

    with patch("backend.main.register_voice_session", new=AsyncMock()), patch(
        "backend.integrations.catalog_cache.schedule_warmup"
    ):
        body = await create_embed_session(
            data={"console_thread_id": "embed_abc"},
            tenant=tenant,
        )

    cc.invalidate_catalog(tenant.tenant_id)
    assert body["warmup"]["cached"] is True
    assert body["warmup"]["status"] == "ready"
    assert body["warmup"]["chars"] > 0
    assert body["console_thread_id"] == "embed_abc"
    print("✓ positive: warm cache reported ready on embed/session")


async def main():
    test_needs_tools_skips_when_cache_warm()
    test_needs_tools_required_without_cache()
    test_needs_tools_actions_always()
    await test_embed_session_returns_before_slow_warmup()
    await test_cached_catalog_injected_flags_ready()
    print("\nAll latency path tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
