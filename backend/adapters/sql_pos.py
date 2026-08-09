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
        "0.0.0.0/8",       # 0.0.0.0 routes to localhost on Linux
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # link-local, includes the AWS/GCP metadata endpoint
        "100.64.0.0/10",   # carrier-grade NAT — cloud internal / Tailscale
        "192.0.0.192/32",  # Oracle Cloud metadata
        "::1/128",
        "::/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _host_from_url(url: str) -> Optional[str]:
    """
    Pull the hostname out of a SQLAlchemy/DB URL.

    Needed because `_connection_url()` PREFERS a pasted `connection_url` over the
    discrete host/port fields. Guarding `config["host"]` therefore checked the
    wrong thing entirely for those tenants — see `_effective_host`.
    """
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname
    except Exception:
        return None


def _normalise_ip(raw_ip: str):
    """
    IPv4-mapped IPv6 (`::ffff:169.254.169.254`) is NOT contained in 169.254.0.0/16
    when compared as an IPv6Address, so a resolver returning only a AAAA record
    walked straight through the blocklist. Fold those down to their IPv4 form.
    """
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    # 6to4 (2002::/16) and Teredo (2001::/32) can also encapsulate a v4 address.
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        return sixtofour
    return ip


def _assert_public_host(host: str) -> None:
    """
    S17: resolve a tenant-supplied DB host and reject private/link-local
    ranges before ever attempting a connection. Without this, "test
    connection" / "discover schema" is a scanning oracle against the backend's
    own internal network (including 169.254.169.254 cloud metadata) for
    anyone holding a tenant API key.
    """
    if not host:
        # Nothing to check. Refusing here would block every tenant whose config
        # has no discrete host field, which is the normal shape for a pasted
        # connection URL.
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Let the real connection attempt surface a normal DNS error rather than
        # a security-sounding one. NOTE: this is deliberately fail-open, so a
        # host whose resolution fails here is still handed to the driver.
        logger.debug("Could not resolve %s for the SSRF check", host)
        return
    for info in infos:
        ip = _normalise_ip(info[4][0])
        if ip is None:
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
        # `timeout` and `command_timeout` are asyncpg client-side options — they
        # never reach the server, so they are safe against any endpoint.
        #
        # `server_settings` is NOT safe: asyncpg puts those keys in the Postgres
        # startup packet, and managed proxies (Prisma Postgres / Accelerate on
        # db.prisma.io, PgBouncer in transaction mode, Supabase's pooler) reject
        # startup parameters they do not recognise. Prisma reports that as
        # "Failed to identify your database: Your Postgres credentials are
        # incorrect", which looks like an auth problem and is not one.
        #
        # A server-side statement_timeout is therefore applied per connection
        # after connect (see _apply_statement_timeout), where a proxy that does
        # not support it can fail harmlessly.
        return {
            "timeout": _CONNECT_TIMEOUT_S,
            "command_timeout": _STATEMENT_TIMEOUT_S,
        }
    if url.startswith("mysql+aiomysql"):
        return {"connect_timeout": _CONNECT_TIMEOUT_S}
    if "pymssql" in url or "aioodbc" in url:
        return {"timeout": _CONNECT_TIMEOUT_S, "login_timeout": _CONNECT_TIMEOUT_S}
    return {}


def _apply_statement_timeout(engine: Any, connection_url: str) -> None:
    """
    Ask Postgres to cancel runaway queries itself.

    Issued as a normal statement on each new connection rather than as a startup
    parameter, so an endpoint that does not allow it (Prisma Postgres, PgBouncer
    in transaction mode) just logs a debug line instead of refusing the whole
    connection.
    """
    if not connection_url.lower().startswith("postgresql+asyncpg"):
        return

    ms = _STATEMENT_TIMEOUT_S * 1000

    # Wrapped end to end. This is a best-effort optimisation on top of the
    # client-side timeouts, so nothing it does may prevent an engine being
    # created — an engine object without `sync_engine`, or a SQLAlchemy version
    # that moves the event API, must degrade rather than break every connection.
    try:
        from sqlalchemy import event

        sync_engine = getattr(engine, "sync_engine", None)
        if sync_engine is None:
            logger.debug("Engine exposes no sync_engine — skipping statement_timeout hook")
            return

        @event.listens_for(sync_engine, "connect")
        def _set_timeout(dbapi_conn, _record):  # pragma: no cover - driver callback
            try:
                cursor = dbapi_conn.cursor()
                cursor.execute(f"SET statement_timeout = {ms}")
                cursor.close()
            except Exception:
                logger.debug(
                    "Endpoint rejected SET statement_timeout — relying on client timeouts"
                )
    except Exception:
        logger.debug("Could not attach the statement_timeout hook", exc_info=True)


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
        _apply_statement_timeout(engine, connection_url)
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
        # A pasted URL wins: it avoids every field-entry failure mode - a
        # truncated 64-character username, a password containing characters the
        # form mangles, a missing SSL setting.
        raw = (self.config.get("connection_url") or "").strip()
        if raw:
            return normalise_sql_url(raw, self.dialect)

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

    def _effective_host(self) -> Optional[str]:
        """
        The host the driver will ACTUALLY dial.

        This used to be `config.get("host", "localhost")`. When a tenant pastes a
        full `connection_url` — the normal path for Prisma/Neon/Supabase, and the
        one `_connection_url()` explicitly prefers — there is no `host` field, so
        that expression returned the literal string "localhost", which resolves
        to 127.0.0.1 and tripped the SSRF guard on EVERY query. That is what made
        every products/services/packages question fail.

        It was also a security hole in the other direction: with a connection_url
        set, the guard inspected "localhost" instead of the real target, so the
        URL form bypassed the SSRF check entirely.
        """
        url = (self.config.get("connection_url") or "").strip()
        if url:
            return _host_from_url(normalise_sql_url(url, self.dialect)) or _host_from_url(url)
        return self.config.get("host") or None

    async def _engine(self):
        _assert_public_host(self._effective_host())
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


