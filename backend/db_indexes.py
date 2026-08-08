"""
MongoDB index definitions — created idempotently on startup.

Every operational query MUST lead with tenant_id; compound indexes follow that prefix.
"""
from __future__ import annotations

import logging

from backend.database import get_db

logger = logging.getLogger(__name__)


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
    await db.tenants.create_index("tenant_id", unique=True, name="tenants_tenant_id")
    await db.tenants.create_index(
        "api_key_hash", unique=True, sparse=True, name="tenants_api_key_hash"
    )
    await db.tenants.create_index(
        "publishable_key",
        unique=True,
        sparse=True,
        name="tenants_publishable_key",
    )
    await db.tenants.create_index("status", name="tenants_status")
    await db.tenants.create_index("owner_email", name="tenants_owner_email")
    await db.tenants.create_index(
        "stripe_customer_id", unique=True, sparse=True, name="tenants_stripe_customer_id"
    )
    await db.tenants.create_index(
        "stripe_subscription_id", unique=True, sparse=True, name="tenants_stripe_subscription_id"
    )
    await db.api_keys.create_index("key", name="api_keys_key")
    await db.api_keys.create_index("tenant_id", name="api_keys_tenant_id")

    await db.users.create_index("email", unique=True, sparse=True, name="users_email")
    await db.users.create_index("user_id", unique=True, sparse=True, name="users_user_id")
    await db.users.create_index([("tenant_id", 1)], name="users_tenant_id")
    await db.users.create_index("role", name="users_role")
    # S22: reset/verify tokens are looked up by their hash directly — without
    # a dedicated index that's a full collection scan on every password reset
    # confirmation and every email verification click.
    await db.users.create_index(
        "reset_token_hash", unique=True, sparse=True, name="users_reset_token_hash"
    )
    await db.users.create_index(
        "verify_token_hash", unique=True, sparse=True, name="users_verify_token_hash"
    )


async def _ensure_operational_indexes() -> None:
    db = get_db()

    # Leads / CRM
    await db.leads.create_index(
        [("tenant_id", 1), ("thread_id", 1)],
        unique=True,
        name="leads_tenant_thread",
    )
    await db.leads.create_index(
        [("tenant_id", 1), ("status", 1)],
        name="leads_tenant_status",
    )
    await db.leads.create_index(
        [("tenant_id", 1), ("company", 1)],
        name="leads_tenant_company",
    )

    # Conversations / chat history
    await db.conversations.create_index(
        [("tenant_id", 1), ("thread_id", 1)],
        unique=True,
        name="conversations_tenant_thread",
    )

    # Orders — list, lookup by order_id, lookup by customer email
    await db.orders.create_index(
        [("tenant_id", 1), ("created_at", -1)],
        name="orders_tenant_created",
    )
    await db.orders.create_index(
        [("tenant_id", 1), ("order_id", 1)],
        name="orders_tenant_order_id",
    )
    await db.orders.create_index(
        [("tenant_id", 1), ("customer_email", 1)],
        name="orders_tenant_email",
    )
    await db.orders.create_index(
        [("tenant_id", 1), ("status", 1), ("created_at", -1)],
        name="orders_tenant_status_created",
    )

    # Appointments — slot check, list, identity lookup
    await db.appointments.create_index(
        [("tenant_id", 1), ("date", 1), ("time", 1)],
        name="appointments_tenant_date_time",
    )
    await db.appointments.create_index(
        [("tenant_id", 1), ("date", 1), ("time", 1), ("status", 1)],
        name="appointments_tenant_slot_status",
    )
    await db.appointments.create_index(
        [("tenant_id", 1), ("email", 1)],
        name="appointments_tenant_email",
    )
    await db.appointments.create_index(
        [("tenant_id", 1), ("thread_id", 1)],
        name="appointments_tenant_thread",
    )

    # Demo meetings (schedule_demo tool)
    await db.meetings.create_index(
        [("tenant_id", 1), ("thread_id", 1)],
        name="meetings_tenant_thread",
    )

    # LangGraph checkpoint collections — scoped lookups by thread
    # T09: thread_id reaches the checkpointer namespaced as "<tenant_id>::<thread_id>",
    # so this index is tenant-partitioned by prefix.
    await db.checkpoints.create_index("thread_id", name="checkpoints_thread_id")
    await db.writes.create_index("thread_id", name="writes_thread_id")


async def _ensure_knowledge_indexes() -> None:
    """RAG knowledge base — tenant-scoped text search + listing."""
    db = get_db()

    await db.tenant_knowledge.create_index(
        [("tenant_id", 1), ("created_at", -1)],
        name="knowledge_tenant_created",
    )
    await db.tenant_knowledge.create_index(
        "chunk_id",
        unique=True,
        sparse=True,
        name="knowledge_chunk_id",
    )
    await db.tenant_knowledge.create_index(
        [("tenant_id", 1), ("source", 1)],
        name="knowledge_tenant_source",
    )

    # Full-text search within tenant (Atlas / standalone MongoDB 4.2+)
    try:
        await db.tenant_knowledge.create_index(
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
    await db.voice_call_links.create_index(
        "call_id",
        unique=True,
        name="voice_call_id",
    )
    await db.voice_call_links.create_index(
        [("tenant_id", 1), ("call_id", 1)],
        name="voice_tenant_call",
    )
    await db.voice_call_links.create_index(
        [("tenant_id", 1), ("console_thread_id", 1)],
        name="voice_tenant_console_thread",
    )
    # T14: kept globally unique so resolve_voice_thread can look a session up by
    # console_thread_id alone (the Vapi webhook has no authenticated tenant).
    # Cross-tenant hijack is blocked by the ownership checks in
    # register_voice_session / link_voice_call. See T15 for the remaining
    # pre-claim denial-of-service, which needs server-generated ids.
    await db.voice_call_sessions.create_index(
        "console_thread_id",
        unique=True,
        name="voice_session_console_thread",
    )
    await db.voice_call_sessions.create_index(
        [("tenant_id", 1), ("console_thread_id", 1)],
        name="voice_session_tenant_console_thread",
    )
    # S02: end-of-call-report webhooks can be redelivered by Vapi on retry —
    # this makes a duplicate delivery a no-op instead of double-billing.
    await db.voice_billing_events.create_index(
        "call_id",
        unique=True,
        name="voice_billing_events_call_id",
    )
