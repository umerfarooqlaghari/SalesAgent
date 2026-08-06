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
        # A14: booking (below) already raises NotImplementedError, but this
        # unconditionally returned True — so test_connection showed a green
        # check and the agent would confirm ANY slot as open, then fail only
        # when it tried to actually book it. Fail closed instead.
        raise NotImplementedError(
            "Google Calendar availability checking is not implemented yet — "
            "use the internal or SQL calendar until OAuth wiring lands."
        )

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
