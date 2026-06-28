import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.database import get_db
from backend.tenant.context import DEFAULT_TENANT_ID, TenantContext
from backend.tenant.secrets import hash_api_key

logger = logging.getLogger(__name__)

DEFAULT_TEST_API_KEY = "test_key_abc123"


async def ensure_tenant_indexes() -> None:
    """Legacy shim — use ensure_all_indexes from db_indexes."""
    from backend.db_indexes import ensure_all_indexes

    await ensure_all_indexes()


async def seed_default_tenant() -> None:
    """Create the default tenant and map legacy test API key."""
    db = get_db()
    api_key_hash = hash_api_key(DEFAULT_TEST_API_KEY)
    existing = await db.tenants.find_one({"tenant_id": DEFAULT_TENANT_ID})
    if existing:
        return

    from backend.agent.prompts import SYSTEM_PROMPT

    await db.tenants.insert_one(
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "org_name": "Alpha Default",
            "api_key_hash": api_key_hash,
            "status": "active",
            "integration_configs": {
                "inventory": {
                    "enabled": True,
                    "sources": [
                        {
                            "id": "default_stub",
                            "enabled": True,
                            "provider": "stub",
                            "priority": 0,
                            "label": "Demo catalog",
                            "config": {"read_only": True},
                        }
                    ],
                },
                "crm": {"enabled": True, "provider": "internal", "config": {}},
                "calendar": {"enabled": True, "provider": "internal", "config": {}},
            },
            "settings": {
                "system_prompt": SYSTEM_PROMPT,
                "webhook_url": None,
                "rate_limit_per_minute": 120,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.info("Seeded default tenant '%s' with test API key.", DEFAULT_TENANT_ID)

    # Keep legacy api_keys doc for backward compatibility during transition
    if not await db.api_keys.find_one({"key": DEFAULT_TEST_API_KEY}):
        await db.api_keys.insert_one(
            {"key": DEFAULT_TEST_API_KEY, "owner": "Alpha Default", "active": True, "tenant_id": DEFAULT_TENANT_ID}
        )


async def migrate_legacy_documents_to_default_tenant() -> None:
    """Attach tenant_id to documents created before multi-tenant rollout."""
    from backend.integrations.service import normalize_integrations

    db = get_db()
    collections = ["leads", "conversations", "orders", "appointments", "voice_call_links"]
    for name in collections:
        result = await db[name].update_many(
            {"tenant_id": {"$exists": False}},
            {"$set": {"tenant_id": DEFAULT_TENANT_ID}},
        )
        if result.modified_count:
            logger.info("Migrated %s documents in '%s' to tenant %s", result.modified_count, name, DEFAULT_TENANT_ID)

    async for tenant_doc in db.tenants.find({}):
        normalized = normalize_integrations(tenant_doc.get("integration_configs"))
        if normalized != tenant_doc.get("integration_configs"):
            await db.tenants.update_one(
                {"tenant_id": tenant_doc["tenant_id"]},
                {"$set": {"integration_configs": normalized}},
            )
            logger.info("Normalized integration_configs for tenant %s", tenant_doc["tenant_id"])


async def resolve_tenant_by_api_key(api_key: str) -> Optional[TenantContext]:
    if not api_key:
        return None

    db = get_db()
    key_hash = hash_api_key(api_key)

    doc = await db.tenants.find_one({"api_key_hash": key_hash, "status": "active"})
    if doc:
        return TenantContext.from_document(doc)

    # Legacy fallback: api_keys collection → default tenant
    legacy = await db.api_keys.find_one({"key": api_key, "active": True})
    if legacy:
        tenant_id = legacy.get("tenant_id", DEFAULT_TENANT_ID)
        tenant_doc = await db.tenants.find_one({"tenant_id": tenant_id, "status": "active"})
        if tenant_doc:
            return TenantContext.from_document(tenant_doc)

    return None


async def get_tenant_by_id(tenant_id: str) -> Optional[TenantContext]:
    db = get_db()
    doc = await db.tenants.find_one({"tenant_id": tenant_id, "status": "active"})
    if not doc:
        return None
    return TenantContext.from_document(doc)


async def migrate_stale_tenant_prompts() -> None:
    """Replace copied Alpha demo prompts on real client tenants (one-time / startup)."""
    from backend.agent.prompts import build_tenant_system_prompt, is_alpha_default_prompt
    from backend.integrations.normalize import normalize_integrations
    from backend.integrations.service import _disable_demo_stub_sources

    db = get_db()
    migrated = 0
    async for doc in db.tenants.find({"tenant_id": {"$ne": DEFAULT_TENANT_ID}, "status": "active"}):
        settings = doc.get("settings") or {}
        prompt = settings.get("system_prompt") or ""
        if not is_alpha_default_prompt(prompt):
            continue

        org = doc.get("org_name") or doc["tenant_id"]
        desc = settings.get("company_description") or ""
        new_prompt = build_tenant_system_prompt(org, desc)

        integrations = normalize_integrations(doc.get("integration_configs"))
        _disable_demo_stub_sources(integrations)

        await db.tenants.update_one(
            {"tenant_id": doc["tenant_id"]},
            {
                "$set": {
                    "settings.system_prompt": new_prompt,
                    "integration_configs": integrations,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        migrated += 1
        logger.info("Migrated Alpha demo prompt → %s for tenant %s", org, doc["tenant_id"])

    if migrated:
        logger.info("Migrated %s tenant prompt(s) off Alpha demo template", migrated)


async def get_tenant_system_prompt(tenant_id: str, fallback: str) -> str:
    ctx = await get_tenant_by_id(tenant_id)
    if not ctx:
        return fallback

    prompt = ctx.settings.system_prompt or fallback

    if tenant_id != DEFAULT_TENANT_ID and ctx.org_name:
        from backend.agent.prompts import build_tenant_system_prompt, is_alpha_default_prompt

        if is_alpha_default_prompt(prompt):
            desc = ctx.settings.company_description or ""
            prompt = build_tenant_system_prompt(ctx.org_name, desc)
            db = get_db()
            await db.tenants.update_one(
                {"tenant_id": tenant_id},
                {"$set": {"settings.system_prompt": prompt}},
            )
            logger.info("Auto-fixed stale Alpha prompt for tenant %s", tenant_id)

    return prompt


async def seed_default_knowledge() -> None:
    """Seed RAG chunks for the default tenant (product catalog facts)."""
    from backend.agent.rag import upsert_knowledge_chunk

    db = get_db()
    existing = await db.tenant_knowledge.count_documents({"tenant_id": DEFAULT_TENANT_ID})
    if existing > 0:
        return

    chunks = [
        (
            "Product Catalog",
            "SaaS Starter Package: $49/month, basic outreach, 1 user license, high availability.",
        ),
        (
            "Product Catalog",
            "SaaS Professional Package: $199/month, 5 user licenses, advanced sales tools.",
        ),
        (
            "Product Catalog",
            "SaaS Enterprise License: $999/month, unlimited users, custom integrations, dedicated success rep.",
        ),
        (
            "Policies",
            "Orders can be cancelled with order number plus email or phone verification.",
        ),
        (
            "Policies",
            "Appointments can be booked, cancelled, or rescheduled. Collect name, email, phone, date, and time.",
        ),
    ]
    for title, text in chunks:
        await upsert_knowledge_chunk(DEFAULT_TENANT_ID, text, title=title, source="seed")
    logger.info("Seeded %s RAG knowledge chunks for tenant %s", len(chunks), DEFAULT_TENANT_ID)
