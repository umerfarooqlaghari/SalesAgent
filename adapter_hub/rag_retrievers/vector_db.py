import hashlib
import logging
import math
from typing import Any, Dict, List

from adapter_hub.adapters.canonical import Product, Customer, Order, Record

logger = logging.getLogger(__name__)


def generate_mock_embedding(text: str) -> List[float]:
    """Deterministic character-hashing text embedding (unit-normalized 64-d)."""
    if not text:
        return [0.0] * 64

    vec = [0.0] * 64
    for word in text.lower().split():
        h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
        for i in range(4):
            idx = (h >> (i * 8)) % 64
            vec[idx] += 1.0

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def compute_cosine_similarity(v1: List[float], v2: List[float]) -> float:
    return float(sum(a * b for a, b in zip(v1, v2)))


class MultiTenantVectorDB:
    """In-memory multi-tenant vector store (namespace = tenant_id)."""

    def __init__(self):
        self._namespaces: Dict[str, List[Dict[str, Any]]] = {}

    async def upsert_entities(self, tenant_id: str, agent_id: str, entities: List[Any]) -> int:
        if tenant_id not in self._namespaces:
            self._namespaces[tenant_id] = []

        namespace = self._namespaces[tenant_id]
        count = 0

        for entity in entities:
            entity_type = entity.__class__.__name__
            payload = entity.model_dump()

            if isinstance(entity, Product):
                text_repr = (
                    f"Product SKU/ID: {entity.id}. Name: {entity.name}. Price: {entity.price}. "
                    f"Stock: {entity.stock_quantity}. Description: {entity.description or ''}"
                )
            elif isinstance(entity, Customer):
                text_repr = (
                    f"Customer ID: {entity.id}. Name: {entity.name}. Email: {entity.email}. "
                    f"Company: {entity.company or ''}. Status: {entity.status}"
                )
            elif isinstance(entity, Order):
                items_str = ", ".join([f"{it.quantity}x {it.product_name}" for it in entity.items])
                text_repr = (
                    f"Order ID: {entity.id}. Customer Email: {entity.customer_email}. "
                    f"Status: {entity.status}. Total: {entity.total_price}. Items: {items_str}"
                )
            elif isinstance(entity, Record):
                text_repr = (
                    f"{entity.entity_label} (table={entity.table_name}). {entity.summary}. "
                    f"Fields: {entity.fields}"
                )
            else:
                text_repr = (
                    f"Entity ID: {entity.id if hasattr(entity, 'id') else 'None'}. "
                    f"Metadata: {str(getattr(entity, 'raw_metadata', {}))}"
                )

            vector = generate_mock_embedding(text_repr)
            doc_id = f"{entity_type}_{entity.id}" if hasattr(entity, "id") else f"{entity_type}_{hash(text_repr)}"
            existing_idx = next((i for i, d in enumerate(namespace) if d["id"] == doc_id), None)

            doc_payload = {
                "id": doc_id,
                "agent_id": agent_id,
                "entity_type": entity_type,
                "text": text_repr,
                "vector": vector,
                "payload": payload,
            }

            if existing_idx is not None:
                namespace[existing_idx] = doc_payload
            else:
                namespace.append(doc_payload)
            count += 1

        return count

    async def query_vector_space(
        self,
        tenant_id: str,
        agent_id: str,
        query_text: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        namespace = self._namespaces.get(tenant_id)
        if not namespace:
            logger.info("Namespace for tenant %s is empty or uninitialized.", tenant_id)
            return []

        query_vec = generate_mock_embedding(query_text)
        results = []
        for doc in namespace:
            sim = compute_cosine_similarity(query_vec, doc["vector"])
            results.append(
                {
                    "id": doc["id"],
                    "entity_type": doc["entity_type"],
                    "text": doc["text"],
                    "score": sim,
                    "payload": doc["payload"],
                }
            )

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def clear_database(self):
        self._namespaces.clear()


vector_db_client = MultiTenantVectorDB()
