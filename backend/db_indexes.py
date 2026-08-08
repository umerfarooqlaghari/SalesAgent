"""
MongoDB index definitions — created idempotently on startup.

Every operational query MUST lead with tenant_id; compound indexes follow that prefix.
"""
from __future__ import annotations

import logging

from pymongo.errors import OperationFailure

from backend.database import get_db

logger = logging.getLogger(__name__)

# Mongo will not change the options of an existing index (e.g. adding
# sparse=True) while the name stays the same — it raises OperationFailure with
# code 85 (IndexOptionsConflict) or 86 (IndexKeySpecsConflict). Recreating the
# index is the documented remedy, and these are all non-unique-data indexes, so
# a brief drop is safe.
_INDEX_CONFLICT_CODES = {85, 86}


async def _ensure_index(collection, keys, **options):
    """create_index that reconciles an existing index with different options."""
    name = options.get("name")
    try:
        return await collection.create_index(keys, **options)
    except OperationFailure as exc:
        if exc.code not in _INDEX_CONFLICT_CODES or not name:
            logger.warning("Index %s on %s failed: %s", name, collection.name, exc)
            return None
        logger.info(
            "Index %s on %s exists with different options — recreating",
            name, collection.name,
        )
        try:
            await collection.drop_index(name)
        except OperationFailure:
            logger.warning("Could not drop index %s on %s", name, collection.name)
            return None
        try:
            return await collection.create_index(keys, **options)
        except OperationFailure as exc2:
            logger.warning("Recreate of index %s on %s failed: %s", name, collection.name, exc2)
            return None




async def ensure_all_indexes() -> None:
    """Create all application indexes (safe to call on every startup)."""
    # A17: a single index-creation conflict (e.g. a pre-existing index with the
    # same name but different options on the real cluster) used to abort every
    # index after it — leaving later collections with none of their indexes.
    for step in (
        _ensure_tenant_indexes,
        _ensure_operational_indexes,
        _ensure_knowledge_indexes,
        _ensure_voice_indexes,
    ):
        try:
            await step()
        except Exception:
            logger.exception("Index step %s failed — continuing with remaining steps", step.__name__)
    logger.info("All MongoDB indexes ensured.")


async def _ensure_tenant_indexes() -> None:
    db = get_db()
    await _ensure_index(db.tenants, "tenant_id", unique=True, name="tenants_tenant_id")
    await _ensure_index(db.tenants, 
        "api_key_hash", unique=True, sparse=True, name="tenants_api_key_hash"
    )
    await _ensure_index(db.tenants, 
        "publishable_key",
        unique=True,
        sparse=True,
        name="tenants_publishable_key",
    )
    await _ensure_index(db.tenants, "status", name="tenants_status")
    await _ensure_index(db.tenants, "owner_email", name="tenants_owner_email")
    await _ensure_index(db.tenants, 
        "stripe_customer_id", unique=True, sparse=True, name="tenants_stripe_customer_id"
    )
    await _ensure_index(db.tenants, 
        "stripe_subscription_id", unique=True, sparse=True, name="tenants_stripe_subscription_id"
    )
    await _ensure_index(db.api_keys, "key", name="api_keys_key")
    await _ensure_index(db.api_keys, "tenant_id", name="api_keys_tenant_id")

    await _ensure_index(db.users, "email", unique=True, sparse=True, name="users_email")
    await _ensure_index(db.users, "user_id", unique=True, sparse=True, name="users_user_id")
    await _ensure_index(db.users, [("tenant_id", 1)], name="users_tenant_id")
    await _ensure_index(db.users, "role", name="users_role")
    # S22: reset/verify tokens are looked up by their hash directly — without
    # a dedicated index that's a full collection scan on every password reset
    # confirmation and every email verification click.
    await _ensure_index(db.users, 
        "reset_token_hash", unique=True, sparse=True, name="users_reset_token_hash"
    )
    await _ensure_index(db.users, 
        "verify_token_hash", unique=True, sparse=True, name="users_verify_token_hash"
    )