# Rows are capped per mapped table when warming the catalog. Generous, because
# the result is cached for the TTL rather than fetched per turn.
_CATALOG_ROW_LIMIT = 60

# Columns that describe what KIND of thing a row is. Pulled to the front of each
# rendered row so the agent can answer "which product is of which type".
_KIND_KEYS = {"category", "type", "kind", "group", "segment", "service_type",
              "product_type", "tier", "plan"}


# Generic synonyms attached to a table's declared ROLE, so a caller's everyday
# word reaches the right table whatever the tenant happens to have named it.
# These are role-based, never tenant-based: nothing here mentions a specific
# customer's products.
_ROLE_SYNONYMS = {
    "products": {"product", "products", "item", "items", "catalog", "catalogue",
                 "sku", "skus", "range", "lineup"},
    "services": {"service", "services", "offering", "offerings", "package",
                 "packages", "plan", "plans", "pricing"},
    "blog": {"blog", "blogs", "post", "posts", "article", "articles", "news"},
    "faq": {"faq", "faqs", "question", "questions", "answer", "answers", "help"},
    "appointments": {"appointment", "appointments", "booking", "bookings",
                     "schedule", "slot", "slots"},
    "orders": {"order", "orders", "purchase", "purchases"},
    "staff": {"staff", "team", "people", "doctor", "doctors", "dentist", "dentists",
              "practitioner", "practitioners", "consultant", "consultants"},
}


def _expand(words: set) -> set:
    """Add naive singular/plural variants so "service" matches a "Services" table."""
    out = set(words)
    for w in words:
        if len(w) > 3:
            out.add(w[:-1] if w.endswith("s") else w + "s")
    return out


def table_query_terms(mapping: Dict[str, Any]) -> set:
    """
    Words that should route a question to THIS table.

    Built from the tenant's own table name, display label and role — plus generic
    synonyms for the role. Previously this was a fixed products/services/blog/faq
    taxonomy with one customer's product names hardcoded into it, so a dental
    clinic whose table is called "Treatments" was skipped entirely when a caller
    said "services", and the agent answered that it had no matching records.
    """
    import re as _re

    raw = " ".join(str(mapping.get(k) or "") for k in ("table", "label", "role"))
    words = {w.lower() for w in _re.findall(r"\w+", raw) if len(w) > 2}
    terms = _expand(words)

    role = str(mapping.get("role") or "").lower()
    for role_key, syns in _ROLE_SYNONYMS.items():
        if role_key in role or role_key in words:
            terms |= syns

    # Column names are part of a table's vocabulary too ("qualification", "director").
    cols = mapping.get("columns") or {}
    col_names = cols if isinstance(cols, list) else list(cols.keys()) + list(cols.values())
    col_words = set()
    for c in col_names:
        for w in _re.findall(r"\w+", str(c)):
            if len(w) > 3:
                col_words.add(w.lower())
    return terms | _expand(col_words)


def select_tables_for_query(mapped: list, q_words: set) -> tuple:
    """
    (tables_to_query, narrowed). Never returns an empty list.

    If nothing matches the caller's words we query everything rather than
    starving the answer — a vocabulary miss must degrade to "too much data",
    never to "no matching records found".
    """
    if not q_words:
        return list(mapped), False
    matched = [m for m in mapped if q_words & table_query_terms(m)]
    if matched:
        return matched, True
    return list(mapped), False


_ASYNC_DRIVERS = {"postgres": "postgresql+asyncpg", "mysql": "mysql+aiomysql",
                  "sqlserver": "mssql+pymssql"}

