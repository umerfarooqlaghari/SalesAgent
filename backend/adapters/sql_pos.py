from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import time
from collections import OrderedDict
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


def _escape_like(value: str) -> str:
    """
    A20: '%' and '_' are LIKE metacharacters, not literals. Caller-supplied text
    ("50% off") reaching a LIKE pattern unescaped silently becomes a wildcard —
    not an injection (params stay bound) but a correctness/latency bug (an
    unintended full scan). Pair with ESCAPE '\\' at every call site.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # link-local, includes the cloud metadata endpoint
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _assert_public_host(host: str) -> None:
    """
    S17: resolve a tenant-supplied DB host and reject private/link-local
    ranges before ever attempting a connection. Without this, "test
    connection" / "discover schema" is a scanning oracle against the backend's
    own internal network (including 169.254.169.254 cloud metadata) for
    anyone holding a tenant API key — a real tenant's external database is
    never legitimately on one of these ranges from this server's perspective.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return  # let the real connection attempt surface a normal DNS error
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if any(ip in net for net in _BLOCKED_NETWORKS):
            raise ValueError("Refusing to connect to a private or link-local database host.")


def _table_map(config: Dict[str, Any]) -> Dict[str, Any]:
    # A25: delegate to the guarded parser — this used to call json.loads directly
    # with no try/except, so one malformed table_map (unbalanced admin JSON edit)
    # raised out of the constructor and 500'd every route touching that source.
    from backend.integrations.table_map_util import parse_table_map_raw

    return parse_table_map_raw(config)


_ENGINES: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()  # url_hash -> (engine, expires_at)
_ENGINES_LOCK = asyncio.Lock()
_ENGINE_TTL_S = 1800  # matches pool_recycle
_ENGINES_MAX = 200

# P05: an unresponsive tenant DB (bad host, firewalled port, overloaded server)
# used to pin an async worker indefinitely — no connect timeout was ever passed
# to the driver. These are driver-specific: asyncpg takes seconds, pymssql takes
# timeout/login_timeout, aiomysql takes connect_timeout.
_CONNECT_TIMEOUT_S = 5
_QUERY_TIMEOUT_S = 5


def _connect_args_for(connection_url: str) -> Dict[str, Any]:
    if connection_url.startswith("postgresql+asyncpg"):
        return {"timeout": _CONNECT_TIMEOUT_S, "command_timeout": _QUERY_TIMEOUT_S}
    if connection_url.startswith("mssql+pymssql"):
        return {"timeout": _QUERY_TIMEOUT_S, "login_timeout": _CONNECT_TIMEOUT_S}
    if connection_url.startswith("mysql+aiomysql"):
        return {"connect_timeout": _CONNECT_TIMEOUT_S}
    return {}


async def _dispose_engine(engine: Any) -> None:
    try:
        await engine.dispose()
    except Exception:
        logger.debug("Engine dispose failed (ignored)", exc_info=True)


