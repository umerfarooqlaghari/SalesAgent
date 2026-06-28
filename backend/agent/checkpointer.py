import logging
import asyncio
from pymongo import MongoClient
from langgraph.checkpoint.mongodb import MongoDBSaver
from backend.config import settings

logger = logging.getLogger(__name__)

_checkpointer: MongoDBSaver = None

def _init_saver() -> MongoDBSaver:
    """Instantiates MongoClient and saver in a background thread to prevent DNS lookup blocking."""
    sync_client = MongoClient(settings.MONGODB_URI)
    return MongoDBSaver(sync_client, db_name=settings.DATABASE_NAME)

async def get_checkpointer() -> MongoDBSaver:
    global _checkpointer
    if _checkpointer is None:
        # Prevent blocking event loop during SRV record DNS resolution
        _checkpointer = await asyncio.to_thread(_init_saver)
        logger.info("MongoDB Synchronous Checkpointer initialized successfully.")
    return _checkpointer
