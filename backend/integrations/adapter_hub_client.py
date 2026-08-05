"""HTTP client for the Adapter-Hub microservice (schema sync + RAG)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

AGENT_ID = "sales_agent"


def _headers(tenant_id: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-API-Key": settings.ADAPTER_HUB_MASTER_KEY,
        "X-Tenant-ID": tenant_id,
        "X-Agent-ID": AGENT_ID,
    }


def _base() -> str:
    return (settings.ADAPTER_HUB_URL or "").rstrip("/")


def mapped_tables_to_whitelist(mapped_tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert Admin UI mapped_tables into adapter_hub whitelist shape."""
    tables: List[Dict[str, Any]] = []
    whitelist: Dict[str, Any] = {"tables": tables}

    for mt in mapped_tables or []:
        if mt.get("enabled") is False:
            continue
        table = mt.get("table")
        if not table:
            continue
        cols = mt.get("columns") or {}
        if isinstance(cols, list):
            cols = {str(c): str(c) for c in cols}
        if not cols:
            continue

        role = (mt.get("role") or "custom").lower()
        label = mt.get("label") or table
        entry = {"table": table, "label": label, "columns": cols, "role": role}
        tables.append(entry)

        # Also populate canonical slots when role matches
        if role == "products" and "products" not in whitelist:
            whitelist["products"] = {
                "table": table,
                "columns": {
                    "id": cols.get("id") or cols.get("sku") or next(iter(cols.values())),
                    "name": cols.get("name") or cols.get("title") or next(iter(cols.values())),
                    "price": cols.get("price") or "0",
                    "stock_quantity": cols.get("stock") or cols.get("stock_quantity") or "0",
                    "description": cols.get("description") or cols.get("details"),
                },
            }
        elif role in ("orders",) and "orders" not in whitelist:
            whitelist["orders"] = {
                "table": table,
                "columns": {
                    "id": cols.get("id") or next(iter(cols.values())),
                    "customer_email": cols.get("email") or cols.get("customer_email") or "",
                    "customer_phone": cols.get("phone"),
                    "status": cols.get("status") or "Pending",
                    "total_price": cols.get("total") or cols.get("total_price") or "0",
                    "items": cols.get("items") or cols.get("description") or cols.get("name") or "",
                },
            }

    return whitelist


async def hub_available() -> bool:
    base = _base()
    if not base or not settings.ADAPTER_HUB_ENABLED:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/health")
            return r.status_code == 200
    except Exception:
        return False


async def register_connection(tenant_id: str, provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    base = _base()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{base}/connections/register",
            headers=_headers(tenant_id),
            json={"provider": provider, "config": config},
        )
        r.raise_for_status()
        return r.json()


async def save_whitelist(tenant_id: str, whitelist: Dict[str, Any]) -> Dict[str, Any]:
    base = _base()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{base}/schema/whitelist",
            headers=_headers(tenant_id),
            json={"whitelist": whitelist},
        )
        r.raise_for_status()
        return r.json()


async def trigger_sync(tenant_id: str) -> Dict[str, Any]:
    base = _base()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{base}/sync", headers=_headers(tenant_id))
        if r.status_code >= 400:
            return {"ok": False, "error": r.text}
        return r.json()


async def retrieve(tenant_id: str, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    base = _base()
    if not base or not settings.ADAPTER_HUB_ENABLED:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{base}/retrieve",
                headers=_headers(tenant_id),
                json={"query": query, "top_k": top_k, "min_score": 0.02},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("results") or []
    except Exception as e:
        logger.debug("Adapter-hub retrieve failed for %s: %s", tenant_id, e)
        return []


async def sync_tenant_inventory(
    tenant_id: str,
    provider: str,
    resolved_config: Dict[str, Any],
    mapped_tables: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Register Postgres connection, push whitelist, and sync rows into hub RAG."""
    if not settings.ADAPTER_HUB_ENABLED:
        return {"ok": False, "skipped": True, "error": "Adapter-hub disabled"}
    if not await hub_available():
        return {"ok": False, "skipped": True, "error": "Adapter-hub unavailable"}

    whitelist = mapped_tables_to_whitelist(mapped_tables)
    if not whitelist.get("tables"):
        return {"ok": False, "error": "No mapped tables to sync"}

    # Strip secrets already resolved — pass plaintext for hub encrypt
    hub_config = {
        "host": resolved_config.get("host"),
        "port": resolved_config.get("port") or 5432,
        "database": resolved_config.get("database"),
        "username": resolved_config.get("username"),
        "password": resolved_config.get("password"),
        "schema": resolved_config.get("schema") or "public",
        "ssl": resolved_config.get("ssl", True),
    }

    await register_connection(tenant_id, provider, hub_config)
    await save_whitelist(tenant_id, whitelist)
    sync_result = await trigger_sync(tenant_id)
    return sync_result
