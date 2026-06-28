from typing import Optional

from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.tenant.context import TenantContext
from backend.tenant.registry import resolve_tenant_by_api_key

_bearer = HTTPBearer(auto_error=False)


async def get_tenant_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
) -> TenantContext:
    """
    Resolve tenant from Authorization: Bearer <api_key> or x-api-key header.
    """
    api_key = None
    if credentials and credentials.credentials:
        api_key = credentials.credentials
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    tenant = await resolve_tenant_by_api_key(api_key)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return tenant
