"""
Catalog cache unit tests — mocks inventory adapters (no MongoDB / SQL).
Positive + negative + TTL + latency (warmup reuse).

Run: python3 -m backend.tests.test_catalog_cache
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations import catalog_cache as cc


def _clear_cache() -> None:
    cc._CACHE.clear()
    cc._LOCKS.clear()


async def test_get_cached_empty():
    _clear_cache()
    assert cc.get_cached_catalog("tenant_x") is None
    print("✓ negative: empty cache miss")


async def test_warmup_positive_and_reuse():
    _clear_cache()
    tenant_id = "tenant_warm"

    from backend.adapters.factory import AdapterFactory

    fake_pos = MagicMock()
    fake_pos.list_products = AsyncMock(
        side_effect=[
            "Productions: Hamlet, Macbeth",
            "Sets: Forest, Castle",
            "Sets: Forest, Castle",
            "Products: Widget A",
        ]
    )
    fake_ctx = SimpleNamespace(org_name="Construct Scenery", tenant_id=tenant_id)

    with patch.object(AdapterFactory, "pos", return_value=fake_pos), patch(
        "backend.tenant.registry.get_tenant_by_id", new=AsyncMock(return_value=fake_ctx)
    ):
        t0 = time.perf_counter()
        first = await cc.warmup_catalog(tenant_id, force=True)
        first_ms = (time.perf_counter() - t0) * 1000

        assert first["ok"] is True
        assert first["cached"] is False
        assert first["chars"] > 0

        cached = cc.get_cached_catalog(tenant_id)
        assert cached is not None
        assert "Hamlet" in cached or "Widget" in cached

        t1 = time.perf_counter()
        second = await cc.warmup_catalog(tenant_id, force=False)
        second_ms = (time.perf_counter() - t1) * 1000

        assert second["ok"] is True
        assert second["cached"] is True
        # Reuse must be near-instant (no SQL round-trips)
        assert second_ms < 50, f"cache hit too slow: {second_ms:.1f}ms"
        # Second call should not hit list_products again when cache warm
        assert fake_pos.list_products.await_count == 4

    print(f"✓ positive: warmup + cache hit ({first_ms:.1f}ms → {second_ms:.1f}ms)")


async def test_warmup_tenant_missing():
    _clear_cache()
    with patch(
        "backend.tenant.registry.get_tenant_by_id", new=AsyncMock(return_value=None)
    ):
        result = await cc.warmup_catalog("missing_tenant", force=True)
    assert result["ok"] is False
    assert "not found" in result.get("error", "").lower()
    assert cc.get_cached_catalog("missing_tenant") is None
    print("✓ negative: tenant not found")


async def test_warmup_empty_inventory():
    _clear_cache()
    tenant_id = "tenant_empty"
    from backend.adapters.factory import AdapterFactory

    fake_pos = MagicMock()
    fake_pos.list_products = AsyncMock(return_value="")
    fake_ctx = SimpleNamespace(org_name="Empty Co", tenant_id=tenant_id)

    with patch.object(AdapterFactory, "pos", return_value=fake_pos), patch(
        "backend.tenant.registry.get_tenant_by_id", new=AsyncMock(return_value=fake_ctx)
    ):
        result = await cc.warmup_catalog(tenant_id, force=True)

    assert result["ok"] is False
    assert "No catalog" in result.get("error", "")
    assert cc.get_cached_catalog(tenant_id) is None
    print("✓ negative: empty inventory")


async def test_ttl_expiry():
    _clear_cache()
    tenant_id = "tenant_ttl"
    cc._CACHE[tenant_id] = {
        "text": "stale catalog",
        "expires_at": time.time() - 1,
        "fetched_at": time.time() - 100,
        "org_name": "X",
    }
    assert cc.get_cached_catalog(tenant_id) is None
    assert tenant_id not in cc._CACHE
    print("✓ negative: TTL expiry drops cache")


async def test_invalidate():
    _clear_cache()
    tenant_id = "tenant_inv"
    cc._CACHE[tenant_id] = {
        "text": "live catalog",
        "expires_at": time.time() + 600,
        "fetched_at": time.time(),
        "org_name": "X",
    }
    assert cc.get_cached_catalog(tenant_id) == "live catalog"
    cc.invalidate_catalog(tenant_id)
    assert cc.get_cached_catalog(tenant_id) is None
    print("✓ positive: invalidate clears entry")


async def test_schedule_warmup_nonblocking():
    """schedule_warmup must not block the caller (voice call start path)."""
    _clear_cache()
    tenant_id = "tenant_bg"
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_warmup(tid: str, force: bool = False):
        started.set()
        await asyncio.sleep(0.2)
        finished.set()
        return {"ok": True, "cached": False, "chars": 1}

    with patch.object(cc, "warmup_catalog", side_effect=slow_warmup):
        t0 = time.perf_counter()
        cc.schedule_warmup(tenant_id)
        elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 50, f"schedule_warmup blocked caller for {elapsed_ms:.1f}ms"
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    print(f"✓ latency: schedule_warmup non-blocking ({elapsed_ms:.1f}ms return)")


async def main():
    await test_get_cached_empty()
    await test_warmup_positive_and_reuse()
    await test_warmup_tenant_missing()
    await test_warmup_empty_inventory()
    await test_ttl_expiry()
    await test_invalidate()
    await test_schedule_warmup_nonblocking()
    print("\nAll catalog cache tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
