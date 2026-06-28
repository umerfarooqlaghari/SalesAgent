from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class ShopifyPOSAdapter:
    """Shopify Admin REST API — products and orders."""

    def __init__(self, config: Dict[str, Any], tenant: TenantContext):
        self.config = config
        self.tenant = tenant
        self.read_only = bool(config.get("read_only", True))
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

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{self._base_url()}{path}",
                headers=self._headers(),
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def test_connection(self) -> None:
        await self._get("/shop.json")

    async def list_products(self, query: Optional[str] = None) -> str:
        data = await self._get("/products.json", {"limit": 50})
        products = data.get("products") or []
        if query and query.strip().lower() not in {"product", "products", "all", "list", ""}:
            q = query.strip().lower()
            products = [p for p in products if q in (p.get("title") or "").lower()]

        if not products:
            return "No products found in Shopify."

        lines = []
        for p in products:
            variant = (p.get("variants") or [{}])[0]
            price = variant.get("price", "N/A")
            stock = variant.get("inventory_quantity", 0)
            lines.append(f"- {p.get('title')}: Price=${price}, In Stock={stock} ({p.get('body_html', '')[:80]})")
        return "Product Catalog (Shopify):\n" + "\n".join(lines)

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        data = await self._get("/products.json", {"limit": 50})
        q = product_name.strip().lower()
        for p in data.get("products") or []:
            if q in (p.get("title") or "").lower():
                variant = (p.get("variants") or [{}])[0]
                return {
                    "name": p.get("title"),
                    "price": f"${variant.get('price', '0')}",
                    "stock_quantity": int(variant.get("inventory_quantity") or 0),
                    "description": (p.get("body_html") or "")[:200],
                    "shopify_variant_id": variant.get("id"),
                }
        return None

    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str:
        if not customer_email and not customer_phone:
            return "Error: You must provide the customer's email or phone number to verify ownership."

        try:
            data = await self._get(f"/orders/{order_id}.json")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"No order found with ID: {order_id}"
            raise

        order = data.get("order") or {}
        email = (order.get("email") or "").lower()
        phone = (order.get("phone") or "").strip()
        email_match = customer_email and email == customer_email.lower().strip()
        phone_match = customer_phone and phone == customer_phone.strip()
        if not email_match and not phone_match:
            return "Security Error: Customer verification failed."

        items = ", ".join(
            f"{li.get('quantity')}x {li.get('title')}" for li in (order.get("line_items") or [])
        )
        return (
            f"Order #{order.get('id')} Details: Status={order.get('financial_status')}, "
            f"Items={items}, Total={order.get('total_price')}."
        )

    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int:
        if self.read_only:
            raise PermissionError("Shopify integration is read-only — enable write to create draft orders.")

        product = await self.lookup_product(product_name)
        if not product or not product.get("shopify_variant_id"):
            raise ValueError(f"Product not found in Shopify: {product_name}")

        payload = {
            "draft_order": {
                "email": customer_email,
                "phone": customer_phone,
                "line_items": [{"variant_id": product["shopify_variant_id"], "quantity": 1}],
            }
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self._base_url()}/draft_orders.json",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            draft = resp.json().get("draft_order") or {}
            return int(draft.get("id") or 0)

    async def cancel_order(self, order_id: int) -> bool:
        if self.read_only:
            return False
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self._base_url()}/orders/{order_id}/cancel.json",
                headers=self._headers(),
                json={},
            )
            return resp.status_code < 400
