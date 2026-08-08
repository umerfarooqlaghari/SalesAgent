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


def _table_map(config: Dict[str, Any]) -> Dict[str, Any]:
    # A25: delegate to the guarded parser — this used to call json.loads directly
    # with no try/except, so one malformed table_map (unbalanced admin JSON edit)
    # raised out of the constructor and 500'd every route touching that source.
    from backend.integrations.table_map_util import parse_table_map_raw

    return parse_table_map_raw(config)


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
    anyone holding a tenant API key.
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


_ENGINES: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()  # url_hash -> (engine, expires_at)
_ENGINES_LOCK = asyncio.Lock()
_ENGINE_TTL_S = 1800  # matches pool_recycle
_ENGINES_MAX = 200

# P05: a tenant database is a third-party system on the voice critical path.
# Without these, an unresponsive host (network partition, table lock, a paused
# cloud instance) hangs the request until the OS gives up — pinning a worker and
# blowing the whole spoken-turn budget.
_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_S = 8
_QUERY_TIMEOUT_S = 10.0   # belt-and-braces around the driver's own timeout


def _connect_args_for(connection_url: str) -> dict:
    """Driver-specific connect/statement timeouts."""
    url = (connection_url or "").lower()
    if url.startswith("postgresql+asyncpg"):
        return {
            "timeout": _CONNECT_TIMEOUT_S,
            "command_timeout": _STATEMENT_TIMEOUT_S,
            # asyncpg applies this server-side, so a runaway query is cancelled
            # by Postgres itself rather than merely abandoned by us.
            "server_settings": {"statement_timeout": str(_STATEMENT_TIMEOUT_S * 1000)},
        }
    if url.startswith("mysql+aiomysql"):
        return {"connect_timeout": _CONNECT_TIMEOUT_S}
    if "pymssql" in url or "aioodbc" in url:
        return {"timeout": _CONNECT_TIMEOUT_S, "login_timeout": _CONNECT_TIMEOUT_S}
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

        # P05: the outer deadline also covers connection acquisition and pool
        # waits, which the driver's own command_timeout does not.
        try:
            return await asyncio.wait_for(_run(), timeout=_QUERY_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error(
                "Tenant SQL query exceeded %.0fs (provider=%s) — abandoning",
                _QUERY_TIMEOUT_S, self.provider,
            )
            raise TimeoutError(
                f"The {self.provider} database did not respond within "
                f"{_QUERY_TIMEOUT_S:.0f}s."
            ) from None

    async def execute_write(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self.read_only:
            raise PermissionError("Integration is read-only — writes are disabled.")
        from sqlalchemy import text

        async def _run():
            engine = await self._engine()
            async with engine.begin() as conn:
                await conn.execute(text(sql), params or {})

        try:
            await asyncio.wait_for(_run(), timeout=_QUERY_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error(
                "Tenant SQL write exceeded %.0fs (provider=%s) — abandoning",
                _QUERY_TIMEOUT_S, self.provider,
            )
            raise TimeoutError(
                f"The {self.provider} database did not respond within "
                f"{_QUERY_TIMEOUT_S:.0f}s."
            ) from None


_NOT_FOUND_MARKERS = (
    "No products found",
    "No matching records found",
    "table is connected but returned no rows",
    "query error:",
)
_MAPPED_BULLET_RE = re.compile(r"^\s*•\s*(.+?)(?:\s*\((.+)\))?\s*$")


def _parse_mapped_bullet(line: str) -> Optional[Dict[str, Any]]:
    m = _MAPPED_BULLET_RE.match(line)
    if not m:
        return None
    name = m.group(1).strip()
    if not name:
        return None
    extras_raw = m.group(2) or ""
    price = None
    stock = None
    description = ""
    for part in extras_raw.split(", "):
        if ":" not in part:
            continue
        key, _, val = part.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "price":
            price = val
        elif key in ("stock", "stock_quantity", "quantity"):
            try:
                stock = int(re.sub(r"[^\d-]", "", val) or "0")
            except ValueError:
                stock = None
        elif key in ("description", "details", "summary") and not description:
            description = val
    if price is None and stock is None:
        return None
    return {
        "name": name,
        "price": price if price is not None else "",
        "stock_quantity": stock if stock is not None else 0,
        "description": description,
    }


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
                # A20: escape LIKE metacharacters in caller-supplied text so
                # "50% off" can't silently turn into a wildcard scan.
                escaped_q = _escape_like(query.strip())
                if self.sql.dialect == "sqlserver":
                    sql = f"SELECT TOP 50 {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} LIKE :q ESCAPE '\\'"
                elif self.sql.dialect == "mysql":
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} LIKE :q ESCAPE '\\' LIMIT 50"
                else:
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} WHERE {name_c} ILIKE :q ESCAPE '\\'  LIMIT 50"
                params = {"q": f"%{escaped_q}%"}
            else:
                # A11: an unbounded SELECT * equivalent against a large legacy
                # products table pulled every row into memory on every "list
                # everything" question.
                if self.sql.dialect == "sqlserver":
                    sql = f"SELECT TOP 50 {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt}"
                else:
                    sql = f"SELECT {name_c}, {price_c}, {stock_c}, {desc_c} FROM {qt} LIMIT 50"
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

    async def lookup_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        """
        A10: this used to fabricate {"price": "0", "stock_quantity": 1} whenever
        the response text didn't match the expected legacy regex — the caller
        (order/booking flow) then quoted a confident but entirely made-up price
        and "in stock" status to the customer instead of surfacing "not found".
        """
        catalog = await self.list_products(product_name)
        if any(marker in catalog for marker in _NOT_FOUND_MARKERS):
            return None

        lines = [ln for ln in catalog.split("\n") if ln.strip()]
        body_lines = lines[1:] if len(lines) > 1 else lines

        for line in body_lines:
            legacy = re.match(r"- (.+?): Price=(.+?), In Stock=(\d+)", line)
            if legacy:
                return {
                    "name": legacy.group(1),
                    "price": legacy.group(2),
                    "stock_quantity": int(legacy.group(3)),
                    "description": "",
                }
            mapped = _parse_mapped_bullet(line)
            if mapped:
                return mapped

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
