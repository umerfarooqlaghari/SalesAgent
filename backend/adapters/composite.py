from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.adapters.base import POSAdapter

logger = logging.getLogger(__name__)


class CompositePOSAdapter:
    """Query multiple inventory sources in priority order; merge product lists."""

    def __init__(self, adapters: List[tuple[int, str, POSAdapter]]):
        self.adapters = sorted(adapters, key=lambda x: x[0])

    async def list_products(self, query: Optional[str] = None) -> str:
        sections: List[str] = []
        for _prio, label, adapter in self.adapters:
            try:
                result = await adapter.list_products(query)
                if result and "No products found" not in result:
                    sections.append(f"[{label}]\n{result}")
            except Exception as e:
                logger.warning("Inventory source %s failed list_products: %s", label, e)
                sections.append(f"[{label}] Error: {e}")

        if not sections:
            return "No products found across configured inventory sources."
        return "\n\n".join(sections)

    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str:
        last_err = "No order found."
        for _prio, label, adapter in self.adapters:
            try:
                result = await adapter.get_order_status(order_id, customer_email, customer_phone)
                if "No order found" not in result and "Error:" not in result[:20]:
                    return result
                last_err = result
            except Exception as e:
                last_err = str(e)
        return last_err

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        for _prio, label, adapter in self.adapters:
            try:
                found = await adapter.lookup_product(product_name)
                if found:
                    return found
            except Exception as e:
                logger.warning("Inventory source %s failed lookup: %s", label, e)
        return None

    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int:
        for _prio, label, adapter in self.adapters:
            try:
                return await adapter.create_order(
                    product_name, customer_email, customer_phone, total_price
                )
            except PermissionError:
                continue
            except Exception as e:
                logger.warning("Inventory source %s failed create_order: %s", label, e)
        raise PermissionError("No writable inventory source configured for order creation.")

    async def cancel_order(self, order_id: int) -> bool:
        for _prio, _label, adapter in self.adapters:
            try:
                if await adapter.cancel_order(order_id):
                    return True
            except Exception:
                continue
        return False
