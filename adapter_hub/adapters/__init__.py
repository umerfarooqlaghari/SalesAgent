from typing import Any, Dict

from adapter_hub.adapters.base import Connector


def get_connector(provider: str, config: Dict[str, Any], tenant_id: str, agent_id: str) -> Connector:
    prov = provider.lower()
    if prov in ("postgres", "postgresql", "sql", "sqlite"):
        from adapter_hub.adapters.postgres import PostgresConnector

        return PostgresConnector(config, tenant_id, agent_id)
    if prov == "shopify":
        from adapter_hub.adapters.shopify import ShopifyConnector

        return ShopifyConnector(config, tenant_id, agent_id)
    if prov == "erp":
        from adapter_hub.adapters.erp import ERPConnector

        return ERPConnector(config, tenant_id, agent_id)
    raise ValueError(f"Unsupported adapter provider: {provider}")