async def get_engine(connection_url: str):
    """
    A04: the old cache was unbounded, never disposed, and keyed by the raw
    connection URL (which embeds the plaintext password) forever. A credential
    rotation therefore leaked up to pool_size+max_overflow sockets under the
    stale key and kept the old password resident in memory indefinitely.

    Now keyed by a hash of the URL, bounded, and TTL'd with a real dispose()
    on both expiry and LRU eviction.
    """
    key = hashlib.sha256(connection_url.encode()).hexdigest()
    async with _ENGINES_LOCK:
        now = time.monotonic()
        hit = _ENGINES.get(key)
        if hit is not None:
            engine, expires_at = hit
            if now <= expires_at:
                _ENGINES.move_to_end(key)
                return engine
            _ENGINES.pop(key, None)
            await _dispose_engine(engine)

        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine(
            connection_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=1800,
            connect_args=_connect_args_for(connection_url),
        )
        _ENGINES[key] = (engine, now + _ENGINE_TTL_S)
        _ENGINES.move_to_end(key)
        while len(_ENGINES) > _ENGINES_MAX:
            _, (old_engine, _old_expiry) = _ENGINES.popitem(last=False)
            await _dispose_engine(old_engine)
        return engine

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
        _assert_public_host(self.config.get("host", "localhost"))
        return await get_engine(self._connection_url())

    async def test(self) -> None:
        from sqlalchemy import text

        engine = await self._engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def list_tables_with_columns(self) -> List[Dict[str, Any]]:
        """
        Introspect schema for admin UI table picker.

        A08/A29: one grouped information_schema query per dialect instead of a
        table-list query plus one extra round trip per table (a 600-table
        warehouse was 601 sequential queries); the dead try/finally: pass this
        replaced was leftover from a removed engine.dispose() — A04 restores
        real disposal at the engine-cache layer instead.
        """
        from sqlalchemy import text

        engine = await self._engine()
        grouped: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()
        async with engine.connect() as conn:
            if self.dialect == "postgres":
                rows = await conn.execute(
                    text(
                        """
                        SELECT c.table_name, c.column_name, c.data_type
                        FROM information_schema.columns c
                        JOIN information_schema.tables t
                          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                        WHERE c.table_schema = :schema AND t.table_type = 'BASE TABLE'
                        ORDER BY c.table_name, c.ordinal_position
                        """
                    ),
                    {"schema": self.schema},
                )
            elif self.dialect == "mysql":
                db = self.config.get("database", "")
                rows = await conn.execute(
                    text(
                        """
                        SELECT c.table_name, c.column_name, c.data_type
                        FROM information_schema.columns c
                        JOIN information_schema.tables t
                          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                        WHERE c.table_schema = :db AND t.table_type = 'BASE TABLE'
                        ORDER BY c.table_name, c.ordinal_position
                        """
                    ),
                    {"db": db},
                )
            elif self.dialect == "sqlserver":
                rows = await conn.execute(
                    text(
                        """
                        SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
                        FROM INFORMATION_SCHEMA.COLUMNS c
                        JOIN INFORMATION_SCHEMA.TABLES t
                          ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
                        WHERE c.TABLE_SCHEMA = :schema AND t.TABLE_TYPE = 'BASE TABLE'
                        ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
                        """
                    ),
                    {"schema": self.schema},
                )
            else:
                raise ValueError(f"Schema discovery not supported for {self.dialect}")

            for table_name, column_name, data_type in rows.fetchall():
                grouped.setdefault(table_name, []).append({"name": column_name, "type": data_type})

        return [{"name": name, "columns": cols} for name, cols in grouped.items()]

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

        async def _run():
            engine = await self._engine()
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params or {})
                return list(result.fetchall())

        # P05: belt-and-suspenders on top of the driver connect_args — some
        # drivers (pymssql) don't enforce a distinct per-query timeout, so a
        # slow query past connect can still hang the worker without this.
        return await asyncio.wait_for(_run(), timeout=_CONNECT_TIMEOUT_S + _QUERY_TIMEOUT_S)

    async def execute_write(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self.read_only:
            raise PermissionError("Integration is read-only — writes are disabled.")
        from sqlalchemy import text

        async def _run():
            engine = await self._engine()
            async with engine.begin() as conn:
                await conn.execute(text(sql), params or {})

        await asyncio.wait_for(_run(), timeout=_CONNECT_TIMEOUT_S + _QUERY_TIMEOUT_S)


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
            # A11: the legacy path had no row limit at all — fetch_all would
            # materialize the whole table. Match the dialect-aware cap used on
            # the mapped path below.
            limit_clause = "TOP 50 " if self.sql.dialect == "sqlserver" else ""
            select_prefix = f"SELECT {limit_clause}{name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt}"

            if not is_generic:
                like_op = "ILIKE" if self.sql.dialect == "postgres" else "LIKE"
                sql = f"{select_prefix} WHERE {name_c} {like_op} :q ESCAPE '\\'"
                params = {"q": f"%{_escape_like(query.strip())}%"}
            else:
                sql = select_prefix
                params = None

            if self.sql.dialect != "sqlserver":
                sql += " LIMIT 50"

            try:
                rows = await self.sql.fetch_all(sql, params)
            except Exception as e:
                logger.error("Legacy list_products query failed: %s", e)
                return f"Inventory query failed: {e}"

            if not rows:
                return "No products found in the database."
            lines = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Product Catalog:\n" + "\n".join(lines)

        experience_terms = {
            "production", "productions", "set", "sets", "scenery", "scenic",
            "po", "purchase", "order", "orders", "project", "projects",
            "experience", "capability", "capabilities", "service", "services",
            "film", "tv", "event", "events", "construction",
        }
        q_words = set(re.findall(r"\w+", q_clean)) if q_clean else set()
        is_capability_query = bool(q_words & experience_terms)

        # Determine category intention from query
        service_terms = {"service", "services", "package", "packages", "pricing", "cost", "costs", "plan", "plans", "development", "ecommerce", "e-commerce", "ai", "ml", "engineering"}
        product_terms = {"product", "products", "mentore", "grabengo", "catalog", "item", "items", "tool", "tools", "software", "sku", "skus"}
        blog_terms = {"blog", "blogs", "post", "posts", "article", "articles", "news"}
        faq_terms = {"faq", "faqs", "question", "questions", "answer", "answers", "help"}

        asking_services = bool(q_words & service_terms)
        asking_products = bool(q_words & product_terms)
        asking_blogs = bool(q_words & blog_terms)
        asking_faqs = bool(q_words & faq_terms)

        results = []
        for mapping in mapped:
            table = mapping.get("table")
            role = (mapping.get("role") or "").lower()
            label = mapping.get("label") or table
            if not table or mapping.get("enabled") is False:
                continue

            t_lower = table.lower()
            l_lower = label.lower()
            t_combined = f"{t_lower} {l_lower} {role}"
            t_words = set(re.findall(r"\w+", t_combined))

            # Specific category routing: skip non-matching table families when user asks for specific category
            if asking_services and not ({"service", "services", "package", "packages", "pricing", "plan", "content", "card", "cards"} & t_words):
                continue
            if asking_products and not ({"product", "products", "item", "items", "catalog", "content", "card", "cards"} & t_words):
                continue
            if asking_blogs and not ({"blog", "blogs", "post", "posts", "article", "articles"} & t_words):
                continue
            if asking_faqs and not ({"faq", "faqs", "question", "questions", "answer", "answers"} & t_words):
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

            table_matched = bool(q_words & t_words) or any(
                qw in tw or tw in qw for qw in q_words for tw in t_words if len(qw) > 2 and len(tw) > 2
            )

            like_op = "ILIKE" if self.sql.dialect == "postgres" else "LIKE"
            search_cols = mapping.get("search_columns") or []
            where_parts = []
            for sc in search_cols:
                if sc:
                    where_parts.append(f"{_quote_ident(str(sc), self.sql.dialect)} {like_op} :q ESCAPE '\\'")
            if not where_parts and cols_map:
                name_col = (
                    cols_map.get("name")
                    or cols_map.get("title")
                    or cols_map.get("supplier_name")
                    or cols_map.get("set_name")
                    or cols_map.get("description")
                    or list(cols_map.values())[0]
                )
                where_parts.append(f"{_quote_ident(str(name_col), self.sql.dialect)} {like_op} :q ESCAPE '\\'")
                # Also search description-like columns when asking about experience
                for key in ("description", "details", "notes", "summary", "type", "category"):
                    if key in cols_map and cols_map[key] != name_col:
                        where_parts.append(
                            f"{_quote_ident(str(cols_map[key]), self.sql.dialect)} {like_op} :q ESCAPE '\\'"
                        )

            where_sql = " OR ".join(where_parts)
            params: Dict[str, Any] = {}

            # Generic / capability: dump sample rows from every enabled table
            if is_generic or is_capability_query or table_matched:
                sql = f"SELECT {', '.join(select_parts)} FROM {qt}"
                if not is_generic and not table_matched and where_sql:
                    # Capability query with specific words — also filter when possible
                    sql = f"SELECT {', '.join(select_parts)} FROM {qt} WHERE ({where_sql})"
                    params = {"q": f"%{_escape_like(query.strip())}%"}
            else:
                if not where_sql:
                    continue
                sql = f"SELECT {', '.join(select_parts)} FROM {qt} WHERE ({where_sql})"
                params = {"q": f"%{_escape_like(query.strip())}%"}

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
                        "question",
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
                            # Retain all rich details (description, content, features, hero text, answers)
                            suffix = f" ({', '.join(extras)})" if extras else ""
                            lines.append(f"  • {display_name}{suffix}")
                        elif extras:
                            lines.append(f"  • {', '.join(extras)}")
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

    _NOT_FOUND_MARKERS = (
        "No products found",
        "No matching records found",
        "returned no rows",
        "query error",
        "Inventory query failed",
    )
    _MAPPED_BULLET_RE = re.compile(r"^\s*•\s*(.+)$")

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        catalog = await self.list_products(product_name)
        if any(marker in catalog for marker in self._NOT_FOUND_MARKERS):
            return None

        lines = catalog.split("\n")
        legacy_line = lines[1] if len(lines) > 1 else catalog
        m = re.match(r"- (.+?): Price=(.+?), In Stock=(\d+)", legacy_line)
        if m:
            return {
                "name": m.group(1),
                "price": m.group(2),
                "stock_quantity": int(m.group(3)),
                "description": "",
            }

        # A10: the mapped path never matches the legacy "- Name: Price=..."
        # shape (it's "[Label]\n  • Name (key: val, ...)"), so the regex above
        # always misses for tenants using mapped tables. Returning a fabricated
        # {price: "0", stock: 1} let the agent confirm an imaginary $0 item to a
        # caller. Best-effort parse the mapped bullet; otherwise say not found.
        for line in lines:
            bullet = self._MAPPED_BULLET_RE.match(line)
            if not bullet:
                continue
            name_part, _, extras_part = bullet.group(1).partition(" (")
            extras: Dict[str, str] = {}
            if extras_part.endswith(")"):
                for pair in extras_part[:-1].split(", "):
                    k, _, v = pair.partition(": ")
                    if k:
                        extras[k.strip().lower()] = v.strip()
            price = extras.get("price") or extras.get("cost")
            stock_raw = extras.get("stock") or extras.get("stock_quantity") or extras.get("quantity")
            if price is None and stock_raw is None:
                return None
            try:
                stock_val = int(re.sub(r"[^\d-]", "", stock_raw)) if stock_raw else 0
            except ValueError:
                stock_val = 0
            return {
                "name": name_part.strip() or product_name,
                "price": price or "0",
                "stock_quantity": stock_val,
                "description": "",
            }
        return None

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
