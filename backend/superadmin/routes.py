from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.auth.dependencies import require_super_admin
from backend.auth.service import UserSession, list_all_tenants, platform_stats
from backend.database import get_db

router = APIRouter(prefix="/api/superadmin", tags=["superadmin"])


@router.get("/stats")
async def get_stats(_admin: UserSession = Depends(require_super_admin)):
    return await platform_stats()


@router.get("/tenants")
async def get_tenants(_admin: UserSession = Depends(require_super_admin)):
    return {"tenants": await list_all_tenants()}


@router.get("/tenants/{tenant_id}/leads")
async def get_tenant_leads(tenant_id: str, _admin: UserSession = Depends(require_super_admin)):
    db = get_db()
    leads = []
    async for doc in db.leads.find({"tenant_id": tenant_id}).limit(200):
        doc["_id"] = str(doc["_id"])
        leads.append(doc)
    return {"tenant_id": tenant_id, "leads": leads}


@router.get("/users")
async def get_users(_admin: UserSession = Depends(require_super_admin)):
    db = get_db()
    users = []
    async for doc in db.users.find({}, {"password_hash": 0}).limit(500):
        users.append(
            {
                "user_id": doc.get("user_id"),
                "email": doc.get("email"),
                "name": doc.get("name"),
                "role": doc.get("role"),
                "tenant_id": doc.get("tenant_id"),
                "status": doc.get("status"),
                "created_at": doc.get("created_at"),
            }
        )
    return {"users": users}
