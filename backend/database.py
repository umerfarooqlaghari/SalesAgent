import os
import logging
import sqlite3
import dns.resolver

# Monkeypatch dnspython Resolver to force reliable nameservers globally, bypassing router SERVFAIL DNS errors
_orig_resolver_init = dns.resolver.Resolver.__init__
def _patched_resolver_init(self, *args, **kwargs):
    _orig_resolver_init(self, *args, **kwargs)
    self.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']
dns.resolver.Resolver.__init__ = _patched_resolver_init
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']


from typing import Dict, Any, List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from .config import settings

logger = logging.getLogger(__name__)

# Path to SQLite POS Database
DB_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB_PATH = os.path.join(DB_DIR, "pos_database.db")

class Database:
    client: Optional[AsyncIOMotorClient] = None
    db: Any = None

    def connect(self):
        if not self.client:
            self.client = AsyncIOMotorClient(settings.MONGODB_URI)
            self.db = self.client[settings.DATABASE_NAME]
            logger.info("Connected to MongoDB Atlas")
            
        # Initialize SQLite POS / Inventory DB
        self.init_sqlite_pos()

    def disconnect(self):
        if self.client:
            self.client.close()
            self.client = None
            self.db = None
            logger.info("Disconnected from MongoDB Atlas")

    def init_sqlite_pos(self):
        """
        Creates and seeds read-only inventory & orders database using SQLite.
        """
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        
        # Create products table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                stock_quantity INTEGER NOT NULL,
                description TEXT
            )
        """)
        
        # Create orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                customer_email TEXT NOT NULL,
                customer_phone TEXT,
                status TEXT NOT NULL,
                total_price TEXT NOT NULL,
                items TEXT NOT NULL
            )
        """)
        
        # Check and seed products
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            products = [
                ("SaaS Starter Package", "$49/mo", 1500, "Basic outreach package with 1 user license."),
                ("SaaS Professional Package", "$199/mo", 500, "Standard plan with 5 user licenses & advanced tools."),
                ("SaaS Enterprise License", "$999/mo", 50, "Unlimited user licenses, custom integrations & dedicated success rep.")
            ]
            cursor.executemany("INSERT INTO products (name, price, stock_quantity, description) VALUES (?, ?, ?, ?)", products)
            logger.info("Simulated POS products seeded successfully.")
            
        # Check and seed orders
        cursor.execute("SELECT COUNT(*) FROM orders")
        if cursor.fetchone()[0] == 0:
            orders = [
                (1001, "cto@cloudgrid.io", "+14155552671", "Shipped", "$999/mo", "1x SaaS Enterprise License"),
                (1002, "sales@growthcorp.com", "+12125559876", "Processing", "$199/mo", "1x SaaS Professional Package"),
                (1003, "john@test.com", None, "Delivered", "$49/mo", "1x SaaS Starter Package")
            ]
            cursor.executemany("INSERT INTO orders (id, customer_email, customer_phone, status, total_price, items) VALUES (?, ?, ?, ?, ?, ?)", orders)
            logger.info("Simulated POS orders seeded successfully.")
            
        conn.commit()
        conn.close()

db_client = Database()

def get_db():
    if db_client.db is None:
        db_client.connect()
    return db_client.db

async def seed_default_api_key():
    """Legacy shim — delegates to multi-tenant seed."""
    from backend.tenant.registry import seed_default_tenant

    await seed_default_tenant()


async def validate_api_key_in_db(api_key: str) -> bool:
    """Legacy shim — use resolve_tenant_by_api_key for new code."""
    from backend.tenant.registry import resolve_tenant_by_api_key

    tenant = await resolve_tenant_by_api_key(api_key)
    return tenant is not None


async def save_lead(tenant_id: str, thread_id: str, lead_data: Dict[str, Any]) -> Dict[str, Any]:
    from backend.repositories.leads import LeadRepository

    return await LeadRepository(tenant_id).upsert(thread_id, lead_data)