# libpq understands these; the async drivers do not, and passing them straight
# through makes connect() raise TypeError on an unexpected keyword argument.
_LIBPQ_ONLY = {"sslmode", "channel_binding", "target_session_attrs", "options",
               "application_name", "connect_timeout", "gssencmode", "sslrootcert",
               "sslcert", "sslkey", "pgbouncer"}


def normalise_sql_url(raw: str, dialect: str) -> str:
    """
    Turn a pasted connection string into one SQLAlchemy and the async driver accept.

    Handles the three things that break a copy-paste from a hosting provider:
      * a sync scheme ("postgres://", "postgresql://"), which would load psycopg2
      * libpq-only query parameters - above all `sslmode=require`, which asyncpg
        rejects as an unexpected keyword argument
      * Prisma's Accelerate URL, which is not the Postgres wire protocol at all
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    url = (raw or "").strip()
    if url.startswith("prisma+postgres://") or "accelerate.prisma-data.net" in url:
        raise ValueError(
            "That is a Prisma Accelerate URL. Accelerate speaks Prisma's own HTTP "
            "protocol, not the Postgres wire protocol, so it cannot be used here. "
            "Use the direct database URL instead (the one whose host is db.prisma.io)."
        )

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if "+" not in scheme:
        base = {"postgres": "postgres", "postgresql": "postgres",
                "mysql": "mysql", "mssql": "sqlserver"}.get(scheme, dialect)
        scheme = _ASYNC_DRIVERS.get(base, _ASYNC_DRIVERS.get(dialect, scheme))

    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = params.get("sslmode")
    kept = {k: v for k, v in params.items() if k.lower() not in _LIBPQ_ONLY}

    if scheme.startswith("postgresql+asyncpg"):
        # asyncpg takes the libpq vocabulary, but under the name `ssl`.
        if sslmode and sslmode.lower() == "disable":
            kept.pop("ssl", None)
        elif sslmode:
            kept.setdefault("ssl", sslmode)
        elif "ssl" not in kept:
            kept["ssl"] = "require"

    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


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

        generic = {
            "product", "products", "service", "services", "all", "list", "everything", "",
            "more", "other", "additional", "next", "show more", "see more", "tell me more",
            "more products", "more services", "other products", "other services",
            "anything else", "what else", "all products", "all services", "catalogue", "catalog",
        }
        q_clean = query.strip().lower() if query else ""
        q_words = {w.lower() for w in re.findall(r"\w+", q_clean)} if q_clean else set()
        is_pagination = bool(q_words & {"more", "other", "additional", "else", "next", "another", "further"})
        is_generic = not q_clean or q_clean in generic or is_pagination

        mapped = get_mapped_tables(self.table_map, "inventory")
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

        q_words = {w.lower() for w in re.findall(r"\w+", q_clean)} if q_clean else set()

        # Which of THIS tenant's tables does the question point at? Derived from
        # their own labels/roles/columns rather than a fixed products-and-services
        # taxonomy, so a clinic ("Treatments", "Doctors") or a production company
        # ("Films", "Cast") routes just as well as a software vendor.
        mapped, narrowed = select_tables_for_query(mapped, q_words)
        is_capability_query = narrowed

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

            # Category routing already happened in select_tables_for_query, which
            # works from this tenant's vocabulary instead of a fixed taxonomy.

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

            # Was 15. This is a cached catalog probe, not a per-turn query, so a
            # tenant with a couple of dozen services was being cut off — which is
            # why "tell me about your products" only ever named a few.
            if self.sql.dialect == "sqlserver":
                sql = sql.replace("SELECT", f"SELECT TOP {_CATALOG_ROW_LIMIT}", 1)
            else:
                sql += f" LIMIT {_CATALOG_ROW_LIMIT}"

            try:
                rows = await self.sql.fetch_all(sql, params or None)
                if not rows and not is_generic and query and len(query.strip()) >= 4 and params:
                    # Fuzzy prefix fallback: e.g. "Grabingo" -> "Grab%"
                    prefix = query.strip()[:4]
                    fuzzy_params = {"q": f"%{_escape_like(prefix)}%"}
                    try:
                        rows = await self.sql.fetch_all(sql, fuzzy_params)
                    except Exception:
                        pass
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
                        kind = None
                        for i in range(min(len(r), len(field_names))):
                            key = str(field_names[i])
                            val = r[i]
                            if val is None or str(val).strip() == "":
                                continue
                            if key.lower() in name_keys and display_name is None:
                                display_name = str(val).strip()
                            elif key.lower() in _KIND_KEYS and kind is None:
                                # Surfaced separately and first, so the model can
                                # group items by type instead of listing them flat.
                                kind = str(val).strip()
                            elif key.lower() not in {"id"}:
                                extras.append(f"{key}: {val}")
                        if kind:
                            extras.insert(0, f"category: {kind}")
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
