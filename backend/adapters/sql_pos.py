from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_ident(name: str) -> str:
    if not name or not _IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _quote_ident(name: str, dialect: str) -> str:
    safe = _safe_ident(name)
    if dialect == "mysql":
        return f"`{safe}`"
    if dialect == "sqlserver":
        return f"[{safe}]"
    return f'"{safe}"'


def _table_map(config: Dict[str, Any]) -> Dict[str, Any]:
    tm = config.get("table_map") or {}
    if isinstance(tm, str):
        tm = json.loads(tm)
    return tm


_ENGINES = {}
_ENGINES_LOCK = asyncio.Lock()

async def get_engine(connection_url: str):
    async with _ENGINES_LOCK:
        if connection_url not in _ENGINES:
            from sqlalchemy.ext.asyncio import create_async_engine
            _ENGINES[connection_url] = create_async_engine(
                connection_url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_recycle=1800
            )
        return _ENGINES[connection_url]

class SqlConnection:
    """Async SQL access for Postgres, SQL Server, and MySQL."""

    def __init__(self, provider: str, config: Dict[str, Any]):
        self.provider = provider.lower()
        self.config = config
        self.dialect = {"postgres": "postgres", "sqlserver": "sqlserver", "mysql": "mysql"}.get(
            self.provider, self.provider
        )
        self.read_only = bool(config.get("read_only", True))
        self.schema = config.get("schema") or ("dbo" if self.dialect == "sqlserver" else "public")

    def _connection_url(self) -> str:
        host = self.config.get("host", "localhost")
        port = self.config.get("port")
        database = self.config.get("database", "")
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        ssl = self.config.get("ssl", True)

        if self.dialect == "postgres":
            port = port or 5432
            q = "?ssl=require" if ssl else ""
            return f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}{q}"
        if self.dialect == "sqlserver":
            port = port or 1433
            return f"mssql+pymssql://{username}:{password}@{host}:{port}/{database}"
        if self.dialect == "mysql":
            port = port or 3306
            return f"mysql+aiomysql://{username}:{password}@{host}:{port}/{database}"
        raise ValueError(f"Unsupported SQL provider: {self.provider}")

    async def _engine(self):
        return await get_engine(self._connection_url())

    async def test(self) -> None:
        from sqlalchemy import text

        engine = await self._engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def list_tables_with_columns(self) -> List[Dict[str, Any]]:
        """Introspect schema for admin UI table picker."""
        from sqlalchemy import text

        engine = await self._engine()
        tables: List[Dict[str, Any]] = []
        try:
            async with engine.connect() as conn:
                if self.dialect == "postgres":
                    trows = await conn.execute(
                        text(
                            """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = :schema
                              AND table_type = 'BASE TABLE'
                            ORDER BY table_name
                            """
                        ),
                        {"schema": self.schema},
                    )
                    table_names = [r[0] for r in trows.fetchall()]
                    for tname in table_names:
                        crows = await conn.execute(
                            text(
                                """
                                SELECT column_name, data_type
                                FROM information_schema.columns
                                WHERE table_schema = :schema AND table_name = :table
                                ORDER BY ordinal_position
                                """
                            ),
                            {"schema": self.schema, "table": tname},
                        )
                        tables.append(
                            {
                                "name": tname,
                                "columns": [{"name": r[0], "type": r[1]} for r in crows.fetchall()],
                            }
                        )
                elif self.dialect == "mysql":
                    db = self.config.get("database", "")
                    trows = await conn.execute(
                        text(
                            """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = :db AND table_type = 'BASE TABLE'
                            ORDER BY table_name
                            """
                        ),
                        {"db": db},
                    )
                    table_names = [r[0] for r in trows.fetchall()]
                    for tname in table_names:
                        crows = await conn.execute(
                            text(
                                """
                                SELECT column_name, data_type
                                FROM information_schema.columns
                                WHERE table_schema = :db AND table_name = :table
                                ORDER BY ordinal_position
                                """
                            ),
                            {"db": db, "table": tname},
                        )
                        tables.append(
                            {
                                "name": tname,
                                "columns": [{"name": r[0], "type": r[1]} for r in crows.fetchall()],
                            }
                        )
                elif self.dialect == "sqlserver":
                    trows = await conn.execute(
                        text(
                            """
                            SELECT TABLE_NAME
                            FROM INFORMATION_SCHEMA.TABLES
                            WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE = 'BASE TABLE'
                            ORDER BY TABLE_NAME
                            """
                        ),
                        {"schema": self.schema},
                    )
                    table_names = [r[0] for r in trows.fetchall()]
                    for tname in table_names:
                        crows = await conn.execute(
                            text(
                                """
                                SELECT COLUMN_NAME, DATA_TYPE
                                FROM INFORMATION_SCHEMA.COLUMNS
                                WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
                                ORDER BY ORDINAL_POSITION
                                """
                            ),
                            {"schema": self.schema, "table": tname},
                        )
                        tables.append(
                            {
                                "name": tname,
                                "columns": [{"name": r[0], "type": r[1]} for r in crows.fetchall()],
                            }
                        )
                else:
                    raise ValueError(f"Schema discovery not supported for {self.dialect}")
        finally:
            pass
        return tables

    def _qualified(self, table: str) -> str:
        t = _quote_ident(table, self.dialect)
        if self.dialect == "sqlserver":
            return f"{_quote_ident(self.schema, self.dialect)}.{t}"
        if self.dialect == "postgres":
            return f"{_quote_ident(self.schema, self.dialect)}.{t}"
        return t

    async def fetch_all(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Tuple[Any, ...]]:
        from sqlalchemy import text

        if self.read_only and not sql.strip().upper().startswith("SELECT"):
            raise PermissionError("Integration is read-only — SELECT only.")

        engine = await self._engine()
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return list(result.fetchall())

    async def execute_write(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self.read_only:
            raise PermissionError("Integration is read-only — writes are disabled.")
        from sqlalchemy import text

        engine = await self._engine()
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})


