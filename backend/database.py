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
