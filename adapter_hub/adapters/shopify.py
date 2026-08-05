import logging
from typing import Any, Dict, List
import httpx

from adapter_hub.adapters.base import Connector
from adapter_hub.adapters.canonical import Product, Customer, Order, OrderItem

logger = logging.getLogger(__name__)

class ShopifyConnector(Connector):
    """
    Shopify REST API Adapter. Standardizes Shopify Products,
    Customers, and Orders into the Canonical B2B Data Model.
    """
    
    def __init__(self, config: Dict[str, Any], tenant_id: str, agent_id: str):
        super().__init__(config, tenant_id, agent_id)
        domain = (config.get("shop_domain") or "").strip().replace("https://", "").replace("http://", "")
        if domain.endswith("/"):
            domain = domain[:-1]
        self.shop_domain = domain
        self.access_token = config.get("access_token") or ""
        self.api_version = config.get("api_version") or "2024-01"

    def _base_url(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}"

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    async def test_connection(self) -> bool:
        # Mock connectivity for test API keys
        if self.access_token == "mock-shopify-key" or "mock" in self.shop_domain:
            return True
            
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(
                    f"{self._base_url()}/shop.json",
                    headers=self._headers()
                )
                resp.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"Shopify connection test failed: {e}")
                raise ConnectionError(f"Shopify API connection failed: {e}")

    async def discover_schema(self) -> List[Dict[str, Any]]:
        """
        Shopify has a static API schema. We return the available standard 
        resources and their key fields.
        """
        return [
            {
                "name": "products",
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "title", "type": "string"},
                    {"name": "body_html", "type": "string"},
                    {"name": "variants", "type": "array"},
                    {"name": "vendor", "type": "string"}
                ]
            },
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "first_name", "type": "string"},
                    {"name": "last_name", "type": "string"},
                    {"name": "email", "type": "string"},
                    {"name": "phone", "type": "string"},
                    {"name": "state", "type": "string"}
                ]
            },
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "email", "type": "string"},
                    {"name": "phone", "type": "string"},
                    {"name": "financial_status", "type": "string"},
                    {"name": "total_price", "type": "string"},
                    {"name": "line_items", "type": "array"}
                ]
            }
        ]

    async def sync_data(self, whitelist: Dict[str, Any]) -> List[Any]:
        """
        Queries Shopify endpoints and parses returned JSON payloads 
        into canonical models.
        """
        # If mock integration, return stubbed data for tests
        if self.access_token == "mock-shopify-key" or "mock" in self.shop_domain:
            return self._sync_mock_data(whitelist)

        canonical_entities = []
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Sync Products
            if "products" in whitelist:
                try:
                    resp = await client.get(f"{self._base_url()}/products.json?limit=50", headers=self._headers())
                    resp.raise_for_status()
                    products_data = resp.json().get("products") or []
                    for p in products_data:
                        variant = (p.get("variants") or [{}])[0]
                        prod = Product(
                            id=str(p.get("id")),
                            name=str(p.get("title", "")),
                            price=float(variant.get("price") or 0.0),
                            stock_quantity=int(variant.get("inventory_quantity") or 0),
                            description=p.get("body_html"),
                            category=p.get("product_type"),
                            raw_metadata=p
                        )
                        canonical_entities.append(prod)
                except Exception as e:
                    logger.warning(f"Failed to sync Shopify products: {e}")

            # 2. Sync Customers
            if "customers" in whitelist:
                try:
                    resp = await client.get(f"{self._base_url()}/customers.json?limit=50", headers=self._headers())
                    resp.raise_for_status()
                    customers_data = resp.json().get("customers") or []
                    for c in customers_data:
                        cust = Customer(
                            id=str(c.get("id")),
                            name=f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or "Unknown",
                            email=str(c.get("email") or ""),
                            phone=c.get("phone"),
                            company=c.get("default_address", {}).get("company") if c.get("default_address") else None,
                            status=c.get("state") or "active",
                            raw_metadata=c
                        )
                        canonical_entities.append(cust)
                except Exception as e:
                    logger.warning(f"Failed to sync Shopify customers: {e}")

            # 3. Sync Orders
            if "orders" in whitelist:
                try:
                    resp = await client.get(f"{self._base_url()}/orders.json?limit=50&status=any", headers=self._headers())
                    resp.raise_for_status()
                    orders_data = resp.json().get("orders") or []
                    for o in orders_data:
                        items_list = []
                        for item in o.get("line_items") or []:
                            items_list.append(OrderItem(
                                product_id=str(item.get("product_id") or ""),
                                product_name=str(item.get("title") or "Item"),
                                quantity=int(item.get("quantity") or 1),
                                unit_price=float(item.get("price") or 0.0)
                            ))
                            
                        ord = Order(
                            id=str(o.get("id")),
                            customer_id=str(o.get("customer", {}).get("id") or "") if o.get("customer") else None,
                            customer_email=str(o.get("email") or ""),
                            customer_phone=o.get("phone"),
                            status=str(o.get("financial_status") or "Pending"),
                            total_price=float(o.get("total_price") or 0.0),
                            items=items_list,
                            raw_metadata=o
                        )
                        canonical_entities.append(ord)
                except Exception as e:
                    logger.warning(f"Failed to sync Shopify orders: {e}")

        return canonical_entities

    def _sync_mock_data(self, whitelist: Dict[str, Any]) -> List[Any]:
        """Provides mock B2B data for dry-runs and automated testing."""
        canonical_entities = []
        if "products" in whitelist:
            canonical_entities.extend([
                Product(id="sh_p_1", name="Shopify Red T-Shirt", price=25.0, stock_quantity=100, description="100% cotton", raw_metadata={"mock": True}),
                Product(id="sh_p_2", name="Shopify Blue Mug", price=12.5, stock_quantity=200, description="Ceramic mug", raw_metadata={"mock": True}),
            ])
        if "customers" in whitelist:
            canonical_entities.extend([
                Customer(id="sh_c_1", name="John Shopify", email="john@shopify-test.com", phone="123456", company="Shopify Test Corp", status="active", raw_metadata={"mock": True})
            ])
        if "orders" in whitelist:
            canonical_entities.extend([
                Order(
                    id="sh_o_1",
                    customer_email="john@shopify-test.com",
                    status="paid",
                    total_price=37.5,
                    items=[
                        OrderItem(product_id="sh_p_1", product_name="Shopify Red T-Shirt", quantity=1, unit_price=25.0),
                        OrderItem(product_id="sh_p_2", product_name="Shopify Blue Mug", quantity=1, unit_price=12.5)
                    ],
                    raw_metadata={"mock": True}
                )
            ])
        return canonical_entities
