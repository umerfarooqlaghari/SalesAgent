from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from backend.adapters.sql_pos import SqlConnection, _quote_ident, _table_map
from backend.integrations.table_map_util import get_mapped_tables
from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class SqlCRMAdapter:
    def __init__(self, provider: str, config: Dict[str, Any], tenant: TenantContext):
        self.sql = SqlConnection(provider, config)
        self.table_map = _table_map(config)
        self.mapped_tables = get_mapped_tables(self.table_map, "crm")

    async def _search_one_table(self, mapping: Dict[str, Any], query: str) -> Optional[str]:
        table = mapping.get("table")
        if not table:
            return None
        qt = self.sql._qualified(table)
        cols_map = mapping.get("columns") or {}
        if isinstance(cols_map, list):
            cols_map = {c: c for c in cols_map}

        search_cols = mapping.get("search_columns") or []
        if not search_cols and cols_map:
            search_cols = [cols_map.get("company") or list(cols_map.values())[0]]

        if not cols_map:
            return None

        select_parts = []
        field_names: list[str] = []
        seen = set()
        for logical, physical in cols_map.items():
            p = str(physical)
            if p not in seen:
                select_parts.append(_quote_ident(p, self.sql.dialect))
                field_names.append(str(logical))
                seen.add(p)

        if not select_parts:
            return None

        where_parts = []
        like_op = "ILIKE" if self.sql.dialect == "postgres" else "LIKE"
        for sc in search_cols:
            if sc:
                where_parts.append(f"{_quote_ident(str(sc), self.sql.dialect)} {like_op} :q")
        if not where_parts:
            return None

        where_sql = " OR ".join(where_parts)
        sql = f"SELECT {', '.join(select_parts)} FROM {qt} WHERE ({where_sql})"

        if self.sql.dialect == "sqlserver":
            sql = sql.replace("SELECT", "SELECT TOP 5", 1)
        else:
            sql += " LIMIT 5"

        rows = await self.sql.fetch_all(sql, {"q": f"%{query}%"})
        if not rows:
            return None

        label = mapping.get("label") or table
        lines = []
        for row in rows:
            pairs = ", ".join(f"{field_names[i]}={row[i]}" for i in range(len(row)))
            lines.append(f"  • {pairs}")
        return f"[{label}]\n" + "\n".join(lines)

    async def search_company(self, company: str) -> str:
        if self.mapped_tables:
            hits = []
            for mapping in self.mapped_tables:
                block = await self._search_one_table(mapping, company)
                if block:
                    hits.append(block)
            if hits:
                return "CRM matches:\n" + "\n".join(hits)
            return f"No existing CRM record found for: {company}"

        # Legacy single-table
        table = self.table_map.get("companies_table", "companies")
        return await self._search_one_table(
            {
                "table": table,
                "label": table,
                "columns": self.table_map.get("companies_columns")
                or {"company": "company", "status": "status", "fit": "fit"},
                "search_columns": [
                    (self.table_map.get("companies_columns") or {}).get("company", "company")
                ],
            },
            company,
        ) or f"No existing CRM record found for company: {company}"

    async def sync_lead(self, lead_data: Dict[str, Any]) -> str:
        if self.sql.read_only:
            return "CRM is read-only — lead sync skipped."
        return "SQL CRM sync not yet implemented for this provider."


class RestCRMAdapter:
    def __init__(self, config: Dict[str, Any], tenant: TenantContext):
        self.config = config
        self.tenant = tenant
        self.read_only = bool(config.get("read_only", True))

    async def search_company(self, company: str) -> str:
        base = (self.config.get("base_url") or "").rstrip("/")
        path = (self.config.get("search_path") or "/search").replace("{company}", company)
        url = f"{base}{path}"
        headers = {}
        auth_header = self.config.get("auth_header")
        auth_token = self.config.get("auth_token")
        if auth_header and auth_token:
            headers[auth_header] = auth_token

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return f"CRM response: {resp.text[:500]}"

    async def sync_lead(self, lead_data: Dict[str, Any]) -> str:
        if self.read_only:
            return "CRM is read-only — sync skipped."
        base = (self.config.get("base_url") or "").rstrip("/")
        path = self.config.get("sync_path") or "/leads"
        headers = {"Content-Type": "application/json"}
        auth_header = self.config.get("auth_header")
        auth_token = self.config.get("auth_token")
        if auth_header and auth_token:
            headers[auth_header] = auth_token

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{base}{path}", headers=headers, json=lead_data)
            resp.raise_for_status()
            return "Lead synced to external CRM."
