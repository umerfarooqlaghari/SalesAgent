"""
Embed / widget API tests with mocked auth + agent (no live Mongo/LLM).

Positive: pk auth → session + query
Negative: missing auth, bad body, pk blocked from admin routes

Run: .venv/bin/python -m backend.tests.test_embed_api
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException

from backend.auth.dependencies import require_secret_tenant
from backend.auth.security import generate_publishable_key, is_publishable_key
from backend.tenant.context import IntegrationConfigs, TenantContext, TenantSettings
from backend.tenant.key_scope import set_key_scope


def _tenant(tid: str = "alpha_devs_c12c3774", org: str = "Alpha Devs") -> TenantContext:
    return TenantContext(
        tenant_id=tid,
        org_name=org,
        settings=TenantSettings(company_description="AI agency"),
        integrations=IntegrationConfigs(),
        status="active",
    )


def test_publishable_key_format():
    pk = generate_publishable_key()
    assert pk.startswith("pk_live_")
    assert is_publishable_key(pk) is True
    assert is_publishable_key("sk_live_abc") is False
    assert is_publishable_key("") is False
    print("✓ positive/negative: publishable key format")


async def test_require_secret_blocks_publishable_scope():
    set_key_scope("publishable")
    try:
        await require_secret_tenant(tenant=_tenant())
        raise AssertionError("expected 403")
    except HTTPException as e:
        assert e.status_code == 403
        assert "Publishable" in e.detail
    finally:
        set_key_scope("secret")
    print("✓ negative: publishable scope blocked from secret routes")


async def test_embed_session_positive():
    from backend.main import create_embed_session
    import backend.main as main_mod

    tenant = _tenant()
    main_mod.settings.VAPI_PUBLIC_KEY = "vapi_pk"
    main_mod.settings.VAPI_ASSISTANT_ID = "asst_1"

    with patch("backend.main.register_voice_session", new=AsyncMock()) as reg, patch(
        "backend.integrations.catalog_cache.schedule_warmup"
    ) as warm:
        data = await create_embed_session(data={}, tenant=tenant)

    reg.assert_awaited()
    warm.assert_called()
    assert data["tenant_id"] == tenant.tenant_id
    assert data["org_name"] == "Alpha Devs"
    assert data["vapi_public_key"] == "vapi_pk"
    assert data["vapi_assistant_id"] == "asst_1"
    assert data["console_thread_id"]
    assert data["metadata"]["tenant_id"] == tenant.tenant_id
    print("✓ positive: embed/session returns Vapi + tenant metadata")


async def test_widget_query_positive_mocked_agent():
    from backend.main import execute_query_route

    tenant = _tenant()
    ai_msg = SimpleNamespace(type="ai", content="We build AI ERP and computer vision.")
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={"messages": [ai_msg]})

    with patch("backend.main.save_conversation_message", new=AsyncMock()), patch(
        "backend.main.get_agent_graph", new=AsyncMock(return_value=fake_graph)
    ), patch("backend.integrations.catalog_cache.schedule_warmup"):
        data = await execute_query_route(
            data={"question": "What do you build?", "context": "Alpha-Devs sales inquiry"},
            tenant=tenant,
        )

    assert data["status"] == "success"
    assert "AI ERP" in data["answer"]
    assert data["tenant_id"] == tenant.tenant_id
    fake_graph.ainvoke.assert_awaited()
    print("✓ positive: widget/query returns agent answer (mocked)")


async def test_widget_query_missing_question():
    from backend.main import execute_query_route

    try:
        await execute_query_route(data={"context": "no question here"}, tenant=_tenant())
        raise AssertionError("expected 400")
    except HTTPException as e:
        assert e.status_code == 400
    print("✓ negative: widget/query missing question → 400")


async def test_widget_query_message_alias():
    from backend.main import execute_query_route

    ai_msg = SimpleNamespace(type="ai", content="Alias path works.")
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={"messages": [ai_msg]})

    with patch("backend.main.save_conversation_message", new=AsyncMock()), patch(
        "backend.main.get_agent_graph", new=AsyncMock(return_value=fake_graph)
    ), patch("backend.integrations.catalog_cache.schedule_warmup"):
        data = await execute_query_route(data={"message": "hello"}, tenant=_tenant())

    assert data["answer"] == "Alias path works."
    print("✓ positive: message field alias for question")


async def test_require_secret_allows_secret_scope():
    set_key_scope("secret")
    out = await require_secret_tenant(tenant=_tenant())
    assert out.tenant_id == "alpha_devs_c12c3774"
    print("✓ positive: secret scope allowed on admin dependency")


async def main():
    test_publishable_key_format()
    await test_require_secret_blocks_publishable_scope()
    await test_require_secret_allows_secret_scope()
    await test_embed_session_positive()
    await test_widget_query_positive_mocked_agent()
    await test_widget_query_missing_question()
    await test_widget_query_message_alias()
    print("\nAll embed API tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
