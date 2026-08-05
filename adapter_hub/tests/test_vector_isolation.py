import pytest
from fastapi.testclient import TestClient

from adapter_hub.main import app
from adapter_hub.config import settings
from adapter_hub.adapters.canonical import Product, Customer
from adapter_hub.rag_retrievers.vector_db import vector_db_client
from adapter_hub.rag_retrievers.retriever import rag_retriever

client = TestClient(app)

@pytest.fixture(autouse=True)
def run_before_and_after_tests():
    # Setup: ensure Vector DB is clean
    vector_db_client.clear_database()
    yield
    # Teardown: clean up
    vector_db_client.clear_database()

@pytest.mark.asyncio
async def test_direct_vector_db_tenant_isolation():
    """
    Verify that tenant data inserted directly into the Vector DB
    is completely isolated and inaccessible from other tenants.
    """
    tenant_a = "tenant_alpha"
    tenant_b = "tenant_beta"
    agent_id = "agent_sales_bot"

    # Entities for Tenant A
    entities_a = [
        Product(id="prod_saas_enterprise", name="Enterprise Plan SaaS", price=999.0, stock_quantity=10, description="Enterprise suite for Tenant A"),
        Product(id="prod_saas_pro", name="Professional Plan SaaS", price=199.0, stock_quantity=50, description="Standard dashboard for Tenant A")
    ]

    # Entities for Tenant B (different products, overlapping keywords)
    entities_b = [
        Product(id="prod_widget_heavy", name="Heavy Duty Construction Widget", price=450.0, stock_quantity=5, description="Industrial hardware widget for Tenant B"),
        Product(id="prod_widget_light", name="Light Weight Aluminum Widget", price=85.0, stock_quantity=20, description="Small item widget for Tenant B")
    ]

    # Upsert data into respective namespaces
    await vector_db_client.upsert_entities(tenant_a, agent_id, entities_a)
    await vector_db_client.upsert_entities(tenant_b, agent_id, entities_b)

    # 1. Query as Tenant A for "SaaS" (Should match Tenant A's products)
    results_a = await vector_db_client.query_vector_space(tenant_a, agent_id, "SaaS", top_k=5)
    assert len(results_a) == 2
    assert all("Enterprise Plan" in r["text"] or "Professional Plan" in r["text"] for r in results_a)
    assert not any("Widget" in r["text"] for r in results_a)

    # 2. Query as Tenant A for "Widget" (Which only exists in Tenant B's space)
    # Even when querying Tenant B's terms, Tenant A MUST NEVER see Tenant B's documents.
    results_a_leak = await vector_db_client.query_vector_space(tenant_a, agent_id, "Widget", top_k=5)
    assert not any("Widget" in r["text"] or "prod_widget" in r["id"] for r in results_a_leak), "Security Failure: Tenant B's data leaked into Tenant A's search results!"

    # 3. Query as Tenant B for "Widget"
    results_b = await vector_db_client.query_vector_space(tenant_b, agent_id, "Widget", top_k=5)
    assert len(results_b) == 2
    assert all("Widget" in r["text"] for r in results_b)
    assert not any("SaaS" in r["text"] for r in results_b)

    # 4. Query as Tenant B for "SaaS" (Which only exists in Tenant A's space)
    results_b_leak = await vector_db_client.query_vector_space(tenant_b, agent_id, "SaaS", top_k=5)
    assert not any("SaaS" in r["text"] or "prod_saas" in r["id"] for r in results_b_leak), "Security Failure: Tenant A's data leaked into Tenant B's search results!"

