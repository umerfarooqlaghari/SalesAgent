from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# A30: composite/routing code used to test raw adapter prose ("No order found"
# not in result) to decide what happened. Centralizing the sentinel here means
# a wording change in one adapter can't silently flip control flow elsewhere —
# still string-based (a full result dataclass would touch every adapter's
# public contract), but now there's exactly one definition to keep in sync.
NOT_FOUND_MARKER = "No order found"
ERROR_PREFIX = "Error:"


def is_not_found(result: str) -> bool:
    return NOT_FOUND_MARKER in (result or "")


def is_error(result: str) -> bool:
    return ERROR_PREFIX in (result or "")[:20]


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
