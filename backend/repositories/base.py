from typing import Any, Dict


class TenantScopedRepository:
    """Base repository — all queries MUST include tenant_id."""

    def __init__(self, tenant_id: str):
        if not tenant_id:
            raise ValueError("tenant_id is required for all repository operations")
        self.tenant_id = tenant_id

    def _tenant_filter(self, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        filt: Dict[str, Any] = {"tenant_id": self.tenant_id}
        if extra:
            filt.update(extra)
        return filt

    @staticmethod
    def _stringify_id(doc: Dict[str, Any]) -> Dict[str, Any]:
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc
