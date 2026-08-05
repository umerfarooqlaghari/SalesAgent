import logging
from typing import Any, Dict, List

from adapter_hub.adapters.base import Connector
from adapter_hub.adapters.canonical import Product, Customer, Order, OrderItem

logger = logging.getLogger(__name__)

class ERPConnector(Connector):
    """
    Enterprise Resource Planning (ERP) Adapter.
    Mock interface illustrating plug-and-play ERP connection and mapping.
    """
    
    def __init__(self, config: Dict[str, Any], tenant_id: str, agent_id: str):
        super().__init__(config, tenant_id, agent_id)
        self.api_endpoint = config.get("api_endpoint", "https://erp.example.com/api/v1")
        self.auth_token = config.get("auth_token", "")

    async def test_connection(self) -> bool:
        # Simplistic validation
        if not self.auth_token:
            raise ConnectionError("ERP Authentication Token is missing")
        return True

    async def discover_schema(self) -> List[Dict[str, Any]]:
        """
        Introspect ERP modules.
        """
        return [
            {
                "name": "erp_inventory_items",
                "columns": [
                    {"name": "item_id", "type": "string"},
                    {"name": "description", "type": "string"},
                    {"name": "unit_cost", "type": "number"},
                    {"name": "qty_available", "type": "number"}
                ]
            },
            {
                "name": "erp_accounts",
                "columns": [
                    {"name": "account_num", "type": "string"},
                    {"name": "company_name", "type": "string"},
                    {"name": "primary_contact_email", "type": "string"}
                ]
            },
            {
                "name": "erp_invoices",
                "columns": [
                    {"name": "invoice_id", "type": "string"},
                    {"name": "customer_num", "type": "string"},
                    {"name": "invoice_total", "type": "number"},
                    {"name": "line_items_json", "type": "string"}
                ]
            }
        ]

    async def sync_data(self, whitelist: Dict[str, Any]) -> List[Any]:
        """
        Pull data from mock ERP modules.
        """
        canonical_entities = []
        
        # 1. Products (from erp_inventory_items)
        if "products" in whitelist:
            canonical_entities.extend([
                Product(
                    id="erp_item_99",
                    name="Industrial Valve A-1",
                    price=450.00,
                    stock_quantity=45,
                    description="Heavy duty hydraulic valve",
                    category="Hardware",
                    raw_metadata={"item_id": "erp_item_99", "vendor": "ValvesInc"}
                )
            ])
            
        # 2. Customers (from erp_accounts)
        if "customers" in whitelist:
            canonical_entities.extend([
                Customer(
                    id="erp_acc_411",
                    name="Alice ERP Manager",
                    email="alice@erpcorp.com",
                    phone="555-erp-1",
                    company="ERPCorp Inc.",
                    status="active",
                    raw_metadata={"account_num": "erp_acc_411"}
                )
            ])
            
        # 3. Orders (from erp_invoices)
        if "orders" in whitelist:
            canonical_entities.extend([
                Order(
                    id="erp_inv_5001",
                    customer_id="erp_acc_411",
                    customer_email="alice@erpcorp.com",
                    status="completed",
                    total_price=450.00,
                    items=[
                        OrderItem(
                            product_id="erp_item_99",
                            product_name="Industrial Valve A-1",
                            quantity=1,
                            unit_price=450.00
                        )
                    ],
                    raw_metadata={"invoice_id": "erp_inv_5001"}
                )
            ])
            
        return canonical_entities