class SqlPOSAdapter:
    """Generic SQL-backed inventory adapter driven entirely by table_map config."""

    def __init__(self, provider: str, config: Dict[str, Any], tenant: TenantContext):
        self.provider = provider
        self.config = config
        self.tenant = tenant
        self.sql = SqlConnection(provider, config)
        self.table_map = _table_map(config)

    def _col(self, group: str, logical: str) -> str:
        cols = self.table_map.get(f"{group}_columns") or {}
        physical = cols.get(logical, logical)
        return _quote_ident(str(physical), self.sql.dialect)

    async def list_products(self, query: Optional[str] = None) -> str:
        from backend.integrations.table_map_util import get_mapped_tables

        mapped = get_mapped_tables(self.table_map, "inventory")
        generic = {"product", "products", "service", "services", "all", "list", "everything", ""}
        q_clean = query.strip().lower() if query else ""
        is_generic = not q_clean or q_clean in generic

        if not mapped:
            table = self.table_map.get("products_table", "products")
            qt = self.sql._qualified(table)
            name_c = self._col("products", "name")
            price_c = self._col("products", "price")
            stock_c = self._col("products", "stock")
            desc_c = self._col("products", "description")

            if not is_generic:
                if self.sql.dialect == "sqlserver":
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} LIKE :q"
                elif self.sql.dialect == "mysql":
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} LIKE :q"
                else:
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} ILIKE :q"
                params = {"q": f"%{query.strip()}%"}
            else:
                sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt}"
                params = None

            try:
                rows = await self.sql.fetch_all(sql, params)
            except Exception as e:
                logger.error("Legacy list_products query failed: %s", e)
                return f"Inventory query failed: {e}"

            if not rows:
                return "No products found in the database."
            lines = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Product Catalog:\n" + "\n".join(lines)

        # Capability / experience questions should hit production / sets / PO tables too
        experience_terms = {
            "production", "productions", "set", "sets", "scenery", "scenic",
            "po", "purchase", "order", "orders", "project", "projects",
            "experience", "capability", "capabilities", "service", "services",
            "film", "tv", "event", "events", "construction",
        }
        q_words = set(re.findall(r"\w+", q_clean)) if q_clean else set()
        is_capability_query = bool(q_words & experience_terms)

        results = []
        for mapping in mapped:
            table = mapping.get("table")
            role = (mapping.get("role") or "").lower()
            label = mapping.get("label") or table
            if not table or mapping.get("enabled") is False:
                continue

            # Skip appointments unless explicitly asked
            if role == "appointments" and not (
                q_clean and ("appointment" in q_clean or "booking" in q_clean or "schedule" in q_clean)
            ):
                continue

            # Skip pure orders on open catalog lists unless asked / capability query
            if role == "orders" and is_generic and not is_capability_query:
                if not (q_clean and ("order" in q_clean or "po" in q_clean or "purchase" in q_clean)):
                    continue

            cols_map = mapping.get("columns") or {}
            if isinstance(cols_map, list):
                cols_map = {c: c for c in cols_map}
            if not cols_map:
                continue

            qt = self.sql._qualified(table)
            select_parts = []
            field_names = []
            seen = set()
            for logical, physical in cols_map.items():
                p = str(physical)
                if p not in seen:
                    select_parts.append(_quote_ident(p, self.sql.dialect))
                    field_names.append(str(logical))
                    seen.add(p)
            if not select_parts:
                continue

            t_lower = table.lower()
            l_lower = label.lower()
            t_words = set(re.findall(r"\w+", t_lower + " " + l_lower + " " + role))
            table_matched = bool(q_words & t_words) or any(
                qw in tw or tw in qw for qw in q_words for tw in t_words if len(qw) > 2 and len(tw) > 2
            )

            like_op = "ILIKE" if self.sql.dialect == "postgres" else "LIKE"
            search_cols = mapping.get("search_columns") or []
            where_parts = []
            for sc in search_cols:
                if sc:
                    where_parts.append(f"{_quote_ident(str(sc), self.sql.dialect)} {like_op} :q")
            if not where_parts and cols_map:
                name_col = (
                    cols_map.get("name")
                    or cols_map.get("title")
                    or cols_map.get("supplier_name")
                    or cols_map.get("set_name")
                    or cols_map.get("description")
                    or list(cols_map.values())[0]
                )
                where_parts.append(f"{_quote_ident(str(name_col), self.sql.dialect)} {like_op} :q")
                # Also search description-like columns when asking about experience
                for key in ("description", "details", "notes", "summary", "type", "category"):
                    if key in cols_map and cols_map[key] != name_col:
                        where_parts.append(
                            f"{_quote_ident(str(cols_map[key]), self.sql.dialect)} {like_op} :q"
                        )

            where_sql = " OR ".join(where_parts)
            params: Dict[str, Any] = {}

            # Generic / capability: dump sample rows from every enabled table
            if is_generic or is_capability_query or table_matched:
                sql = f"SELECT {', '.join(select_parts)} FROM {qt}"
                if not is_generic and not table_matched and where_sql:
                    # Capability query with specific words — also filter when possible
                    sql = f"SELECT {', '.join(select_parts)} FROM {qt} WHERE ({where_sql})"
                    params = {"q": f"%{query.strip()}%"}
            else:
                if not where_sql:
                    continue
                sql = f"SELECT {', '.join(select_parts)} FROM {qt} WHERE ({where_sql})"
                params = {"q": f"%{query.strip()}%"}

            if self.sql.dialect == "sqlserver":
                sql = sql.replace("SELECT", "SELECT TOP 15", 1)
            else:
                sql += " LIMIT 15"

            try:
                rows = await self.sql.fetch_all(sql, params or None)
                if rows:
                    lines = []
                    name_keys = {
                        "name", "title", "product_name", "set_name",
                        "production_name", "project_name", "item_name", "service_name",
                    }
                    for r in rows:
                        display_name = None
                        extras = []
                        for i in range(min(len(r), len(field_names))):
                            key = str(field_names[i])
                            val = r[i]
                            if val is None or str(val).strip() == "":
                                continue
                            if key.lower() in name_keys and display_name is None:
                                display_name = str(val).strip()
                            elif key.lower() not in {"id"}:
                                extras.append(f"{key}: {val}")
                        if display_name:
                            suffix = f" ({', '.join(extras[:3])})" if extras else ""
                            lines.append(f"  • {display_name}{suffix}")
                        elif extras:
                            lines.append(f"  • {', '.join(extras[:4])}")
                    results.append(f"[{label}]\n" + "\n".join(lines))
                elif is_generic or is_capability_query:
                    results.append(f"[{label}] — table is connected but returned no rows.")
            except Exception as e:
                logger.warning("Query failed for table %s: %s", table, e)
                results.append(f"[{label}] — query error: {e}")

        if not results:
            return (
                "No matching records found across mapped database tables. "
                "Confirm Production / Sets / PO tables are mapped under Integrations → Inventory."
            )
        return "\n\n".join(results)

    async def get_order_status(
        self,
        order_id: int,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
    ) -> str:
        if not customer_email and not customer_phone:
            return "Error: You must provide the customer's email or phone number to verify ownership."

        table = self.table_map.get("orders_table", "orders")
        qt = self.sql._qualified(table)
        id_c = self._col("orders", "id")
        email_c = self._col("orders", "email")
        phone_c = self._col("orders", "phone")
        status_c = self._col("orders", "status")
        total_c = self._col("orders", "total")
        items_c = self._col("orders", "items")

        sql = f"SELECT {id_c}, {email_c}, {phone_c}, {status_c}, {total_c}, {items_c} FROM {qt} WHERE {id_c} = :oid"
        rows = await self.sql.fetch_all(sql, {"oid": order_id})
        if not rows:
            return f"No order found with ID: {order_id}"

        db_id, db_email, db_phone, db_status, db_total, db_items = rows[0]
        email_match = customer_email and str(customer_email).lower().strip() == str(db_email or "").lower().strip()
        phone_match = customer_phone and str(customer_phone).strip() == str(db_phone or "").strip()
        if not email_match and not phone_match:
            return "Security Error: Customer verification failed."

        return f"Order #{db_id} Details: Status={db_status}, Items={db_items}, Total={db_total}."

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        catalog = await self.list_products(product_name)
        if "No products found" in catalog:
            return None
        first_line = catalog.split("\n")[1] if "\n" in catalog else catalog
        m = re.match(r"- (.+?): Price=(.+?), In Stock=(\d+)", first_line)
        if not m:
            return {"name": product_name, "price": "0", "stock_quantity": 1, "description": ""}
        return {
            "name": m.group(1),
            "price": m.group(2),
            "stock_quantity": int(m.group(3)),
            "description": "",
        }

    async def create_order(
        self,
        product_name: str,
        customer_email: str,
        customer_phone: str,
        total_price: str,
    ) -> int:
        if self.config.get("read_only", True):
            raise PermissionError("This inventory source is read-only — cannot create orders.")

        table = self.table_map.get("orders_table", "orders")
        qt = self.sql._qualified(table)
        email_c = self._col("orders", "email")
        phone_c = self._col("orders", "phone")
        status_c = self._col("orders", "status")
        total_c = self._col("orders", "total")
        items_c = self._col("orders", "items")

        sql = (
            f"INSERT INTO {qt} ({email_c}, {phone_c}, {status_c}, {total_c}, {items_c}) "
            f"VALUES (:email, :phone, :status, :total, :items)"
        )
        await self.sql.execute_write(
            sql,
            {
                "email": customer_email,
                "phone": customer_phone,
                "status": "Pending Agent Follow-up",
                "total": total_price,
                "items": f"1x {product_name}",
            },
        )
        return 0  # Caller may use Mongo order id; SQL id retrieval is dialect-specific

    async def cancel_order(self, order_id: int) -> bool:
        if self.config.get("read_only", True):
            return False
        table = self.table_map.get("orders_table", "orders")
        qt = self.sql._qualified(table)
        id_c = self._col("orders", "id")
        status_c = self._col("orders", "status")
        await self.sql.execute_write(
            f"UPDATE {qt} SET {status_c} = :st WHERE {id_c} = :oid",
            {"st": "Cancelled", "oid": order_id},
        )
        return True