async def get_lead(tenant_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
    from backend.repositories.leads import LeadRepository

    return await LeadRepository(tenant_id).get_by_thread(thread_id)


async def list_leads(tenant_id: str) -> List[Dict[str, Any]]:
    from backend.repositories.leads import LeadRepository

    return await LeadRepository(tenant_id).list_all()

async def save_conversation_message(
    tenant_id: str,
    thread_id: str,
    role: str,
    message: str,
    thought: Optional[str] = None,
    source: Optional[str] = None,
):
    from datetime import datetime, timezone

    db = get_db()
    entry = {
        "role": role,
        "content": message,
        "thought": thought,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if source:
        entry["source"] = source
    await db.conversations.update_one(
        {"tenant_id": tenant_id, "thread_id": thread_id},
        {"$push": {"messages": entry}, "$setOnInsert": {"tenant_id": tenant_id, "thread_id": thread_id}},
        upsert=True,
    )

async def get_conversation(tenant_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = await db.conversations.find_one({"tenant_id": tenant_id, "thread_id": thread_id})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc

async def list_conversations(tenant_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.conversations.find({"tenant_id": tenant_id})
    convs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        convs.append(doc)
    return convs

async def rename_conversation(tenant_id: str, thread_id: str, title: str):
    db = get_db()
    await db.conversations.update_one(
        {"tenant_id": tenant_id, "thread_id": thread_id},
        {"$set": {"title": title}, "$setOnInsert": {"tenant_id": tenant_id, "thread_id": thread_id}},
        upsert=True,
    )

async def delete_conversation(tenant_id: str, thread_id: str):
    db = get_db()
    await db.conversations.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.leads.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.checkpoints.delete_many({"thread_id": thread_id})
    await db.writes.delete_many({"thread_id": thread_id})

# ---------------------------------------------------------------------------
# Appointment booking helpers
# ---------------------------------------------------------------------------

async def check_slot_available(tenant_id: str, date_str: str, time_str: str) -> bool:
    """Returns True if the requested date+time slot has no existing booking for this tenant."""
    db = get_db()
    existing = await db.appointments.find_one({
        "tenant_id": tenant_id,
        "date": date_str,
        "time": time_str,
        "status": {"$ne": "cancelled"}
    })
    return existing is None

async def create_appointment(
    tenant_id: str,
    thread_id: str,
    name: str,
    email: str,
    phone: str,
    date_str: str,
    time_str: str,
    notes: str = "",
) -> Dict[str, Any]:
    """Saves a confirmed appointment to MongoDB and returns the booking document."""
    from datetime import datetime, timezone
    db = get_db()
    doc = {
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "name": name,
        "email": email,
        "phone": phone,
        "date": date_str,
        "time": time_str,
        "notes": notes,
        "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.appointments.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc

async def list_appointments(tenant_id: str) -> List[Dict[str, Any]]:
    """Returns all appointments for a tenant ordered by date/time."""
    db = get_db()
    cursor = db.appointments.find({"tenant_id": tenant_id}).sort([("date", 1), ("time", 1)])
    appts = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        appts.append(doc)
    return appts

# ---------------------------------------------------------------------------
# Customer order helpers (voice/chat purchases)
# ---------------------------------------------------------------------------

def _lookup_product(product_name: str) -> Optional[Dict[str, Any]]:
    """Find a product in the SQLite POS catalog by fuzzy name match."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    try:
        query = product_name.strip()
        cursor.execute(
            "SELECT id, name, price, stock_quantity, description FROM products WHERE LOWER(name) LIKE LOWER(?)",
            (f"%{query}%",)
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "price": row[2],
                "stock_quantity": row[3],
                "description": row[4],
            }

        # Map common shorthand / tier names to packages
        aliases = {
            "starter": "SaaS Starter",
            "professional": "SaaS Professional",
            "enterprise": "SaaS Enterprise",
            "basic": "SaaS Starter",
            "pro": "SaaS Professional",
        }
        lowered = query.lower()
        for key, prefix in aliases.items():
            if key in lowered:
                cursor.execute(
                    "SELECT id, name, price, stock_quantity, description FROM products WHERE LOWER(name) LIKE LOWER(?)",
                    (f"%{prefix}%",)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "name": row[1],
                        "price": row[2],
                        "stock_quantity": row[3],
                        "description": row[4],
                    }
        return None
    finally:
        conn.close()

def _create_sqlite_order(
    customer_email: str,
    customer_phone: str,
    product_name: str,
    total_price: str,
) -> int:
    """Insert a new order into the SQLite POS database and return the order id."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO orders (customer_email, customer_phone, status, total_price, items)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_email,
                customer_phone or None,
                "Pending Agent Follow-up",
                total_price,
                f"1x {product_name}",
            ),
        )
        order_id = cursor.lastrowid
        conn.commit()
        return int(order_id)
    finally:
        conn.close()

async def create_order(
    tenant_id: str,
    thread_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    product_name: str,
    total_price: str,
    sqlite_order_id: int,
) -> Dict[str, Any]:
    """Persist a customer order to MongoDB."""
    from datetime import datetime, timezone

    db = get_db()
    doc = {
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "order_id": sqlite_order_id,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "product_name": product_name,
        "total_price": total_price,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.orders.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc

async def list_orders(tenant_id: str) -> List[Dict[str, Any]]:
    """Returns all customer orders for a tenant, newest first."""
    db = get_db()
    cursor = db.orders.find({"tenant_id": tenant_id}).sort([("created_at", -1)])
    orders = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        orders.append(doc)
    return orders

# ---------------------------------------------------------------------------
# Voice call ↔ console chat linking (typed details during calls)
# ---------------------------------------------------------------------------

async def link_voice_call(tenant_id: str, call_id: str, console_thread_id: str) -> None:
    """Link a Vapi call to the console chat thread so typed messages are visible to the voice agent."""
    from datetime import datetime, timezone

    db = get_db()
    await db.voice_call_links.update_one(
        {"call_id": call_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "call_id": call_id,
                "console_thread_id": console_thread_id,
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )


async def register_voice_session(tenant_id: str, console_thread_id: str) -> None:
    """Register tenant scope for a console thread before Vapi assigns a call id (avoids race on first LLM turn)."""
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.voice_call_sessions.update_one(
        {"console_thread_id": console_thread_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "console_thread_id": console_thread_id,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def _extract_voice_metadata(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge metadata from Vapi payload paths (start() metadata may land in different fields)."""
    payload = payload or {}
    call_data = payload.get("call") or {}
    meta: Dict[str, Any] = {}
    for src in (
        payload.get("metadata"),
        call_data.get("metadata"),
        (call_data.get("assistantOverrides") or {}).get("metadata"),
        (call_data.get("assistant") or {}).get("metadata"),
    ):
        if isinstance(src, dict):
            meta.update(src)
    return meta


async def resolve_voice_thread(
    call_data: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]] = None,
) -> tuple[str, Optional[str], str]:
    """
    Resolve which thread the voice agent should use and the tenant scope.
    Prefers explicit link, then call metadata from Vapi start(), else isolated vapi_{call_id} thread.
    """
    call_data = call_data or {}
    call_id = call_data.get("id") or "vapi_default_session"
    metadata = _extract_voice_metadata(payload or {"call": call_data})
    console_from_meta = metadata.get("console_thread_id") or metadata.get("consoleThreadId")
    tenant_id = metadata.get("tenant_id") or metadata.get("tenantId")

    db = get_db()

    if console_from_meta and not tenant_id:
        session = await db.voice_call_sessions.find_one({"console_thread_id": console_from_meta})
        if session and session.get("tenant_id"):
            tenant_id = session["tenant_id"]

    link_doc = await db.voice_call_links.find_one({"call_id": call_id})
    if link_doc:
        tenant_id = link_doc.get("tenant_id") or tenant_id
        linked_thread = link_doc.get("console_thread_id")
        if linked_thread:
            return linked_thread, linked_thread, tenant_id or settings.DEFAULT_TENANT_ID

    if console_from_meta:
        if not tenant_id:
            session = await db.voice_call_sessions.find_one({"console_thread_id": console_from_meta})
            tenant_id = (session or {}).get("tenant_id")
        tenant_id = tenant_id or settings.DEFAULT_TENANT_ID
        await link_voice_call(tenant_id, call_id, console_from_meta)
        return console_from_meta, console_from_meta, tenant_id

    isolated = f"vapi_{call_id}"
    return isolated, None, tenant_id or settings.DEFAULT_TENANT_ID

async def get_linked_console_thread(call_id: str) -> Optional[str]:
    db = get_db()
    doc = await db.voice_call_links.find_one({"call_id": call_id})
    return doc.get("console_thread_id") if doc else None

async def unlink_voice_call(call_id: str) -> None:
    db = get_db()
    await db.voice_call_links.delete_one({"call_id": call_id})

async def get_recent_typed_chat_messages(
    tenant_id: str,
    thread_id: str,
    since_iso: Optional[str] = None,
    limit: int = 10,
) -> List[str]:
    """Return recent user-typed chat messages for a thread (optionally after link time)."""
    conv = await get_conversation(tenant_id, thread_id)
    if not conv:
        return []

    from datetime import datetime

    cutoff = None
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except ValueError:
            cutoff = None

    typed: List[str] = []
    for entry in conv.get("messages", []):
        if entry.get("role") != "user":
            continue
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        # Skip messages that look like pure voice transcripts mirrored from call
        if entry.get("source") == "voice":
            continue
        if cutoff and entry.get("timestamp"):
            try:
                ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except ValueError:
                pass
        typed.append(content)

    return typed[-limit:]

def _normalize_phone(phone: str) -> str:
    """Strip non-digits for loose phone matching."""
    return "".join(c for c in phone if c.isdigit())

async def find_active_appointments(
    tenant_id: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    thread_id: Optional[str] = None,
    date_str: Optional[str] = None,
    time_str: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find non-cancelled appointments matching caller identity within a tenant."""
    db = get_db()
    filters: List[Dict[str, Any]] = [
        {"tenant_id": tenant_id},
        {"status": {"$ne": "cancelled"}},
    ]

    if thread_id:
        filters.append({"thread_id": thread_id})

    identity_clauses: List[Dict[str, Any]] = []
    if email and email.strip():
        identity_clauses.append({"email": {"$regex": f"^{email.strip()}$", "$options": "i"}})
    if phone and phone.strip():
        normalized = _normalize_phone(phone)
        if normalized:
            identity_clauses.append({"phone": {"$regex": normalized[-10:]}})

    if identity_clauses:
        filters.append({"$or": identity_clauses})

    if date_str and date_str.strip():
        filters.append({"date": date_str.strip()})
    if time_str and time_str.strip():
        filters.append({"time": time_str.strip()})

    query: Dict[str, Any] = {"$and": filters} if len(filters) > 1 else filters[0]
    cursor = db.appointments.find(query).sort([("date", 1), ("time", 1)])
    results = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results

async def cancel_appointment_record(tenant_id: str, appt_id: str) -> bool:
    """Mark an appointment as cancelled in MongoDB."""
    from bson import ObjectId

    db = get_db()
    result = await db.appointments.update_one(
        {"_id": ObjectId(appt_id), "tenant_id": tenant_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    return result.modified_count > 0

async def reschedule_appointment_record(
    tenant_id: str,
    appt_id: str,
    new_date: str,
    new_time: str,
) -> bool:
    """Move an appointment to a new date/time."""
    from bson import ObjectId

    db = get_db()
    result = await db.appointments.update_one(
        {"_id": ObjectId(appt_id), "tenant_id": tenant_id, "status": {"$ne": "cancelled"}},
        {"$set": {"date": new_date.strip(), "time": new_time.strip()}},
    )
    return result.modified_count > 0

async def find_active_orders(
    tenant_id: str,
    order_id: Optional[int] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find non-cancelled orders matching caller identity within a tenant."""
    db = get_db()
    filters: List[Dict[str, Any]] = [
        {"tenant_id": tenant_id},
        {"status": {"$ne": "cancelled"}},
    ]

    if order_id is not None:
        filters.append({"order_id": order_id})
    if thread_id:
        filters.append({"thread_id": thread_id})

    identity_clauses: List[Dict[str, Any]] = []
    if email and email.strip():
        identity_clauses.append({"customer_email": {"$regex": f"^{email.strip()}$", "$options": "i"}})
    if phone and phone.strip():
        normalized = _normalize_phone(phone)
        if normalized:
            identity_clauses.append({"customer_phone": {"$regex": normalized[-10:]}})

    if identity_clauses:
        filters.append({"$or": identity_clauses})

    query: Dict[str, Any] = {"$and": filters} if len(filters) > 1 else filters[0]
    cursor = db.orders.find(query).sort([("created_at", -1)])
    results = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results

def _cancel_sqlite_order(order_id: int) -> bool:
    """Mark a SQLite POS order as cancelled."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE orders SET status = ? WHERE id = ? AND status != ?",
            ("Cancelled", order_id, "Cancelled"),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

async def cancel_order_record(tenant_id: str, order_id: int) -> bool:
    """Mark an order as cancelled in MongoDB and SQLite."""
    db = get_db()
    result = await db.orders.update_one(
        {"tenant_id": tenant_id, "order_id": order_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    sqlite_updated = _cancel_sqlite_order(order_id)
    return result.modified_count > 0 or sqlite_updated

