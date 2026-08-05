import logging
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from adapter_hub.adapters.base import Connector
from adapter_hub.adapters.canonical import Product, Customer, Order, OrderItem, Record

logger = logging.getLogger(__name__)

class PostgresConnector(Connector):
    """
    Postgres SQL Adapter supporting both schema discovery 
    and canonical syncing of B2B entities.
    """
    
    def __init__(self, config: Dict[str, Any], tenant_id: str, agent_id: str):
        super().__init__(config, tenant_id, agent_id)
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 5432)
        self.database = config.get("database", "")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.schema = config.get("schema", "public")
        self.connection_url = config.get("connection_url")
        
        # Determine the engine connection string
        if self.connection_url:
            self.url = self.connection_url
        elif "sqlite" in self.host or self.database.endswith(".db"):
            self.url = f"sqlite+aiosqlite:///{self.database}" if self.database else "sqlite+aiosqlite:///:memory:"
        else:
            ssl = config.get("ssl", True)
            q = "?ssl=require" if ssl and "sqlite" not in str(self.host) else ""
            self.url = (
                f"postgresql+asyncpg://{self.username}:{self.password}"
                f"@{self.host}:{self.port}/{self.database}{q}"
            )

        self.engine = create_async_engine(self.url, pool_pre_ping=True)

    async def test_connection(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Postgres connection test failed for tenant {self.tenant_id}: {e}")
            raise ConnectionError(f"Database connection failed: {e}")

    async def discover_schema(self) -> List[Dict[str, Any]]:
        """
        Introspect tables and columns from information_schema.
        """
        # If SQLite, use special pragma statements
        if "sqlite" in self.url:
            return await self._discover_sqlite_schema()

        tables: List[Dict[str, Any]] = []
        try:
            async with self.engine.connect() as conn:
                # Query table names
                t_rows = await conn.execute(
                    text(
                        """
                        SELECT table_name 
                        FROM information_schema.tables 
                        WHERE table_schema = :schema AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                        """
                    ),
                    {"schema": self.schema}
                )
                table_names = [r[0] for r in t_rows.fetchall()]
                
                for tname in table_names:
                    c_rows = await conn.execute(
                        text(
                            """
                            SELECT column_name, data_type 
                            FROM information_schema.columns 
                            WHERE table_schema = :schema AND table_name = :table
                            ORDER BY ordinal_position
                            """
                        ),
                        {"schema": self.schema, "table": tname}
                    )
                    tables.append({
                        "name": tname,
                        "columns": [{"name": r[0], "type": r[1]} for r in c_rows.fetchall()]
                    })
        except Exception as e:
            logger.error(f"Postgres schema discovery failed: {e}")
            raise RuntimeError(f"Schema discovery failed: {e}")
            
        return tables

    async def _discover_sqlite_schema(self) -> List[Dict[str, Any]]:
        tables: List[Dict[str, Any]] = []
        try:
            async with self.engine.connect() as conn:
                t_rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
                table_names = [r[0] for r in t_rows.fetchall()]
                
                for tname in table_names:
                    c_rows = await conn.execute(text(f"PRAGMA table_info({tname})"))
                    # PRAGMA returns (cid, name, type, notnull, dflt_value, pk)
                    columns = [{"name": r[1], "type": r[2]} for r in c_rows.fetchall()]
                    tables.append({
                        "name": tname,
                        "columns": columns
                    })
        except Exception as e:
            logger.error(f"SQLite schema discovery failed: {e}")
            raise
        return tables

    async def sync_data(self, whitelist: Dict[str, Any]) -> List[Any]:
        """
        Queries whitelisted tables and columns, normalizes the data, 
        and returns list of canonical entities.
        Whitelist structure example:
        {
          "products": {
            "table": "products_catalog",
            "columns": {"id": "sku", "name": "title", "price": "cost", "stock_quantity": "qty"}
          },
          "customers": {
            "table": "clients",
            "columns": {"id": "id", "name": "full_name", "email": "email_addr", "company": "firm"}
          }
        }
        """
        canonical_entities = []
        
        async with self.engine.connect() as conn:
            # 1. Sync Products
            if "products" in whitelist:
                prod_conf = whitelist["products"]
                table = prod_conf.get("table")
                cols = prod_conf.get("columns", {})
                if table and cols:
                    query_cols = []
                    col_keys = []
                    for canonical_field, db_col in cols.items():
                        query_cols.append(f'"{db_col}"' if "sqlite" not in self.url else f"`{db_col}`")
                        col_keys.append(canonical_field)
                    
                    if query_cols:
                        select_clause = ", ".join(query_cols)
                        q = text(f"SELECT {select_clause} FROM {table}")
                        rows = (await conn.execute(q)).fetchall()
                        for row in rows:
                            row_dict = dict(zip(col_keys, row))
                            # Coerce price and stock to proper types
                            price_val = float(row_dict.get("price", 0.0) or 0.0)
                            stock_val = int(row_dict.get("stock_quantity", 0) or 0)
                            
                            product = Product(
                                id=str(row_dict.get("id")),
                                name=str(row_dict.get("name", "Unnamed Product")),
                                price=price_val,
                                stock_quantity=stock_val,
                                description=row_dict.get("description"),
                                category=row_dict.get("category"),
                                raw_metadata=dict(zip(cols.values(), row))
                            )
                            canonical_entities.append(product)

            # 2. Sync Customers
            if "customers" in whitelist:
                cust_conf = whitelist["customers"]
                table = cust_conf.get("table")
                cols = cust_conf.get("columns", {})
                if table and cols:
                    query_cols = []
                    col_keys = []
                    for canonical_field, db_col in cols.items():
                        query_cols.append(f'"{db_col}"' if "sqlite" not in self.url else f"`{db_col}`")
                        col_keys.append(canonical_field)
                    
                    if query_cols:
                        select_clause = ", ".join(query_cols)
                        q = text(f"SELECT {select_clause} FROM {table}")
                        rows = (await conn.execute(q)).fetchall()
                        for row in rows:
                            row_dict = dict(zip(col_keys, row))
                            customer = Customer(
                                id=str(row_dict.get("id")),
                                name=str(row_dict.get("name", "Unknown")),
                                email=str(row_dict.get("email", "")),
                                phone=row_dict.get("phone"),
                                company=row_dict.get("company"),
                                status=str(row_dict.get("status", "active")),
                                raw_metadata=dict(zip(cols.values(), row))
                            )
                            canonical_entities.append(customer)

            # 3. Sync Orders
            if "orders" in whitelist:
                ord_conf = whitelist["orders"]
                table = ord_conf.get("table")
                cols = ord_conf.get("columns", {})
                if table and cols:
                    query_cols = []
                    col_keys = []
                    for canonical_field, db_col in cols.items():
                        query_cols.append(f'"{db_col}"' if "sqlite" not in self.url else f"`{db_col}`")
                        col_keys.append(canonical_field)
                    
                    if query_cols:
                        select_clause = ", ".join(query_cols)
                        q = text(f"SELECT {select_clause} FROM {table}")
                        rows = (await conn.execute(q)).fetchall()
                        for row in rows:
                            row_dict = dict(zip(col_keys, row))
                            # Coerce total_price
                            total_p = float(row_dict.get("total_price", 0.0) or 0.0)
                            items_str = row_dict.get("items", "")
                            
                            # Parse items string, e.g. "1x SaaS Professional Package" or JSON list
                            items_list = []
                            if items_str:
                                if isinstance(items_str, str) and (items_str.startswith("[") or items_str.startswith("{")):
                                    import json
                                    try:
                                        parsed = json.loads(items_str)
                                        if isinstance(parsed, list):
                                            for pi in parsed:
                                                items_list.append(OrderItem(
                                                    product_name=pi.get("product_name", "Unknown Item"),
                                                    quantity=pi.get("quantity", 1),
                                                    unit_price=float(pi.get("unit_price", 0.0))
                                                ))
                                    except Exception:
                                        pass
                                if not items_list:
                                    # Fallback simple string parser
                                    items_list.append(OrderItem(
                                        product_name=str(items_str),
                                        quantity=1,
                                        unit_price=total_p
                                    ))

                            order = Order(
                                id=str(row_dict.get("id")),
                                customer_id=row_dict.get("customer_id"),
                                customer_email=str(row_dict.get("customer_email", "")),
                                customer_phone=row_dict.get("customer_phone"),
                                status=str(row_dict.get("status", "Pending")),
                                total_price=total_p,
                                items=items_list,
                                raw_metadata=dict(zip(cols.values(), row))
                            )
                            canonical_entities.append(order)

            # 4. Sync arbitrary custom tables (productions, sets, POs, …)
            for table_conf in whitelist.get("tables") or []:
                if not isinstance(table_conf, dict):
                    continue
                table = table_conf.get("table")
                cols = table_conf.get("columns") or {}
                label = table_conf.get("label") or table or "Record"
                if isinstance(cols, list):
                    cols = {str(c): str(c) for c in cols}
                if not table or not cols:
                    continue

                query_cols = []
                col_keys = []
                for canonical_field, db_col in cols.items():
                    query_cols.append(f'"{db_col}"' if "sqlite" not in self.url else f"`{db_col}`")
                    col_keys.append(str(canonical_field))

                if not query_cols:
                    continue

                select_clause = ", ".join(query_cols)
                schema = self.schema or "public"
                qualified = (
                    f'"{schema}"."{table}"'
                    if "sqlite" not in self.url
                    else table
                )
                # LIMIT keeps sync bounded for large production DBs
                q = text(f"SELECT {select_clause} FROM {qualified} LIMIT 500")
                try:
                    rows = (await conn.execute(q)).fetchall()
                except Exception as e:
                    logger.warning("Skipping table %s during sync: %s", table, e)
                    continue

                for idx, row in enumerate(rows):
                    row_dict = {k: v for k, v in zip(col_keys, row) if v is not None}
                    rid = str(row_dict.get("id") or row_dict.get("name") or f"{table}_{idx}")
                    summary_parts = [f"{label}:"]
                    for k, v in row_dict.items():
                        summary_parts.append(f"{k}={v}")
                    summary = " ".join(summary_parts)
                    canonical_entities.append(
                        Record(
                            id=f"{table}:{rid}",
                            entity_label=str(label),
                            table_name=str(table),
                            summary=summary,
                            fields=row_dict,
                            raw_metadata={"table": table, "label": label},
                        )
                    )

        return canonical_entities