async def _ensure_operational_indexes() -> None:
    db = get_db()

    # Leads / CRM
    await _ensure_index(db.leads, 
        [("tenant_id", 1), ("thread_id", 1)],
        unique=True,
        name="leads_tenant_thread",
    )
    await _ensure_index(db.leads, 
        [("tenant_id", 1), ("status", 1)],
        name="leads_tenant_status",
    )
    await _ensure_index(db.leads, 
        [("tenant_id", 1), ("company", 1)],
        name="leads_tenant_company",
    )

    # Conversations / chat history
    await _ensure_index(db.conversations, 
        [("tenant_id", 1), ("thread_id", 1)],
        unique=True,
        name="conversations_tenant_thread",
    )

    # Orders — list, lookup by order_id, lookup by customer email
    await _ensure_index(db.orders, 
        [("tenant_id", 1), ("created_at", -1)],
        name="orders_tenant_created",
    )
    await _ensure_index(db.orders, 
        [("tenant_id", 1), ("order_id", 1)],
        name="orders_tenant_order_id",
    )
    await _ensure_index(db.orders, 
        [("tenant_id", 1), ("customer_email", 1)],
        name="orders_tenant_email",
    )
    await _ensure_index(db.orders, 
        [("tenant_id", 1), ("status", 1), ("created_at", -1)],
        name="orders_tenant_status_created",
    )

    # Appointments — slot check, list, identity lookup
    await _ensure_index(db.appointments, 
        [("tenant_id", 1), ("date", 1), ("time", 1)],
        name="appointments_tenant_date_time",
    )
    await _ensure_index(db.appointments, 
        [("tenant_id", 1), ("date", 1), ("time", 1), ("status", 1)],
        name="appointments_tenant_slot_status",
    )
    await _ensure_index(db.appointments, 
        [("tenant_id", 1), ("email", 1)],
        name="appointments_tenant_email",
    )
    await _ensure_index(db.appointments, 
        [("tenant_id", 1), ("thread_id", 1)],
        name="appointments_tenant_thread",
    )

    # Demo meetings (schedule_demo tool)
    await _ensure_index(db.meetings, 
        [("tenant_id", 1), ("thread_id", 1)],
        name="meetings_tenant_thread",
    )

    # LangGraph checkpoint collections — scoped lookups by thread
    # T09: thread_id reaches the checkpointer namespaced as "<tenant_id>::<thread_id>",
    # so this index is tenant-partitioned by prefix.
    await _ensure_index(db.checkpoints, "thread_id", name="checkpoints_thread_id")
    await _ensure_index(db.writes, "thread_id", name="writes_thread_id")


async def _ensure_knowledge_indexes() -> None:
    """RAG knowledge base — tenant-scoped text search + listing."""
    db = get_db()

    await _ensure_index(db.tenant_knowledge, 
        [("tenant_id", 1), ("created_at", -1)],
        name="knowledge_tenant_created",
    )
    await _ensure_index(db.tenant_knowledge, 
        "chunk_id",
        unique=True,
        sparse=True,
        name="knowledge_chunk_id",
    )
    await _ensure_index(db.tenant_knowledge, 
        [("tenant_id", 1), ("source", 1)],
        name="knowledge_tenant_source",
    )

    # Full-text search within tenant (Atlas / standalone MongoDB 4.2+)
    try:
        await _ensure_index(db.tenant_knowledge, 
            [("tenant_id", 1), ("text", "text"), ("title", "text")],
            name="knowledge_tenant_text",
            default_language="english",
        )
        logger.info("RAG text index ready (tenant_id + text/title).")
    except Exception as e:
        logger.warning(
            "Could not create RAG text index (keyword fallback still works): %s", e
        )


async def _ensure_voice_indexes() -> None:
    db = get_db()
    await _ensure_index(db.voice_call_links, 
        "call_id",
        unique=True,
        name="voice_call_id",
    )
    await _ensure_index(db.voice_call_links, 
        [("tenant_id", 1), ("call_id", 1)],
        name="voice_tenant_call",
    )
    await _ensure_index(db.voice_call_links, 
        [("tenant_id", 1), ("console_thread_id", 1)],
        name="voice_tenant_console_thread",
    )
    # T14: kept globally unique so resolve_voice_thread can look a session up by
    # console_thread_id alone (the Vapi webhook has no authenticated tenant).
    # Cross-tenant hijack is blocked by the ownership checks in
    # register_voice_session / link_voice_call. See T15 for the remaining
    # pre-claim denial-of-service, which needs server-generated ids.
    await _ensure_index(db.voice_call_sessions, 
        "console_thread_id",
        unique=True,
        name="voice_session_console_thread",
    )
    await _ensure_index(db.voice_call_sessions, 
        [("tenant_id", 1), ("console_thread_id", 1)],
        name="voice_session_tenant_console_thread",
    )
    # S15: the billing webhook's fallback lookup is by call_id. Without this the
    # query was a full collection scan on every end-of-call report — and since
    # nothing wrote the field, a scan that could never match. Sparse, because
    # sessions registered before Vapi assigns an id have no call_id yet.
    await _ensure_index(db.voice_call_sessions,
        "call_id",
        sparse=True,
        name="voice_session_call_id",
    )
    # S02: end-of-call-report webhooks can be redelivered by Vapi on retry —
    # this makes a duplicate delivery a no-op instead of double-billing.
    await _ensure_index(db.voice_billing_events, 
        "call_id",
        unique=True,
        name="voice_billing_events_call_id",
    )
