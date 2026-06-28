import sqlite3
from typing import Any, Dict, Optional

from backend.database import (
    SQLITE_DB_PATH,
    _cancel_sqlite_order,
    _create_sqlite_order,
    _lookup_product,
)
from backend.tenant.context import TenantContext


class EmptyPOSAdapter:
    """Returned when a tenant has no active inventory sources — avoids leaking the Alpha demo catalog."""

    def __init__(self, tenant: TenantContext):
        self.tenant = tenant

    async def list_products(self, query: Optional[str] = None) -> str:
        org = self.tenant.org_name or "this organization"
        return (
            f"No inventory source is connected for {org}. "
            "Ask an admin to connect Inventory & POS under Integrations, or offer human follow-up."
        )

    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str:
        return "Order lookup is not available — no inventory/POS integration is configured."

    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int:
        raise RuntimeError("Orders are not available without an inventory integration.")

    async def cancel_order(self, order_id: int) -> bool:
        return False

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        return None


class StubPOSAdapter:
    """Wraps the existing SQLite POS stub — default for all tenants until Shopify is connected."""

    def __init__(self, tenant: TenantContext):
        self.tenant = tenant

    async def list_products(self, query: Optional[str] = None) -> str:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        try:
            generic_terms = {"product", "products", "service", "services", "all", "list", "everything", ""}
            if query and query.strip().lower() not in generic_terms:
                cursor.execute(
                    "SELECT name, price, stock_quantity, description FROM products WHERE LOWER(name) LIKE LOWER(?)",
                    (f"%{query.strip()}%",),
                )
                rows = cursor.fetchall()
                if not rows:
                    cursor.execute("SELECT name, price, stock_quantity, description FROM products")
                    rows = cursor.fetchall()
            else:
                cursor.execute("SELECT name, price, stock_quantity, description FROM products")
                rows = cursor.fetchall()

            if not rows:
                return "No products found in the database."

            results = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Product Catalog:\n" + "\n".join(results)
        finally:
            conn.close()

    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str:
        if not customer_email and not customer_phone:
            return "Error: You must provide the customer's email or phone number to verify ownership."

        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, customer_email, customer_phone, status, total_price, items FROM orders WHERE id = ?",
                (order_id,),
            )
            row = cursor.fetchone()
            if not row:
                return f"No order found with ID: {order_id}"

            db_id, db_email, db_phone, db_status, db_total, db_items = row
            email_match = customer_email and customer_email.lower().strip() == db_email.lower().strip()
            phone_match = customer_phone and customer_phone.strip() == (db_phone or "").strip()
            if not email_match and not phone_match:
                return "Security Error: Customer verification failed."

            return f"Order #{db_id} Details: Status={db_status}, Items={db_items}, Total={db_total}."
        finally:
            conn.close()

    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int:
        return _create_sqlite_order(customer_email, customer_phone, product_name, total_price)

    async def cancel_order(self, order_id: int) -> bool:
        return _cancel_sqlite_order(order_id)

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        return _lookup_product(product_name)
