from abc import ABC, abstractmethod
from typing import Any, Dict, List

class Connector(ABC):
    """
    Abstract Base Class that all Adapter-Hub connectors must extend.
    Provides methods for testing connections, discovering schemas, 
    and extracting data into canonical formats.
    """
    
    def __init__(self, config: Dict[str, Any], tenant_id: str, agent_id: str):
        self.config = config
        self.tenant_id = tenant_id
        self.agent_id = agent_id

    @abstractmethod
    async def test_connection(self) -> bool:
        """
        Verify credentials and connectivity to the third-party client system.
        Returns True if successful, raises an exception otherwise.
        """
        pass

    @abstractmethod
    async def discover_schema(self) -> List[Dict[str, Any]]:
        """
        Perform an introspective scan of tables, columns, fields, and types.
        Returns a list of tables and columns in a standard representation.
        """
        pass

    @abstractmethod
    async def sync_data(self, whitelist: Dict[str, Any]) -> List[Any]:
        """
        Extract data from whitelisted resources, normalize them, 
        and return them as instances of the Canonical Data Model.
        """
        pass
