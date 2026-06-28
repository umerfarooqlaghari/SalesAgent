"""
Verify tenant data isolation (Phase 1).

Run from repo root with MongoDB configured:
  python3 -m backend.test_tenant_isolation
"""
import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import db_client, get_lead, list_leads, save_lead
from backend.tenant.registry import ensure_tenant_indexes, seed_default_tenant


async def main() -> None:
    db_client.connect()
    await ensure_tenant_indexes()
    await seed_default_tenant()

    tenant_a = "alpha_default"
    tenant_b = "isolation_test_tenant"
    thread_id = "isolation_probe_thread"

    await save_lead(tenant_a, thread_id, {"company": "Tenant A Corp", "status": "Test"})
    await save_lead(tenant_b, thread_id, {"company": "Tenant B Corp", "status": "Test"})

    lead_a = await get_lead(tenant_a, thread_id)
    lead_b = await get_lead(tenant_b, thread_id)

    assert lead_a and lead_a.get("company") == "Tenant A Corp", lead_a
    assert lead_b and lead_b.get("company") == "Tenant B Corp", lead_b

    leads_a = await list_leads(tenant_a)
    leads_b = await list_leads(tenant_b)
    assert not any(l.get("company") == "Tenant B Corp" for l in leads_a)
    assert not any(l.get("company") == "Tenant A Corp" for l in leads_b)

    print("Tenant isolation OK: same thread_id, different tenant_id → separate lead records.")

    db = db_client.db
    await db.leads.delete_many({"thread_id": thread_id, "tenant_id": {"$in": [tenant_a, tenant_b]}})
    db_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
