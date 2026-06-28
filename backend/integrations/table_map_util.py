"""Normalize table_map — legacy single-table + multi-table mapped_tables."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional


def parse_table_map_raw(config: Dict[str, Any]) -> Dict[str, Any]:
    import json

    tm = config.get("table_map") or {}
    if isinstance(tm, str):
        try:
            tm = json.loads(tm) if tm.strip() else {}
        except json.JSONDecodeError:
            tm = {}
    return tm if isinstance(tm, dict) else {}


def get_mapped_tables(table_map: Dict[str, Any], category: str) -> List[Dict[str, Any]]:
    """Return enabled mapped table configs, migrating legacy shapes if needed."""
    raw = table_map.get("mapped_tables")
    if isinstance(raw, list) and raw:
        return [t for t in raw if t.get("enabled", True) and t.get("table")]

    migrated: List[Dict[str, Any]] = []
    if category == "crm":
        table = table_map.get("companies_table")
        cols = table_map.get("companies_columns") or {}
        if table:
            search = []
            company_col = cols.get("company")
            if company_col:
                search.append(company_col)
            migrated.append(
                {
                    "id": "legacy_companies",
                    "table": table,
                    "label": table,
                    "enabled": True,
                    "search_columns": search or list(cols.values())[:1],
                    "columns": cols,
                }
            )
    elif category == "inventory":
        for role, table_key, cols_key in (
            ("products", "products_table", "products_columns"),
            ("orders", "orders_table", "orders_columns"),
        ):
            table = table_map.get(table_key)
            cols = table_map.get(cols_key) or {}
            if table:
                migrated.append(
                    {
                        "id": f"legacy_{role}",
                        "table": table,
                        "label": table,
                        "role": role,
                        "enabled": True,
                        "search_columns": [],
                        "columns": cols,
                    }
                )
    elif category == "calendar":
        table = table_map.get("appointments_table")
        cols = table_map.get("appointments_columns") or {}
        if table:
            migrated.append(
                {
                    "id": "legacy_appointments",
                    "table": table,
                    "label": table,
                    "role": "appointments",
                    "enabled": True,
                    "search_columns": [],
                    "columns": cols,
                }
            )
    return migrated


def table_by_role(mapped: List[Dict[str, Any]], role: str) -> Optional[Dict[str, Any]]:
    for t in mapped:
        if t.get("role") == role:
            return t
    return None
