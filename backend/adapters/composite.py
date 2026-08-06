from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from backend.adapters.base import POSAdapter, is_error, is_not_found

logger = logging.getLogger(__name__)


class CompositePOSAdapter:
    """Query multiple inventory sources in priority order; merge product lists."""

    def __init__(self, adapters: List[tuple[int, str, POSAdapter]]):
        self.adapters = sorted(adapters, key=lambda x: x[0])

    async def list_products(self, query: Optional[str] = None) -> str:
        # A13: sources are independent reads — no reason to await them serially.
        async def _one(label: str, adapter: POSAdapter) -> Optional[str]:
            try:
                result = await adapter.list_products(query)
                if result and not is_not_found(result):
                    return f"[{label}]\n{result}"
            except Exception as e:
                logger.warning("Inventory source %s failed list_products: %s", label, e)
                return f"[{label}] Error: {e}"
            return None

        results = await asyncio.gather(
            *(_one(label, adapter) for _prio, label, adapter in self.adapters)
        )
        sections = [r for r in results if r]

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
                if not is_not_found(result) and not is_error(result):
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
        # A13: only skip to the next source for a source that flatly can't take
        # the write (read-only / unimplemented). Any other exception means we
        # genuinely don't know whether the INSERT committed before failing —
        # retrying a different source on that ambiguity is how a timeout after
        # a committed write turns into a second, duplicate order.
        for _prio, label, adapter in self.adapters:
            try:
                return await adapter.create_order(
                    product_name, customer_email, customer_phone, total_price
                )
            except (PermissionError, NotImplementedError):
                continue
            except Exception as e:
                logger.error(
                    "Inventory source %s failed create_order — not retrying another "
                    "source (risk of a duplicate order): %s", label, e,
                )
                raise
        raise PermissionError("No writable inventory source configured for order creation.")

    async def cancel_order(self, order_id: int) -> bool:
        for _prio, _label, adapter in self.adapters:
            try:
                if await adapter.cancel_order(order_id):
                    return True
            except Exception:
                continue
        return False
