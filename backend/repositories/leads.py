from typing import Any, Dict, List, Optional

from backend.database import get_db
from backend.repositories.base import TenantScopedRepository


class LeadRepository(TenantScopedRepository):
    async def upsert(self, thread_id: str, lead_data: Dict[str, Any]) -> Dict[str, Any]:
        db = get_db()
        payload = {**lead_data, "thread_id": thread_id, "tenant_id": self.tenant_id}
        await db.leads.update_one(
            self._tenant_filter({"thread_id": thread_id}),
            {"$set": payload},
            upsert=True,
        )
        return payload

    async def get_by_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        db = get_db()
        doc = await db.leads.find_one(self._tenant_filter({"thread_id": thread_id}))
        return self._stringify_id(doc) if doc else None

    async def list_all(self) -> List[Dict[str, Any]]:
        db = get_db()
        cursor = db.leads.find(self._tenant_filter())
        return [self._stringify_id(doc) async for doc in cursor]

    async def delete_by_thread(self, thread_id: str) -> int:
        db = get_db()
        result = await db.leads.delete_many(self._tenant_filter({"thread_id": thread_id}))
        return result.deleted_count
