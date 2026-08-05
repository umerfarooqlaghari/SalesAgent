import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from adapter_hub.adapters import get_connector
from adapter_hub.auth.secrets import encrypt_secret, decrypt_secret

logger = logging.getLogger(__name__)

# A simple file-based storage for the microservice metadata (Connections & Whitelists)
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata_store.json")

class DiscoveryManager:
    """
    Manages client connection configurations, Whitelists storage, 
    and handles introspective schema caching with TTL.
    """
    
    def __init__(self):
        self.schema_cache: Dict[str, Dict[str, Any]] = {}  # key: tenant_id:agent_id, val: {"timestamp": float, "schema": list}
        self.cache_ttl_seconds = 300  # 5 minutes TTL
        self._load_store()

    def _load_store(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r") as f:
                    self.store = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load metadata store: {e}")
                self.store = {"connections": {}, "whitelists": {}}
        else:
            self.store = {"connections": {}, "whitelists": {}}

    def _save_store(self):
        try:
            with open(DB_FILE, "w") as f:
                json.dump(self.store, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write to metadata store: {e}")

    def _get_key(self, tenant_id: str, agent_id: str) -> str:
        return f"{tenant_id}:{agent_id}"

    async def register_connection(self, tenant_id: str, agent_id: str, provider: str, config: Dict[str, Any]):
        """
        Register a client connection config. Sensitive fields (passwords, access tokens, etc.)
        are encrypted at rest before storing.
        """
        key = self._get_key(tenant_id, agent_id)
        
        # Encrypt sensitive keys
        encrypted_config = config.copy()
        secret_keys = ["password", "access_token", "auth_token"]
        for sk in secret_keys:
            if sk in encrypted_config and encrypted_config[sk]:
                encrypted_config[sk] = encrypt_secret(str(encrypted_config[sk]))

        self.store["connections"][key] = {
            "provider": provider,
            "config": encrypted_config
        }
        self._save_store()

    async def get_connection(self, tenant_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a client connection config, decrypting passwords and access tokens on-the-fly.
        """
        key = self._get_key(tenant_id, agent_id)
        conn_info = self.store["connections"].get(key)
        if not conn_info:
            return None
            
        provider = conn_info["provider"]
        config = conn_info["config"].copy()
        
        secret_keys = ["password", "access_token", "auth_token"]
        for sk in secret_keys:
            if sk in config and config[sk]:
                try:
                    config[sk] = decrypt_secret(config[sk])
                except Exception as e:
                    logger.error(f"Failed to decrypt credential {sk} for tenant {tenant_id}: {e}")
                    config[sk] = ""
                    
        return {
            "provider": provider,
            "config": config
        }

    async def discover_and_cache_schema(self, tenant_id: str, agent_id: str, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        """
        Performs introspective scan of client system. Caches structural metadata with a TTL.
        """
        key = self._get_key(tenant_id, agent_id)
        now = time.time()
        
        if not bypass_cache and key in self.schema_cache:
            cache = self.schema_cache[key]
            if now - cache["timestamp"] < self.cache_ttl_seconds:
                logger.info(f"Returning cached schema for {key}")
                return cache["schema"]
                
        conn_info = await self.get_connection(tenant_id, agent_id)
        if not conn_info:
            raise ValueError(f"No connection registered for tenant {tenant_id}, agent {agent_id}")
            
        connector = get_connector(conn_info["provider"], conn_info["config"], tenant_id, agent_id)
        schema = await connector.discover_schema()
        
        # Cache the result
        self.schema_cache[key] = {
            "timestamp": now,
            "schema": schema
        }
        
        return schema

    async def save_whitelist(self, tenant_id: str, agent_id: str, whitelist: Dict[str, Any]):
        """
        Saves which tables/fields are whitelisted per agent/tenant.
        """
        key = self._get_key(tenant_id, agent_id)
        self.store["whitelists"][key] = whitelist
        self._save_store()

    async def get_whitelist(self, tenant_id: str, agent_id: str) -> Dict[str, Any]:
        key = self._get_key(tenant_id, agent_id)
        return self.store["whitelists"].get(key) or {}

discovery_manager = DiscoveryManager()
