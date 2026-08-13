import os
import logging
import re
import sqlite3

from typing import Dict, Any, List, NamedTuple, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from .config import get_mongodb_connection_uri, settings

logger = logging.getLogger(__name__)

# Path to SQLite POS Database
DB_DIR = os.environ.get("SQLITE_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
SQLITE_DB_PATH = os.path.join(DB_DIR, "pos_database.db")

class Database:
    client: Optional[AsyncIOMotorClient] = None
    db: Any = None

    def connect(self):
        if not self.client:
            self.client = AsyncIOMotorClient(get_mongodb_connection_uri())
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

async def list_conversations(tenant_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    # S20: an unbounded find() plus every message array pulled the full chat
    # history of every thread for a tenant into memory on one admin page load.
    db = get_db()
    cursor = (
        db.conversations.find({"tenant_id": tenant_id}, {"messages": 0})
        .sort([("_id", -1)])
        .limit(min(limit, 500))
    )
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
    """
    T10: the checkpoint deletes used to filter on thread_id alone, so
    `DELETE /api/conversations/<any thread id>` destroyed another tenant's live
    agent state. Checkpoints are stored under the namespaced key (see T09).
    """
    from backend.tenant.thread_scope import scoped_thread_id

    db = get_db()
    await db.conversations.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.leads.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})

    ckpt_key = scoped_thread_id(tenant_id, thread_id)
    await db.checkpoints.delete_many({"thread_id": ckpt_key})
    await db.writes.delete_many({"thread_id": ckpt_key})

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

async def list_appointments(tenant_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Returns all appointments for a tenant ordered by date/time."""
    db = get_db()
    cursor = (
        db.appointments.find({"tenant_id": tenant_id})
        .sort([("date", 1), ("time", 1)])
        .limit(min(limit, 500))
    )
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

async def list_orders(tenant_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Returns all customer orders for a tenant, newest first."""
    db = get_db()
    cursor = db.orders.find({"tenant_id": tenant_id}).sort([("created_at", -1)]).limit(min(limit, 500))
    orders = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        orders.append(doc)
    return orders

# ---------------------------------------------------------------------------
# Voice call ↔ console chat linking (typed details during calls)
# ---------------------------------------------------------------------------

async def link_voice_call(tenant_id: str, call_id: str, console_thread_id: str) -> str:
    """
    Link a Vapi call to the console chat thread so typed messages are visible to
    the voice agent. Returns the ISO timestamp it wrote, so callers do not have
    to re-read the document (P09).
    """
    from datetime import datetime, timezone

    db = get_db()
    linked_at = datetime.now(timezone.utc).isoformat()
    # T11: the filter used to be {call_id} alone, and call_id was globally unique,
    # so any tenant could POST a victim's live call id and rewrite the row's
    # tenant_id — redirecting that customer's conversation into their own
    # transcript and billing the minutes wherever they chose.
    existing = await db.voice_call_links.find_one({"call_id": call_id}, {"tenant_id": 1})
    if existing and existing.get("tenant_id") not in (None, tenant_id):
        logger.warning(
            "Refusing cross-tenant voice link: call_id=%s owned by %s, requested by %s",
            call_id, existing.get("tenant_id"), tenant_id,
        )
        raise PermissionError("This call is linked to a different tenant.")

    await db.voice_call_links.update_one(
        {"call_id": call_id, "tenant_id": tenant_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "call_id": call_id,
                "console_thread_id": console_thread_id,
                "linked_at": linked_at,
            }
        },
        upsert=True,
    )

    # S15: the end-of-call billing webhook falls back to
    # `voice_call_sessions.find_one({"call_id": ...})` when no link row is found,
    # but NOTHING ever wrote call_id into that collection — the branch was dead,
    # unindexed, and the minutes for those calls were silently never metered.
    # The session is registered before Vapi assigns a call id, so this is the
    # first moment the id exists: stamp it on. Scoped by tenant_id so it can
    # never attach a call to someone else's session.
    try:
        await db.voice_call_sessions.update_one(
            {"console_thread_id": console_thread_id, "tenant_id": tenant_id},
            {"$set": {"call_id": call_id, "call_id_linked_at": linked_at}},
        )
    except Exception:
        logger.debug("Could not stamp call_id onto the voice session", exc_info=True)

    return linked_at


