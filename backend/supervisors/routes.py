"""Supervisor CRUD routes — per-tenant management of human escalation targets."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException

from backend.auth.dependencies import require_secret_tenant
from backend.database import get_db
from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/supervisors", tags=["supervisors"])


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MongoDB ObjectId fields to strings for JSON serialization."""
    doc["_id"] = str(doc["_id"])
    return doc


# ---------------------------------------------------------------------------
# GET — list all supervisors for this tenant
# ---------------------------------------------------------------------------

@router.get("")
async def list_supervisors(
    tenant: TenantContext = Depends(require_secret_tenant),
) -> Dict[str, Any]:
    """Return all supervisors registered for this tenant, ordered by creation time."""
    db = get_db()
    cursor = db.supervisors.find(
        {"tenant_id": tenant.tenant_id}
    ).sort("created_at", 1)
    supervisors: List[Dict[str, Any]] = []
    async for doc in cursor:
        supervisors.append(_serialize(doc))
    return {"supervisors": supervisors}


# ---------------------------------------------------------------------------
# POST — create a new supervisor
# ---------------------------------------------------------------------------

@router.post("")
async def create_supervisor(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
) -> Dict[str, Any]:
    """
    Register a new supervisor for human handoff routing.
    Required fields: name, email
    Optional fields: department, phone, active (default: true)
    """
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    department = (payload.get("department") or "").strip()
    phone = (payload.get("phone") or "").strip()
    active = bool(payload.get("active", True))

    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="a valid email is required")

    db = get_db()

    # Enforce unique email per tenant
    existing = await db.supervisors.find_one({"tenant_id": tenant.tenant_id, "email": email})
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A supervisor with email '{email}' already exists for this tenant.",
        )

    doc = {
        "tenant_id": tenant.tenant_id,
        "name": name,
        "email": email,
        "department": department,
        "phone": phone,
        "active": active,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.supervisors.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    logger.info("Supervisor created: %s (%s) for tenant %s", name, email, tenant.tenant_id)
    return {"supervisor": doc}


# ---------------------------------------------------------------------------
# PUT — update an existing supervisor
# ---------------------------------------------------------------------------

@router.put("/{supervisor_id}")
async def update_supervisor(
    supervisor_id: str,
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
) -> Dict[str, Any]:
    """Update one or more fields on an existing supervisor."""
    try:
        oid = ObjectId(supervisor_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid supervisor ID")

    db = get_db()
    existing = await db.supervisors.find_one(
        {"_id": oid, "tenant_id": tenant.tenant_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")

    updates: Dict[str, Any] = {}
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        updates["name"] = name
    if "email" in payload:
        email = (payload["email"] or "").strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="a valid email is required")
        # Check uniqueness (allow same email on same doc)
        conflict = await db.supervisors.find_one(
            {"tenant_id": tenant.tenant_id, "email": email, "_id": {"$ne": oid}}
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"Another supervisor already uses email '{email}'.",
            )
        updates["email"] = email
    if "department" in payload:
        updates["department"] = (payload["department"] or "").strip()
    if "phone" in payload:
        updates["phone"] = (payload["phone"] or "").strip()
    if "active" in payload:
        updates["active"] = bool(payload["active"])

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    await db.supervisors.update_one({"_id": oid}, {"$set": updates})
    updated = await db.supervisors.find_one({"_id": oid})
    return {"supervisor": _serialize(updated)}


# ---------------------------------------------------------------------------
# DELETE — remove a supervisor
# ---------------------------------------------------------------------------

@router.delete("/{supervisor_id}")
async def delete_supervisor(
    supervisor_id: str,
    tenant: TenantContext = Depends(require_secret_tenant),
) -> Dict[str, Any]:
    """Permanently remove a supervisor from this tenant."""
    try:
        oid = ObjectId(supervisor_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid supervisor ID")

    db = get_db()
    result = await db.supervisors.delete_one(
        {"_id": oid, "tenant_id": tenant.tenant_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    logger.info("Supervisor %s deleted for tenant %s", supervisor_id, tenant.tenant_id)
    return {"ok": True, "deleted_id": supervisor_id}
