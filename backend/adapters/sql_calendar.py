"""SQL-backed calendar adapter using table_map from admin config."""
from __future__ import annotations

from typing import Any, Dict

from backend.adapters.sql_pos import SqlConnection, _table_map
from backend.tenant.context import TenantContext


class SqlCalendarAdapter:
    def __init__(self, provider: str, config: Dict[str, Any], tenant: TenantContext):
        self.sql = SqlConnection(provider, config)
        self.table_map = _table_map(config)
        self.tenant = tenant

    async def check_availability(self, date_str: str, time_str: str) -> bool:
        table = self.table_map.get("appointments_table", "appointments")
        qt = self.sql._qualified(table)
        cols = self.table_map.get("appointments_columns") or {}
        from backend.adapters.sql_pos import _quote_ident

        date_c = _quote_ident(cols.get("date", "date"), self.sql.dialect)
        time_c = _quote_ident(cols.get("time", "time"), self.sql.dialect)
        status_c = _quote_ident(cols.get("status", "status"), self.sql.dialect)

        sql = f"SELECT 1 FROM {qt} WHERE {date_c} = :d AND {time_c} = :t AND {status_c} != :cancelled LIMIT 1"
        rows = await self.sql.fetch_all(sql, {"d": date_str, "t": time_str, "cancelled": "cancelled"})
        return len(rows) == 0

    async def book_slot(
        self,
        name: str,
        email: str,
        phone: str,
        date_str: str,
        time_str: str,
        notes: str = "",
    ) -> str:
        if self.sql.read_only:
            raise PermissionError("Calendar SQL source is read-only.")
        raise NotImplementedError("SQL calendar booking — configure write access and extend table_map.")
