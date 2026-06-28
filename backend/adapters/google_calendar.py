"""Google Calendar adapter — token or service account from admin config."""
from __future__ import annotations

import logging
from typing import Any, Dict

from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class GoogleCalendarAdapter:
    def __init__(self, config: Dict[str, Any], tenant: TenantContext):
        self.config = config
        self.tenant = tenant
        self.calendar_id = config.get("calendar_id") or "primary"
        self.read_only = bool(config.get("read_only", False))

    async def check_availability(self, date_str: str, time_str: str) -> bool:
        # Full OAuth implementation deferred — test confirms config present
        if not self.config.get("access_token") and not self.config.get("service_account_json"):
            raise ValueError("Google Calendar requires access_token or service_account_json.")
        logger.info("Google Calendar availability check for %s %s (tenant=%s)", date_str, time_str, self.tenant.tenant_id)
        return True

    async def book_slot(
        self,
        name: str,
        email: str,
        phone: str,
        date_str: str,
        time_str: str,
        notes: str = "",
    ) -> str:
        if self.read_only:
            raise PermissionError("Google Calendar is read-only.")
        raise NotImplementedError("Google Calendar booking requires OAuth setup — use internal calendar for now.")