async def register_voice_session(tenant_id: str, console_thread_id: str) -> None:
    """Register tenant scope for a console thread before Vapi assigns a call id (avoids race on first LLM turn)."""
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    # T12: keyed on console_thread_id alone, an attacker could register a victim's
    # (guessable) embed thread id and have resolve_voice_thread serve the victim's
    # caller the ATTACKER's system prompt and knowledge base.
    existing = await db.voice_call_sessions.find_one(
        {"console_thread_id": console_thread_id}, {"tenant_id": 1}
    )
    if existing and existing.get("tenant_id") not in (None, tenant_id):
        logger.warning(
            "Refusing cross-tenant voice session: console_thread_id=%s owned by %s, requested by %s",
            console_thread_id, existing.get("tenant_id"), tenant_id,
        )
        raise PermissionError("This session belongs to a different tenant.")

    await db.voice_call_sessions.update_one(
        {"console_thread_id": console_thread_id, "tenant_id": tenant_id},
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


class VoiceThread(NamedTuple):
    """
    Resolution result for one spoken turn.

    P09: this used to be a bare 3-tuple, so the caller re-read voice_call_links
    immediately afterwards just to get `linked_at`. Returning the document we
    already fetched removes a Mongo round-trip from every turn.

    Access fields by name. Three-way positional unpacking no longer works, since
    `link_doc` is a fourth field.
    """

    agent_thread_id: str
    console_thread_id: Optional[str]
    tenant_id: str
    link_doc: Optional[Dict[str, Any]] = None

    @property
    def linked_at(self) -> Optional[str]:
        return (self.link_doc or {}).get("linked_at")


async def resolve_voice_thread(
    call_data: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]] = None,
) -> "VoiceThread":
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
            return VoiceThread(
                linked_thread, linked_thread,
                tenant_id or settings.DEFAULT_TENANT_ID, link_doc,
            )

    if console_from_meta:
        if not tenant_id:
            session = await db.voice_call_sessions.find_one({"console_thread_id": console_from_meta})
            tenant_id = (session or {}).get("tenant_id")
        tenant_id = tenant_id or settings.DEFAULT_TENANT_ID
        try:
            linked_at = await link_voice_call(tenant_id, call_id, console_from_meta)
        except PermissionError:
            # T11 now rejects a cross-tenant claim. On this path that must degrade
            # to an isolated thread rather than raising — a raised exception here
            # would 500 the whole spoken turn.
            logger.warning(
                "Voice link contested for call %s (tenant %s) — using an isolated thread",
                call_id, tenant_id,
            )
            return VoiceThread(f"vapi_{call_id}", None, tenant_id, link_doc)
        # Freshly linked above; link_voice_call hands back the timestamp it wrote
        # so typed messages from before the link are not pulled into this turn.
        return VoiceThread(
            console_from_meta, console_from_meta, tenant_id,
            {"call_id": call_id, "tenant_id": tenant_id,
             "console_thread_id": console_from_meta,
             "linked_at": linked_at},
        )

    isolated = f"vapi_{call_id}"
    return VoiceThread(isolated, None, tenant_id or settings.DEFAULT_TENANT_ID, link_doc)

async def get_linked_console_thread(call_id: str) -> Optional[str]:
    db = get_db()
    doc = await db.voice_call_links.find_one({"call_id": call_id})
    return doc.get("console_thread_id") if doc else None

async def unlink_voice_call(tenant_id: str, call_id: str) -> bool:
    """T13: scoped to the caller's tenant; returns whether anything was removed."""
    db = get_db()
    result = await db.voice_call_links.delete_one({"call_id": call_id, "tenant_id": tenant_id})
    return result.deleted_count > 0

