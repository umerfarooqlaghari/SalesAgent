import contextvars
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from adapter_hub.config import settings

# Thread-safe context variables to keep track of tenant/agent scopes
current_tenant_id = contextvars.ContextVar("current_tenant_id", default="")
current_agent_id = contextvars.ContextVar("current_agent_id", default="")

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
            
        # For simplicity, validate against master key.
        # This can be extended to check database tenant table hashed api keys.
        if api_key != settings.MASTER_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Invalid API Key", "code": "UNAUTHORIZED"}
            )
            
        if not tenant_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Tenant ID is missing in X-Tenant-ID header", "code": "BAD_REQUEST"}
            )
            
        if not agent_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Agent ID is missing in X-Agent-ID header", "code": "BAD_REQUEST"}
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
