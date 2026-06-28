"""Discover SQL tables/columns and suggest table_map mappings for layman-friendly admin UI."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from backend.adapters.sql_pos import SqlConnection

SQL_PROVIDERS = frozenset({"postgres", "sqlserver", "mysql"})

COLUMN_HINTS: Dict[str, Tuple[str, ...]] = {
    "name": ("name", "product_name", "item_name", "title", "product", "service_name"),
    "price": ("price", "unit_price", "cost", "amount", "list_price"),
    "stock": ("stock", "stock_quantity", "quantity", "qty", "inventory", "in_stock"),
    "description": ("description", "desc", "details", "summary", "body"),
    "id": ("id", "order_id", "pk"),
    "email": ("email", "customer_email", "email_address", "contact_email"),
    "phone": ("phone", "customer_phone", "mobile", "tel", "phone_number"),
    "status": ("status", "state", "order_status"),
    "total": ("total", "total_price", "amount", "grand_total", "order_total"),
    "items": ("items", "line_items", "order_items", "product_list", "details"),
    "company": ("company", "company_name", "organization", "org_name", "account_name", "customer_name", "name"),
    "fit": ("fit", "is_qualified", "qualified", "is_fit", "lead_fit"),
    "date": ("date", "appt_date", "appointment_date", "scheduled_date", "event_date", "start_date"),
    "time": ("time", "appt_time", "appointment_time", "scheduled_time", "start_time", "event_time"),
}

TABLE_HINTS: Dict[str, Tuple[str, ...]] = {
    "products_table": ("product", "products", "catalog", "items", "services", "sku", "inventory"),
    "orders_table": ("order", "orders", "sales", "purchase", "transactions"),
    "companies_table": ("company", "companies", "customer", "customers", "client", "clients", "account", "accounts", "lead", "leads"),
    "appointments_table": ("appointment", "appointments", "booking", "bookings", "schedule", "calendar", "events"),
}

ROLE_FIELDS: Dict[str, Dict[str, Any]] = {
    "inventory": {
        "tables": [
            ("products_table", "Product catalog", ("name", "price", "stock", "description")),
            ("orders_table", "Orders", ("id", "email", "phone", "status", "total", "items")),
        ],
    },
    "crm": {
        "tables": [
            ("companies_table", "Companies / leads", ("company", "status", "fit")),
        ],
    },
    "calendar": {
        "tables": [
            ("appointments_table", "Appointments", ("date", "time", "email", "phone", "name", "status")),
        ],
    },
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _score_table(table_name: str, hints: Tuple[str, ...]) -> int:
    n = _norm(table_name)
    best = 0
    for h in hints:
        hn = _norm(h)
        if n == hn:
            return 100
        if hn in n or n in hn:
            best = max(best, 80)
        if n.endswith(hn) or n.startswith(hn):
            best = max(best, 60)
    return best


def _pick_column(columns: List[str], logical: str) -> Optional[str]:
    hints = COLUMN_HINTS.get(logical, (logical,))
    col_norm = {_norm(c): c for c in columns}
    for hint in hints:
        hn = _norm(hint)
        if hn in col_norm:
            return col_norm[hn]
    for c in columns:
        cn = _norm(c)
        for hint in hints:
            hn = _norm(hint)
            if hn in cn or cn in hn:
                return c
    optional = {"fit", "items", "description", "time"}
    if logical in optional:
        return None
    return columns[0] if columns else None


def _pick_table(table_names: List[str], role_key: str) -> Optional[str]:
    hints = TABLE_HINTS.get(role_key, ())
    scored = [(t, _score_table(t, hints)) for t in table_names]
    scored.sort(key=lambda x: (-x[1], x[0]))
    if scored and scored[0][1] > 0:
        return scored[0][0]
    return None


def suggest_table_map(category: str, tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    table_names = [t["name"] for t in tables]
    cols_by_table = {t["name"]: [c["name"] for c in t.get("columns", [])] for t in tables}
    mapping: Dict[str, Any] = {}
    role = ROLE_FIELDS.get(category, {})

    for table_key, _label, logical_cols in role.get("tables", []):
        picked = _pick_table(table_names, table_key)
        if not picked:
            continue
        mapping[table_key] = picked
        col_map: Dict[str, str] = {}
        available = cols_by_table.get(picked, [])
        for logical in logical_cols:
            physical = _pick_column(available, logical)
            if physical:
                col_map[logical] = physical
        group = table_key.replace("_table", "")
        mapping[f"{group}_columns"] = col_map

    return mapping


def suggest_mapped_tables(category: str, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Suggest one mapped_tables entry per relevant DB table."""
    import uuid

    table_names = [t["name"] for t in tables]
    cols_by_table = {t["name"]: [c["name"] for c in t.get("columns", [])] for t in tables}
    result: List[Dict[str, Any]] = []

    if category == "crm":
        scored = [(t, _score_table(t, TABLE_HINTS["companies_table"])) for t in table_names]
        scored.sort(key=lambda x: (-x[1], x[0]))
        for tname, score in scored[:12]:
            if score < 30 and result:
                continue
            cols = cols_by_table.get(tname, [])
            col_map: Dict[str, str] = {}
            used_physical: set[str] = set()
            for logical in ("company", "status", "fit", "email", "phone", "id"):
                physical = _pick_column(cols, logical)
                if physical and physical not in used_physical:
                    col_map[logical] = physical
                    used_physical.add(physical)
            if not col_map and cols:
                for c in cols[:8]:
                    col_map[c] = c
            search = []
            for key in ("company", "email", "name", "phone"):
                if key in col_map:
                    search.append(col_map[key])
                    break
            if not search and col_map:
                search = [list(col_map.values())[0]]
            result.append(
                {
                    "id": f"mt_{uuid.uuid4().hex[:8]}",
                    "table": tname,
                    "label": tname.replace("_", " ").title(),
                    "enabled": score >= 60,
                    "search_columns": search,
                    "columns": col_map,
                }
            )
        return result

    role = ROLE_FIELDS.get(category, {})
    for table_key, label, logical_cols in role.get("tables", []):
        picked = _pick_table(table_names, table_key)
        if not picked:
            continue
        available = cols_by_table.get(picked, [])
        col_map = {}
        for logical in logical_cols:
            physical = _pick_column(available, logical)
            if physical:
                col_map[logical] = physical
        result.append(
            {
                "id": f"mt_{uuid.uuid4().hex[:8]}",
                "table": picked,
                "label": label,
                "role": table_key.replace("_table", ""),
                "enabled": True,
                "search_columns": [],
                "columns": col_map,
            }
        )
    return result


async def discover_sql_schema(provider: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    conn = SqlConnection(provider, config)
    return await conn.list_tables_with_columns()


def build_discovery_response(category: str, provider: str, tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    suggested = suggest_table_map(category, tables)
    suggested_mapped = suggest_mapped_tables(category, tables)
    roles = ROLE_FIELDS.get(category, {})
    role_defs = []
    for table_key, label, logical_cols in roles.get("tables", []):
        cols_key = table_key.replace("_table", "_columns")
        role_defs.append(
            {
                "table_key": table_key,
                "columns_key": cols_key,
                "label": label,
                "logical_columns": list(logical_cols),
                "suggested_table": suggested.get(table_key),
                "suggested_columns": suggested.get(cols_key, {}),
            }
        )

    return {
        "ok": True,
        "provider": provider,
        "category": category,
        "tables": tables,
        "suggested_table_map": suggested,
        "suggested_mapped_tables": suggested_mapped,
        "roles": role_defs,
    }