async def get_recent_typed_chat_messages(
    tenant_id: str,
    thread_id: str,
    since_iso: Optional[str] = None,
    limit: int = 10,
) -> List[str]:
    """Return recent user-typed chat messages for a thread (optionally after link time)."""
    conv = await get_conversation(tenant_id, thread_id)
    if not conv and (thread_id or "").startswith("vapi_"):
        call_id = thread_id.replace("vapi_", "")
        linked = await get_linked_console_thread(call_id)
        if linked:
            conv = await get_conversation(tenant_id, linked)
    elif conv and (thread_id or "").startswith("vapi_"):
        call_id = thread_id.replace("vapi_", "")
        linked = await get_linked_console_thread(call_id)
        if linked:
            linked_conv = await get_conversation(tenant_id, linked)
            if linked_conv:
                # Merge messages from both threads
                msgs = list(conv.get("messages", [])) + list(linked_conv.get("messages", []))
                conv = {"messages": msgs}

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

    if not typed:
        try:
            db = get_db()
            cursor = db.conversations.find({"tenant_id": tenant_id}).sort("updated_at", -1).limit(5)
            async for doc in cursor:
                for entry in doc.get("messages", []):
                    if entry.get("role") == "user" and entry.get("source") != "voice":
                        c = (entry.get("content") or "").strip()
                        if c and ("@" in c or any(ch.isdigit() for ch in c)):
                            typed.append(c)
        except Exception:
            pass

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
    """Find non-cancelled appointments matching caller identity or thread within a tenant."""
    db = get_db()
    filters: List[Dict[str, Any]] = [
        {"tenant_id": tenant_id},
        {"status": {"$ne": "cancelled"}},
    ]

    identity_clauses: List[Dict[str, Any]] = []
    if email and email.strip():
        identity_clauses.append({"email": {"$regex": f"^{re.escape(email.strip())}$", "$options": "i"}})
    if phone and phone.strip():
        normalized = _normalize_phone(phone)
        if normalized:
            identity_clauses.append({"phone": {"$regex": normalized[-10:]}})

    if identity_clauses:
        filters.append({"$or": identity_clauses})
    elif thread_id:
        filters.append({"thread_id": thread_id})

    if date_str and date_str.strip():
        filters.append({"date": {"$regex": re.escape(date_str.strip()), "$options": "i"}})
    if time_str and time_str.strip():
        filters.append({"time": {"$regex": re.escape(time_str.strip()), "$options": "i"}})

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
    try:
        query = {"_id": ObjectId(appt_id), "tenant_id": tenant_id, "status": {"$ne": "cancelled"}}
    except Exception:
        query = {"id": appt_id, "tenant_id": tenant_id, "status": {"$ne": "cancelled"}}

    result = await db.appointments.update_one(
        query,
        {"$set": {"status": "cancelled"}},
    )
    return result.modified_count > 0

async def delete_appointment_record(tenant_id: str, appt_id: str) -> bool:
    """Permanently delete an appointment document from MongoDB."""
    from bson import ObjectId

    db = get_db()
    try:
        res = await db.appointments.delete_one({"_id": ObjectId(appt_id), "tenant_id": tenant_id})
        if res.deleted_count > 0:
            return True
    except Exception:
        pass
    res = await db.appointments.delete_one({"id": appt_id, "tenant_id": tenant_id})
    return res.deleted_count > 0

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

async def update_appointment_fields(
    tenant_id: str,
    appt_id: str,
    updates: Dict[str, Any],
) -> bool:
    """Update name, email, phone, date, time, or notes on an active appointment."""
    from bson import ObjectId

    db = get_db()
    clean_updates = {
        k: v.strip() if isinstance(v, str) else v
        for k, v in updates.items()
        if v is not None and (not isinstance(v, str) or v.strip() != "")
    }
    if not clean_updates:
        return False
    result = await db.appointments.update_one(
        {"_id": ObjectId(appt_id), "tenant_id": tenant_id, "status": {"$ne": "cancelled"}},
        {"$set": clean_updates},
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
        # A33: see find_active_appointments — escape before building the pattern.
        identity_clauses.append(
            {"customer_email": {"$regex": f"^{re.escape(email.strip())}$", "$options": "i"}}
        )
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

async def next_order_id(tenant_id: str) -> int:
    """
    Allocate a per-tenant order number atomically.

    T07: order ids used to come from the shared SQLite AUTOINCREMENT sequence,
    which is global across every tenant on the instance.
    """
    db = get_db()
    from pymongo import ReturnDocument

    doc = await db.counters.find_one_and_update(
        {"_id": f"order_id:{tenant_id}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int((doc or {}).get("seq", 1))


def _cancel_sqlite_order(order_id: int) -> bool:
    """Mark a SQLite POS order as cancelled (demo tenant only — see T08)."""
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
    """
    Mark an order as cancelled. MongoDB is authoritative and tenant-scoped.

    T08: this used to call _cancel_sqlite_order(order_id) unconditionally. The
    SQLite orders table has no tenant column and a shared id sequence, so tenant
    A cancelling their order #1042 could cancel tenant B's row — and returning
    True on the strength of that SQLite update alone reported success for an
    order the caller did not own.
    """
    from backend.config import settings

    db = get_db()
    result = await db.orders.update_one(
        {"tenant_id": tenant_id, "order_id": order_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    cancelled = result.modified_count > 0

    if tenant_id == settings.DEFAULT_TENANT_ID:
        # Keep the demo POS in step, but never let it decide the outcome.
        _cancel_sqlite_order(order_id)

    return cancelled

