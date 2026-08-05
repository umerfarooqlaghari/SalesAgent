import logging
from typing import Any, Dict
from fastapi import FastAPI, HTTPException, Depends, Query
from pydantic import BaseModel

from adapter_hub.auth import TenantIsolationMiddleware, current_tenant_id, current_agent_id
from adapter_hub.sync_workers.schema_discovery import discovery_manager
from adapter_hub.sync_workers.vector_sync import sync_worker
from adapter_hub.rag_retrievers import rag_retriever

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Adapter-Hub Microservice",
    description="Unified integration & retrieval layer for multi-tenant B2B AI agents.",
    version="1.0.0"
)

# Enforce strict multi-tenant isolation and scoping middleware
app.add_middleware(TenantIsolationMiddleware)

class ConnectionRegisterRequest(BaseModel):
    provider: str
    config: Dict[str, Any]

class WhitelistSaveRequest(BaseModel):
    whitelist: Dict[str, Any]

class RAGQueryRequest(BaseModel):
    query: str
    top_k: int = 5
    min_score: float = 0.05

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "adapter-hub"}

@app.post("/connections/register")
async def register_connection(req: ConnectionRegisterRequest):
    tenant_id = current_tenant_id.get()
    agent_id = current_agent_id.get()
    
    try:
        await discovery_manager.register_connection(
            tenant_id=tenant_id,
            agent_id=agent_id,
            provider=req.provider,
            config=req.config
        )
        return {"ok": True, "message": "Connection registered successfully."}
    except Exception as e:
        logger.error(f"Error registering connection: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/schema/discover")
async def discover_schema(bypass_cache: bool = Query(default=False)):
    tenant_id = current_tenant_id.get()
    agent_id = current_agent_id.get()
    
    try:
        schema = await discovery_manager.discover_and_cache_schema(
            tenant_id=tenant_id,
            agent_id=agent_id,
            bypass_cache=bypass_cache
        )
        return {"ok": True, "schema": schema}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Error discovering schema: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/schema/whitelist")
async def save_whitelist(req: WhitelistSaveRequest):
    tenant_id = current_tenant_id.get()
    agent_id = current_agent_id.get()
    
    try:
        await discovery_manager.save_whitelist(
            tenant_id=tenant_id,
            agent_id=agent_id,
            whitelist=req.whitelist
        )
        return {"ok": True, "message": "Whitelist mappings saved successfully."}
    except Exception as e:
        logger.error(f"Error saving whitelist: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync")
async def trigger_sync():
    tenant_id = current_tenant_id.get()
    agent_id = current_agent_id.get()
    
    res = await sync_worker.run_sync_for_tenant(tenant_id, agent_id)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error", "Sync execution failed"))
    return res

@app.post("/retrieve")
async def retrieve_rag(req: RAGQueryRequest):
    tenant_id = current_tenant_id.get()
    agent_id = current_agent_id.get()
    
    try:
        results = await rag_retriever.retrieve(
            tenant_id=tenant_id,
            agent_id=agent_id,
            query=req.query,
            top_k=req.top_k,
            min_score=req.min_score
        )
        return {"ok": True, "results": results}
    except Exception as e:
        logger.error(f"Error retrieving context: {e}")
        raise HTTPException(status_code=500, detail=str(e))
