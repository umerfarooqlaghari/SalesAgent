from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from backend.auth.dependencies import get_current_user, require_super_admin
from backend.auth.service import (
    UserSession,
    login_user,
    platform_stats,
    regenerate_api_key,
    register_client,
)
from backend.tenant.registry import resolve_tenant_by_api_key

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def register(payload: Dict[str, Any] = Body(...)):
    """Client self-registration — creates tenant + user, returns API key once."""
    try:
        return await register_client(
            org_name=payload.get("org_name") or "",
            email=payload.get("email") or "",
            password=payload.get("password") or "",
            name=payload.get("name") or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/login")
async def login(payload: Dict[str, Any] = Body(...)):
    """Email + password login → JWT session."""
    email = payload.get("email")
    password = payload.get("password")
    api_key = (payload.get("api_key") or "").strip()

    if email and password:
        try:
            return await login_user(email, password)
        except ValueError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    if api_key:
        tenant = await resolve_tenant_by_api_key(api_key)
        if not tenant:
            raise HTTPException(status_code=401, detail="Invalid or inactive API key")
        return {
            "valid": True,
            "tenant_id": tenant.tenant_id,
            "org_name": tenant.org_name,
            "status": tenant.status,
        }

    raise HTTPException(status_code=400, detail="email/password or api_key required")


@router.get("/me")
async def auth_me(user: UserSession = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "org_name": user.org_name,
    }


@router.post("/regenerate-api-key")
async def regenerate_key(user: UserSession = Depends(get_current_user)):
    """Issue a new API key (shown once). Requires JWT login."""
    try:
        api_key = await regenerate_api_key(user.user_id)
        return {
            "api_key": api_key,
            "message": "Save this API key now — it will not be shown again.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