@pytest.mark.asyncio
async def test_retriever_with_re_ranking_isolation():
    """
    Verify isolation behavior and correctness when querying via the high-level
    RAG Retriever service which applies terms overlap re-ranking.
    """
    tenant_a = "tenant_alpha"
    tenant_b = "tenant_beta"
    agent_id = "agent_sales_bot"

    # Entities for Tenant A
    entities_a = [
        Customer(id="cust_1", name="Alice Customer", email="alice@alpha.com", company="Alpha Analytics"),
    ]
    # Entities for Tenant B
    entities_b = [
        Customer(id="cust_2", name="Bob Customer", email="bob@beta.com", company="Beta Builders"),
    ]

    await vector_db_client.upsert_entities(tenant_a, agent_id, entities_a)
    await vector_db_client.upsert_entities(tenant_b, agent_id, entities_b)

    # Retrieval for Tenant A searching for "Customer"
    results_a = await rag_retriever.retrieve(tenant_a, agent_id, "Customer")
    assert len(results_a) == 1
    assert results_a[0]["payload"]["name"] == "Alice Customer"

    # Retrieval for Tenant B searching for "Customer"
    results_b = await rag_retriever.retrieve(tenant_b, agent_id, "Customer")
    assert len(results_b) == 1
    assert results_b[0]["payload"]["name"] == "Bob Customer"

def test_http_endpoint_auth_and_scoping_isolation():
    """
    Verify that the FastAPI routing and Middleware correctly intercept requests,
    validate API keys, and enforce tenant-scoping context parameters.
    """
    headers_valid_a = {
        "X-API-Key": settings.MASTER_API_KEY,
        "X-Tenant-ID": "tenant_alpha",
        "X-Agent-ID": "agent_sales_bot"
    }

    # 1. Missing API Key
    resp = client.post("/retrieve", json={"query": "test"}, headers={"X-Tenant-ID": "t1", "X-Agent-ID": "a1"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"

    # 2. Invalid API Key
    resp = client.post("/retrieve", json={"query": "test"}, headers={"X-API-Key": "bad-key", "X-Tenant-ID": "t1", "X-Agent-ID": "a1"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"

    # 3. Missing Tenant ID
    resp = client.post("/retrieve", json={"query": "test"}, headers={"X-API-Key": settings.MASTER_API_KEY, "X-Agent-ID": "a1"})
    assert resp.status_code == 400
    assert "Tenant ID is missing" in resp.json()["error"]

    # 4. Connection Register & Discovery isolation simulation
    # Register connection for Tenant A
    resp_reg = client.post(
        "/connections/register",
        json={
            "provider": "shopify",
            "config": {
                "shop_domain": "alpha-store.myshopify.com",
                "access_token": "mock-shopify-key"
            }
        },
        headers=headers_valid_a
    )
    assert resp_reg.status_code == 200
    assert resp_reg.json()["ok"] is True

    # Scan and Discover tables for Tenant A
    resp_disc = client.get("/schema/discover", headers=headers_valid_a)
    assert resp_disc.status_code == 200
    assert "products" in [t["name"] for t in resp_disc.json()["schema"]]

    # Save whitelist maps for Tenant A
    resp_white = client.post(
        "/schema/whitelist",
        json={
            "whitelist": {
                "products": {"table": "products", "columns": {"id": "id", "name": "title"}}
            }
        },
        headers=headers_valid_a
    )
    assert resp_white.status_code == 200

    # Trigger Sync for Tenant A (will pull mock shopify data and push to Tenant A namespace in Vector DB)
    resp_sync = client.post("/sync", headers=headers_valid_a)
    assert resp_sync.status_code == 200
    assert resp_sync.json()["synchronized_count"] > 0

    # Retrieve RAG results for Tenant A
    resp_ret = client.post("/retrieve", json={"query": "Shopify Red T-Shirt"}, headers=headers_valid_a)
    assert resp_ret.status_code == 200
    results = resp_ret.json()["results"]
    assert len(results) > 0
    assert "Shopify Red T-Shirt" in results[0]["text"]

    # Retrieve RAG results for Tenant B (who has nothing synced yet)
    headers_valid_b = {
        "X-API-Key": settings.MASTER_API_KEY,
        "X-Tenant-ID": "tenant_beta",
        "X-Agent-ID": "agent_sales_bot"
    }
    resp_ret_b = client.post("/retrieve", json={"query": "Shopify Red T-Shirt"}, headers=headers_valid_b)
    assert resp_ret_b.status_code == 200
    assert len(resp_ret_b.json()["results"]) == 0, "Security Failure: Tenant B retrieved Tenant A's synced data!"
