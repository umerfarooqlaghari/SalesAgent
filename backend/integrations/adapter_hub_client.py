"""HTTP client for the Adapter-Hub microservice (schema sync + RAG)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.config import settings


def _derive(tenant_id: str) -> str:
    """Per-tenant Adapter-Hub key. Mirrors adapter_hub/auth/tenant_keys.py."""
    import base64
    import hashlib
    import hmac

    digest = hmac.new(
        settings.ADAPTER_HUB_MASTER_KEY.encode("utf-8"),
        b"adapter-hub-tenant-v1:" + (tenant_id or "").encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return "ahk_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

logger = logging.getLogger(__name__)

AGENT_ID = "sales_agent"


def _headers(tenant_id: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        # S10: send the key DERIVED for this tenant, not the shared master key.
        # The hub recomputes it from the claimed tenant id, so a caller holding
        # one tenant's key cannot act as another.
        "X-API-Key": _derive(tenant_id),
        "X-Tenant-ID": tenant_id,
        "X-Agent-ID": AGENT_ID,
    }


def _base() -> str:
    return (settings.ADAPTER_HUB_URL or "").rstrip("/")


def _assert_safe_transport(base: str) -> None:
    """
    A23: adapter-hub receives a tenant's plaintext DB password over this
    connection (see hub_config below). Refuse anything but HTTPS or an
    explicit loopback address so that config can never traverse a plaintext
    hop to a non-local host.
    """
    from urllib.parse import urlparse

    parsed = urlparse(base)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise RuntimeError(
        f"Refusing to send tenant credentials to adapter-hub over an insecure transport: {base!r}"
    )


def _scrub(text: str, secret: Optional[str]) -> str:
    """Redact a known secret value (e.g. a DB password) out of error text."""
    if not secret:
        return text
    return text.replace(secret, "***")


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

        # A22: a missing column used to be defaulted to a literal "0"/"Pending"
        # string, so a tenant whose products table has no price column had
        # adapter-hub confidently answer "$0" instead of "unknown" for every
        # item.
        if role == "products" and "products" not in whitelist:
            product_cols = {
                "id": cols.get("id") or cols.get("sku") or next(iter(cols.values())),
                "name": cols.get("name") or cols.get("title") or next(iter(cols.values())),
                "price": cols.get("price"),
                "stock_quantity": cols.get("stock") or cols.get("stock_quantity"),
                "description": cols.get("description") or cols.get("details"),
            }
            whitelist["products"] = {
                "table": table,
                "columns": {k: v for k, v in product_cols.items() if v},
            }
        elif role in ("orders",) and "orders" not in whitelist:
            order_cols = {
                "id": cols.get("id") or next(iter(cols.values())),
                "customer_email": cols.get("email") or cols.get("customer_email"),
                "customer_phone": cols.get("phone"),
                "status": cols.get("status"),
                "total_price": cols.get("total") or cols.get("total_price"),
                "items": cols.get("items") or cols.get("description") or cols.get("name"),
            }
            whitelist["orders"] = {
                "table": table,
                "columns": {k: v for k, v in order_cols.items() if v},
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
        # P04: this is an optional enrichment on the voice critical path. A 10s
        # timeout here exceeded the entire turn budget on its own.
        async with httpx.AsyncClient(timeout=1.5) as client:
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

    password = resolved_config.get("password")
    _assert_safe_transport(_base())

    # Strip secrets already resolved — pass plaintext for hub encrypt
    hub_config = {
        "host": resolved_config.get("host"),
        "port": resolved_config.get("port") or 5432,
        "database": resolved_config.get("database"),
        "username": resolved_config.get("username"),
        "password": password,
        "schema": resolved_config.get("schema") or "public",
        "ssl": resolved_config.get("ssl", True),
    }

    try:
        await register_connection(tenant_id, provider, hub_config)
        await save_whitelist(tenant_id, whitelist)
        sync_result = await trigger_sync(tenant_id)
        return sync_result
    except Exception as e:
        # A23: an httpx error can embed the request URL/body (including the
        # password just sent) in its string representation — never let that
        # reach logs or an API response unscrubbed.
        raise RuntimeError(_scrub(str(e), password)) from None
