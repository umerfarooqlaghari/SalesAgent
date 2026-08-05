import logging
from typing import Dict, Any, List

from adapter_hub.adapters import get_connector
from adapter_hub.sync_workers.schema_discovery import discovery_manager
from adapter_hub.rag_retrievers.vector_db import vector_db_client

logger = logging.getLogger(__name__)

class SyncWorker:
    """
    Worker class responsible for querying third-party systems using adapters, 
    canonicalizing records, and synchronizing them into the tenant-isolated Vector DB.
    """
    
    async def run_sync_for_tenant(self, tenant_id: str, agent_id: str) -> Dict[str, Any]:
        """
        Runs a sync job for a single tenant and agent.
        """
        logger.info(f"Starting vector sync job for tenant {tenant_id}, agent {agent_id}")
        
        # 1. Load connection details
        conn_info = await discovery_manager.get_connection(tenant_id, agent_id)
        if not conn_info:
            return {"ok": False, "error": "No connection registered"}
            
        # 2. Load whitelist mappings
        whitelist = await discovery_manager.get_whitelist(tenant_id, agent_id)
        if not whitelist:
            return {"ok": False, "error": "No whitelist configured"}

        try:
            # 3. Instantiate connector and pull raw data converted into canonical entities
            connector = get_connector(conn_info["provider"], conn_info["config"], tenant_id, agent_id)
            canonical_entities = await connector.sync_data(whitelist)
            
            if not canonical_entities:
                return {"ok": True, "synchronized_count": 0, "message": "No new data to sync"}
                
            # 4. Upsert canonical entities into the namespace-isolated Vector DB
            success_count = await vector_db_client.upsert_entities(tenant_id, agent_id, canonical_entities)
            
            logger.info(f"Successfully synced {success_count} B2B records to Vector DB for tenant {tenant_id}")
            return {
                "ok": True,
                "synchronized_count": success_count,
                "message": f"Successfully synced {success_count} canonical records."
            }
        except Exception as e:
            logger.error(f"Sync job failed for tenant {tenant_id}: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

    async def run_cdc_polling_cycle(self) -> Dict[str, Any]:
        """
        Simulates a polling sync cycle across all registered tenant connections.
        Can be scheduled or run continuously.
        """
        results = {}
        # Get all registered keys from the discovery manager
        keys = list(discovery_manager.store["connections"].keys())
        for key in keys:
            try:
                tenant_id, agent_id = key.split(":")
                res = await self.run_sync_for_tenant(tenant_id, agent_id)
                results[key] = res
            except Exception as e:
                logger.error(f"Failed parsing key or running sync for key {key}: {e}")
                results[key] = {"ok": False, "error": str(e)}
        return results

sync_worker = SyncWorker()
