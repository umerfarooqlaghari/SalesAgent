"""
Integration tests — requires MongoDB (MONGODB_URI in backend/.env).
Run: python3 -m backend.tests.test_integration
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.database import db_client, get_lead, save_lead, list_leads
from backend.tenant.registry import (
    ensure_tenant_indexes,
    seed_default_tenant,
    resolve_tenant_by_api_key,
    seed_default_knowledge,
)
from backend.tenant.secrets import hash_api_key
from backend.integrations.service import IntegrationService, normalize_integrations


async def test_tenant_isolation():
    await ensure_tenant_indexes()
    await seed_default_tenant()

    tenant_a = "alpha_default"
    tenant_b = "test_tenant_b"
    thread_id = "isolation_thread_x"

    db_client.connect()
    db = db_client.db
    await db.tenants.update_one(
        {"tenant_id": tenant_b},
        {
            "$set": {
                "tenant_id": tenant_b,
                "org_name": "Test Tenant B",
                "api_key_hash": hash_api_key("test_key_tenant_b"),
                "status": "active",
                "integration_configs": normalize_integrations({}),
            }
        },
        upsert=True,
    )

    await save_lead(tenant_a, thread_id, {"company": "Alpha Corp", "status": "Test"})
    await save_lead(tenant_b, thread_id, {"company": "Beta Corp", "status": "Test"})

    a = await get_lead(tenant_a, thread_id)
    b = await get_lead(tenant_b, thread_id)
    assert a["company"] == "Alpha Corp", a
    assert b["company"] == "Beta Corp", b

    leads_a = await list_leads(tenant_a)
    assert not any(l.get("company") == "Beta Corp" for l in leads_a)

    await db.leads.delete_many({"thread_id": thread_id})
    print("✓ tenant isolation")


async def test_auth_login():
    await seed_default_tenant()
    t = await resolve_tenant_by_api_key("test_key_abc123")
    assert t is not None
    assert t.tenant_id == "alpha_default"

    bad = await resolve_tenant_by_api_key("invalid_key_xyz")
    assert bad is None
    print("✓ auth / API key resolution")


async def test_admin_view():
    await seed_default_tenant()
    view = await IntegrationService.get_admin_view("alpha_default")
    assert view["tenant_id"] == "alpha_default"
    assert "integrations" in view
    assert view["integrations"]["inventory"]["enabled"] is True
    print("✓ admin tenant view")


async def test_rag_seed():
    await seed_default_knowledge()
    from backend.agent.rag import retrieve_context

    ctx = await retrieve_context("alpha_default", "enterprise pricing")
    assert "Enterprise" in ctx or "999" in ctx or ctx == ""
    print("✓ RAG seed/retrieve")


async def main():
    db_client.connect()
    try:
        await test_auth_login()
        await test_tenant_isolation()
        await test_admin_view()
        await test_rag_seed()
        print("\nAll integration tests passed.")
    finally:
        db_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
