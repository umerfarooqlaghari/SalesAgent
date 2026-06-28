from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from backend.integrations.providers import build_schemas_response
from backend.integrations.service import IntegrationService
from backend.tenant.context import TenantContext
from backend.auth.dependencies import get_tenant_or_api_key

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/integration-schemas")
async def get_integration_schemas(_tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Provider field definitions for dynamic Admin UI forms."""
    return build_schemas_response()


@router.get("/tenant")
async def get_tenant_admin(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Current tenant config with secrets masked."""
    try:
        return await IntegrationService.get_admin_view(tenant.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/integrations")
async def save_integrations(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Save integration configs (encrypts secrets server-side)."""
    try:
        return await IntegrationService.save_integrations(tenant.tenant_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/settings")
async def save_tenant_settings(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Update tenant agent settings (system prompt, company description, etc.)."""
    try:
        return await IntegrationService.save_settings(tenant.tenant_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/settings/reset-agent-prompt")
async def reset_agent_prompt(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Replace Alpha demo prompt with a tenant-specific prompt using org name."""
    try:
        return await IntegrationService.reset_agent_prompt(tenant.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/integrations/test")
async def test_integration(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """
    Test a connection before saving.
    Body: { category, provider, config, source_id? }
    """
    category = payload.get("category")
    provider_id = payload.get("provider")
    config = payload.get("config") or {}
    source_id = payload.get("source_id")

    if not category or not provider_id:
        raise HTTPException(status_code=400, detail="category and provider are required")

    existing_config = None
    if source_id:
        view = await IntegrationService.get_admin_view(tenant.tenant_id)
        integrations = view.get("integrations") or {}
        if category == "inventory":
            for src in integrations.get("inventory", {}).get("sources", []):
                if src.get("id") == source_id:
                    existing_config = src.get("config")
                    break
        else:
            existing_config = integrations.get(category, {}).get("config")

    result = await IntegrationService.test_connection(
        tenant.tenant_id,
        category,
        provider_id,
        config,
        existing_config,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/integrations/discover-schema")
async def discover_integration_schema(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """
    Connect to SQL database and list tables/columns with suggested mappings.
    Body: { category, provider, config, source_id? }
    """
    category = payload.get("category")
    provider_id = payload.get("provider")
    config = payload.get("config") or {}
    source_id = payload.get("source_id")

    if not category or not provider_id:
        raise HTTPException(status_code=400, detail="category and provider are required")

    existing_config = None
    if source_id:
        view = await IntegrationService.get_admin_view(tenant.tenant_id)
        integrations = view.get("integrations") or {}
        if category == "inventory":
            for src in integrations.get("inventory", {}).get("sources", []):
                if src.get("id") == source_id:
                    existing_config = src.get("config")
                    break
        else:
            existing_config = integrations.get(category, {}).get("config")

    try:
        return await IntegrationService.discover_schema(
            tenant.tenant_id,
            category,
            provider_id,
            config,
            existing_config,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/knowledge")
async def list_knowledge_admin(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    from backend.agent.rag import list_knowledge

    chunks = await list_knowledge(tenant.tenant_id)
    return {"chunks": chunks}


@router.post("/knowledge")
async def add_knowledge_admin(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    from backend.agent.rag import upsert_knowledge_chunk

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    chunk_id = await upsert_knowledge_chunk(
        tenant.tenant_id,
        text,
        title=payload.get("title") or "General",
        source=payload.get("source") or "admin",
    )
    return {"status": "ok", "chunk_id": chunk_id}
