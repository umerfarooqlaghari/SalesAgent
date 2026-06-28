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
    """
    Seeds a default API key 'test_key_abc123' in MongoDB Atlas for local sandbox testing.
    """
    db = get_db()
    # Check if key already exists
    key_doc = await db.api_keys.find_one({"key": "test_key_abc123"})
    if not key_doc:
        await db.api_keys.insert_one({
            "key": "test_key_abc123",
            "owner": "GrowthCorp",
            "active": True
        })
        logger.info("Seeded default test API Key: 'test_key_abc123'")

async def validate_api_key_in_db(api_key: str) -> bool:
    """
    Checks MongoDB collection for a valid active API key.
    """
    db = get_db()
    doc = await db.api_keys.find_one({"key": api_key, "active": True})
    return doc is not None

async def save_lead(thread_id: str, lead_data: Dict[str, Any]) -> Dict[str, Any]:
    db = get_db()
    lead_data["thread_id"] = thread_id
    await db.leads.update_one(
        {"thread_id": thread_id},
        {"$set": lead_data},
        upsert=True
    )
    return lead_data

async def get_lead(thread_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = await db.leads.find_one({"thread_id": thread_id})
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

async def list_leads() -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.leads.find({})
    leads = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        leads.append(doc)
    return leads

async def save_conversation_message(thread_id: str, role: str, message: str, thought: Optional[str] = None):
    db = get_db()
    entry = {
        "role": role,
        "content": message,
        "thought": thought
    }
    await db.conversations.update_one(
        {"thread_id": thread_id},
        {"$push": {"messages": entry}},
        upsert=True
    )

async def get_conversation(thread_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    doc = await db.conversations.find_one({"thread_id": thread_id})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc

async def list_conversations() -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.conversations.find({})
    convs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        convs.append(doc)
    return convs

async def rename_conversation(thread_id: str, title: str):
    db = get_db()
    await db.conversations.update_one(
        {"thread_id": thread_id},
        {"$set": {"title": title}},
        upsert=True
    )

async def delete_conversation(thread_id: str):
    db = get_db()
    # Delete from conversation logs
    await db.conversations.delete_many({"thread_id": thread_id})
    # Delete from lead profiles
    await db.leads.delete_many({"thread_id": thread_id})
    # Delete from LangGraph checkpointer memory collections
    await db.checkpoints.delete_many({"thread_id": thread_id})
    await db.writes.delete_many({"thread_id": thread_id})

# ---------------------------------------------------------------------------
# Appointment booking helpers
# ---------------------------------------------------------------------------

async def check_slot_available(date_str: str, time_str: str) -> bool:
    """Returns True if the requested date+time slot has no existing booking."""
    db = get_db()
    existing = await db.appointments.find_one({
        "date": date_str,
        "time": time_str,
        "status": {"$ne": "cancelled"}
    })
    return existing is None

async def create_appointment(
    thread_id: str,
    name: str,
    email: str,
    phone: str,
    date_str: str,
    time_str: str,
    notes: str = ""
) -> Dict[str, Any]:
    """Saves a confirmed appointment to MongoDB and returns the booking document."""
    from datetime import datetime, timezone
    db = get_db()
    doc = {
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

async def list_appointments() -> List[Dict[str, Any]]:
    """Returns all appointments ordered by date/time."""
    db = get_db()
    cursor = db.appointments.find({}).sort([("date", 1), ("time", 1)])
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

async def list_orders() -> List[Dict[str, Any]]:
    """Returns all customer orders, newest first."""
    db = get_db()
    cursor = db.orders.find({}).sort([("created_at", -1)])
    orders = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        orders.append(doc)
    return orders

def _normalize_phone(phone: str) -> str:
    """Strip non-digits for loose phone matching."""
    return "".join(c for c in phone if c.isdigit())

async def find_active_appointments(
    email: Optional[str] = None,
    phone: Optional[str] = None,
    thread_id: Optional[str] = None,
    date_str: Optional[str] = None,
    time_str: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find non-cancelled appointments matching caller identity."""
    db = get_db()
    filters: List[Dict[str, Any]] = [{"status": {"$ne": "cancelled"}}]

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

async def cancel_appointment_record(appt_id: str) -> bool:
    """Mark an appointment as cancelled in MongoDB."""
    from bson import ObjectId

    db = get_db()
    result = await db.appointments.update_one(
        {"_id": ObjectId(appt_id), "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    return result.modified_count > 0

async def reschedule_appointment_record(
    appt_id: str,
    new_date: str,
    new_time: str,
) -> bool:
    """Move an appointment to a new date/time."""
    from bson import ObjectId

    db = get_db()
    result = await db.appointments.update_one(
        {"_id": ObjectId(appt_id), "status": {"$ne": "cancelled"}},
        {"$set": {"date": new_date.strip(), "time": new_time.strip()}},
    )
    return result.modified_count > 0

async def find_active_orders(
    order_id: Optional[int] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find non-cancelled orders matching caller identity."""
    db = get_db()
    filters: List[Dict[str, Any]] = [{"status": {"$ne": "cancelled"}}]

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

async def cancel_order_record(order_id: int) -> bool:
    """Mark an order as cancelled in MongoDB and SQLite."""
    db = get_db()
    result = await db.orders.update_one(
        {"order_id": order_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    sqlite_updated = _cancel_sqlite_order(order_id)
    return result.modified_count > 0 or sqlite_updated

