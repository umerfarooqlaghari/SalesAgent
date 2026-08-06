"""Lightweight tenant-scoped RAG — keyword retrieval from MongoDB knowledge chunks."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from backend.agent.text_utils import score_text_overlap
from backend.database import get_db

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _score_query(query: str, chunk_text: str) -> float:
    return score_text_overlap(query, chunk_text)


async def ensure_knowledge_indexes() -> None:
    """Legacy shim — indexes managed in db_indexes."""
    from backend.db_indexes import _ensure_knowledge_indexes

    await _ensure_knowledge_indexes()


async def retrieve_context(tenant_id: str, query: str, limit: int = 4) -> str:
    """Return top matching knowledge snippets (Mongo + adapter-hub) for the system prompt."""
    if not query or not query.strip():
        return ""

    db = get_db()
    snippets: List[Dict[str, Any]] = []

    try:
        cursor = db.tenant_knowledge.find(
            {
                "tenant_id": tenant_id,
                "$text": {"$search": query},
            },
            {"score": {"$meta": "textScore"}, "text": 1, "title": 1},
        ).sort([("score", {"$meta": "textScore"})]).limit(limit)
        async for doc in cursor:
            snippets.append(doc)
    except Exception as e:
        logger.debug("Text index search unavailable, using keyword fallback: %s", e)
        snippets = []

    if not snippets:
        cursor = db.tenant_knowledge.find({"tenant_id": tenant_id}).limit(80)
        scored: List[tuple[float, Dict[str, Any]]] = []
        async for doc in cursor:
            scored.append((_score_query(query, doc.get("text", "")), doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        snippets = [d for s, d in scored if s > 0][:limit]

    lines = []
    for doc in snippets:
        title = doc.get("title") or "Knowledge"
        text = (doc.get("text") or "").strip()
        if text:
            lines.append(f"- [{title}] {text[:600]}")

    # Merge adapter-hub vector/keyword RAG (synced Production / PO / Sets rows)
    try:
        from backend.integrations.adapter_hub_client import retrieve as hub_retrieve

        hub_hits = await hub_retrieve(tenant_id, query, top_k=limit)
        for hit in hub_hits:
            text = (hit.get("text") or "").strip()
            if not text:
                continue
            etype = hit.get("entity_type") or "Record"
            lines.append(f"- [Hub:{etype}] {text[:600]}")
    except Exception as e:
        logger.debug("Adapter-hub RAG merge skipped: %s", e)

    return "\n".join(lines)


async def upsert_knowledge_chunk(
    tenant_id: str,
    text: str,
    title: str = "General",
    source: str = "admin",
) -> str:
    from datetime import datetime, timezone
    import uuid
    from pymongo import ReturnDocument

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    # A02: this used to be a plain insert_one with a fresh uuid4 despite the
    # name "upsert", so re-seeding (baseline FAQ, admin re-save) duplicated
    # rows without bound. Key on (tenant_id, source, title); $setOnInsert only
    # takes effect when the filter finds nothing, so an existing row keeps its
    # original chunk_id/created_at.
    doc = await db.tenant_knowledge.find_one_and_update(
        {"tenant_id": tenant_id, "source": source, "title": title},
        {
            "$set": {
                "tenant_id": tenant_id,
                "title": title,
                "text": text.strip(),
                "source": source,
                "updated_at": now,
            },
            "$setOnInsert": {"chunk_id": str(uuid.uuid4()), "created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["chunk_id"]


async def list_knowledge(tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    db = get_db()
    out = []
    cursor = db.tenant_knowledge.find({"tenant_id": tenant_id}).sort([("created_at", -1)]).limit(limit)
    async for doc in cursor:
        doc["id"] = doc.get("chunk_id") or str(doc.get("_id", ""))
        out.append(doc)
    return out
