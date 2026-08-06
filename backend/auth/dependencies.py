from typing import Optional

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth.security import decode_access_token
from backend.auth.service import UserSession, get_user_session
from backend.tenant.context import TenantContext
from backend.tenant.key_scope import get_key_scope, set_key_scope
from backend.tenant.middleware import get_tenant_context
from backend.tenant.registry import get_tenant_by_id

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> UserSession:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    session = await get_user_session(payload["sub"])
    if not session:
        raise HTTPException(status_code=401, detail="User not found")

    # S18: a password reset bumps token_version so every JWT issued before it
    # stops working immediately, instead of staying valid for its full 72h.
    if payload.get("tver", 0) != session.token_version:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")

    return session


async def require_super_admin(user: UserSession = Depends(get_current_user)) -> UserSession:
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


async def get_tenant_for_user(user: UserSession = Depends(get_current_user)) -> TenantContext:
    if not user.tenant_id:
        raise HTTPException(status_code=403, detail="No tenant linked to this account")
    tenant = await get_tenant_by_id(user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


async def get_tenant_or_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> TenantContext:
    """
    Dashboard JWT session OR API key (secret sk_ OR publishable pk_).
    Publishable keys are only for embed/query — use require_secret_tenant for admin.
    """
    if credentials and credentials.credentials:
        token = credentials.credentials
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            session = await get_user_session(payload["sub"])
            # S18: reject a JWT issued before a password reset bumped token_version.
            if session and payload.get("tver", 0) == session.token_version and session.tenant_id:
                tenant = await get_tenant_by_id(session.tenant_id)
                if tenant:
                    set_key_scope("jwt")
                    return tenant
            if session and session.role == "super_admin":
                raise HTTPException(status_code=403, detail="Use super-admin routes for this account")
        # Fall through — treat as API key (sk_ or pk_)
        return await get_tenant_context(credentials=credentials)

    raise HTTPException(status_code=401, detail="Authentication required")


async def require_secret_tenant(
    tenant: TenantContext = Depends(get_tenant_or_api_key),
) -> TenantContext:
    """Block publishable (pk_) keys from admin / privileged routes."""
    if get_key_scope() == "publishable":
        raise HTTPException(
            status_code=403,
            detail=(
                "Publishable keys (pk_live_…) can only use embed/query endpoints. "
                "Use your secret API key (sk_live_…) or dashboard login for this action."
            ),
        )
    return tenant
