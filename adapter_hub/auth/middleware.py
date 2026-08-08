import contextvars
import hmac
import logging
import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from adapter_hub.auth.tenant_keys import verify_tenant_key
from adapter_hub.config import settings

# Thread-safe context variables to keep track of tenant/agent scopes
current_tenant_id = contextvars.ContextVar("current_tenant_id", default="")
current_agent_id = contextvars.ContextVar("current_agent_id", default="")

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

class TenantIsolationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Allow open endpoints (health check, openapi docs) without headers
        if request.url.path in ["/", "/health", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)
            
        api_key = request.headers.get("X-API-Key")
        tenant_id = request.headers.get("X-Tenant-ID")
        agent_id = request.headers.get("X-Agent-ID")
        
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "API key is missing in X-API-Key header", "code": "UNAUTHORIZED"}
            )
            
        if not tenant_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Tenant ID is missing in X-Tenant-ID header", "code": "BAD_REQUEST"}
            )

        if not _ID_RE.match(tenant_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Invalid X-Tenant-ID format", "code": "BAD_REQUEST"}
            )

        # S10: the key must be the one derived for THIS tenant. Previously any
        # holder of the shared master key could act as any tenant simply by
        # editing X-Tenant-ID — auth proved "some caller", the header decided
        # who. Now the header is a claim that the key has to corroborate.
        if not verify_tenant_key(settings.MASTER_API_KEY, tenant_id, api_key):
            # A master key is still accepted in development so local tooling and
            # the existing tests keep working. In production it is refused: it
            # is precisely the credential that let one caller impersonate every
            # tenant, and it shipped in source.
            master_ok = (
                settings.allow_master_key_fallback
                and hmac.compare_digest(
                    api_key.encode("utf-8", "replace"),
                    settings.MASTER_API_KEY.encode("utf-8"),
                )
            )
            if not master_ok:
                logger.warning(
                    "Rejected Adapter-Hub request for tenant %s — key does not "
                    "match the derived key for that tenant.", tenant_id,
                )
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "Invalid API Key", "code": "UNAUTHORIZED"}
                )
            logger.warning(
                "Adapter-Hub master key used directly for tenant %s. This is a "
                "development-only fallback; set per-tenant keys before production.",
                tenant_id,
            )

        if not agent_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Agent ID is missing in X-Agent-ID header", "code": "BAD_REQUEST"}
            )

        if not _ID_RE.match(agent_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Invalid X-Agent-ID format", "code": "BAD_REQUEST"}
            )
            
        # Set thread-safe context variables
        token_t = current_tenant_id.set(tenant_id)
        token_a = current_agent_id.set(agent_id)
        
        try:
            response = await call_next(request)
            return response
        finally:
            # Clean up context variables after request completes
            current_tenant_id.reset(token_t)
            current_agent_id.reset(token_a)
