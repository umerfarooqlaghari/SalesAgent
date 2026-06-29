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

@router.post("/forgot-password")
async def forgot_password(payload: Dict[str, Any] = Body(...)):
    """Request a password reset link. Sends via AWS SES email."""
    import secrets
    import hashlib
    from datetime import datetime, timezone, timedelta
    from backend.database import get_db
    from backend.auth.email import send_reset_password_email
    
    email = (payload.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
        
    db = get_db()
    user = await db.users.find_one({"email": email, "status": "active"})
    if not user:
        # Prevent email enumeration: always return success message
        return {"message": "If this email is registered, a password reset link has been sent."}
        
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "reset_token_hash": token_hash,
            "reset_token_exp": expiry.isoformat()
        }}
    )
    
    await send_reset_password_email(email, token)
    return {"message": "If this email is registered, a password reset link has been sent."}

@router.post("/reset-password")
async def reset_password(payload: Dict[str, Any] = Body(...)):
    """Verify reset token and update password."""
    import hashlib
    from datetime import datetime, timezone
    from backend.database import get_db
    from backend.auth.security import hash_password
    
    token = (payload.get("token") or "").strip()
    password = payload.get("password") or ""
    
    if not token or not password:
        raise HTTPException(status_code=400, detail="token and password are required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    user = await db.users.find_one({"reset_token_hash": token_hash, "status": "active"})
    
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
    exp_str = user.get("reset_token_exp")
    if not exp_str:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
    exp_time = datetime.fromisoformat(exp_str)
    if exp_time < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
    # Valid token, update password and clear token
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {
            "$set": {"password_hash": hash_password(password)},
            "$unset": {"reset_token_hash": "", "reset_token_exp": ""}
        }
    )
    
    return {"message": "Password reset successfully. You can now log in with your new password."}
