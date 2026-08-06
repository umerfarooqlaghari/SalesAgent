import os
import sys
import pytest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations.query_cache import (
    get_query_cache,
    set_query_cache,
    invalidate_tenant_query_cache,
    _IN_MEMORY_CACHE
)

@pytest.mark.asyncio
async def test_query_cache_in_memory_fallback():
    tenant_id = "test_tenant_cache"
    query_key = "q:products_ord:_em:_ph:"
    value = "Product A: $50 | Product B: $100"

    # Initially missing
    cached = await get_query_cache(tenant_id, query_key)
    assert cached is None

    # Set cache
    await set_query_cache(tenant_id, query_key, value, ttl=10)

    # Hit cache
    cached_after = await get_query_cache(tenant_id, query_key)
    assert cached_after == value

    # Invalidate cache
    await invalidate_tenant_query_cache(tenant_id)
    cached_invalidated = await get_query_cache(tenant_id, query_key)
    assert cached_invalidated is None

@pytest.mark.asyncio
async def test_query_cache_ttl_expiration():
    tenant_id = "test_tenant_ttl"
    query_key = "q:short_ttl"
    value = "Expired Data"

    # Set 1 second TTL
    await set_query_cache(tenant_id, query_key, value, ttl=1)
    
    # Immediately available
    assert await get_query_cache(tenant_id, query_key) == value

    # Wait 1.1s for expiration
    await asyncio.sleep(1.1)

    # Should be expired
    assert await get_query_cache(tenant_id, query_key) is None

@pytest.mark.asyncio
async def test_query_normalization_synonyms():
    tenant_id = "test_tenant_norm"
    
    # Store response under "SaaS Starter"
    await set_query_cache(tenant_id, "q:SaaS Starter", "Starter details", ttl=10)
    
    # Synonym questions ("what is SaaS Starter", "tell me about SaaS Starter") should all hit the exact same cache entry
    hit1 = await get_query_cache(tenant_id, "q:what is SaaS Starter")
    hit2 = await get_query_cache(tenant_id, "q:tell me about SaaS Starter")
    
    assert hit1 == "Starter details"
    assert hit2 == "Starter details"

