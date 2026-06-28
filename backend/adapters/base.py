from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class CRMAdapter(Protocol):
    async def search_company(self, company: str) -> str: ...
    async def sync_lead(self, lead_data: Dict[str, Any]) -> str: ...


@runtime_checkable
class POSAdapter(Protocol):
    async def list_products(self, query: Optional[str] = None) -> str: ...
    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str: ...
    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int: ...
    async def cancel_order(self, order_id: int) -> bool: ...
    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]: ...


@runtime_checkable
class CalendarAdapter(Protocol):
    async def check_availability(self, date_str: str, time_str: str) -> bool: ...
    async def book_slot(
        self,
        name: str,
        email: str,
        phone: str,
        date_str: str,
        time_str: str,
        notes: str = "",
    ) -> str: ...


class NoOpCRMAdapter:
    async def search_company(self, company: str) -> str:
        return f"No CRM integration configured. No record lookup for: {company}"

    async def sync_lead(self, lead_data: Dict[str, Any]) -> str:
        return "CRM sync skipped — no CRM adapter configured for this tenant."
