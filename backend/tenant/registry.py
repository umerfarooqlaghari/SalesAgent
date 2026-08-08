import asyncio
import logging
import secrets
import time
from collections import OrderedDict
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
    """
    Create the default tenant.

    S05: this used to write hash_api_key(DEFAULT_TEST_API_KEY) into the tenant's
    own `api_key_hash`, unconditionally. Gating the legacy `api_keys` collection
    (below) did nothing about that, because `resolve_tenant_by_api_key` matches
    `api_key_hash` on the PRIMARY path — so `X-API-Key: test_key_abc123`
    authenticated as the default tenant with full `secret` scope in production.

    In production the seeded tenant now gets a random secret key. In development
    the well-known key is still mapped, because local tooling and the docs use
    it — and there the legacy collection is available anyway.
    """
    from backend.config import settings

    db = get_db()
    existing = await db.tenants.find_one({"tenant_id": DEFAULT_TENANT_ID})
    if existing:
        await _rotate_well_known_default_key(existing)
        return

    if settings.is_production:
        generated = f"sk_live_{secrets.token_urlsafe(32)}"
        api_key_hash = hash_api_key(generated)
        logger.warning(
            "Seeded default tenant with a RANDOM secret key (production). The raw "
            "key is intentionally not logged — rotate it from the dashboard to "
            "obtain a usable one."
        )
    else:
        api_key_hash = hash_api_key(DEFAULT_TEST_API_KEY)

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

    # S05: the legacy api_keys doc maps a well-known, publicly-documented test
    # key ("test_key_abc123") straight to the default tenant. Keeping that
    # doc alive in production would let anyone authenticate with it.
    from backend.config import settings

    if not settings.is_production and not await db.api_keys.find_one({"key": DEFAULT_TEST_API_KEY}):
        await db.api_keys.insert_one(
            {"key": DEFAULT_TEST_API_KEY, "owner": "Alpha Default", "active": True, "tenant_id": DEFAULT_TENANT_ID}
        )


async def _rotate_well_known_default_key(tenant_doc: dict) -> None:
    """
    S05, existing deployments: the seed already ran, so the well-known hash is
    ALREADY in the database and gating the seeder fixes nothing. Detect it and
    replace it with a random key on the next production boot.

    Deliberately narrow: it only ever replaces a hash that equals
    hash_api_key(DEFAULT_TEST_API_KEY), so a tenant with a real key is untouched.
    """
    from backend.config import settings

    if not settings.is_production:
        return
    if tenant_doc.get("api_key_hash") != hash_api_key(DEFAULT_TEST_API_KEY):
        return

    db = get_db()
    generated = f"sk_live_{secrets.token_urlsafe(32)}"
    await db.tenants.update_one(
        {"tenant_id": tenant_doc.get("tenant_id"), "api_key_hash": hash_api_key(DEFAULT_TEST_API_KEY)},
        {"$set": {"api_key_hash": hash_api_key(generated)}},
    )
    invalidate_tenant(tenant_doc.get("tenant_id"))
    logger.critical(
        "SECURITY: tenant %s was authenticating with the publicly-known test API "
        "key. Its api_key_hash has been rotated to a random value. Anyone who had "
        "that key has lost access; regenerate a key from the dashboard. Treat any "
        "data this tenant holds as having been readable.",
        tenant_doc.get("tenant_id"),
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
    """
    Resolve tenant from secret (sk_) or publishable (pk_) key.
    Sets current_key_scope to 'secret' or 'publishable'.
    """
    if not api_key:
        return None

    from backend.auth.security import is_publishable_key
    from backend.tenant.key_scope import set_key_scope

    db = get_db()

    # Publishable keys are stored in plaintext (designed for browser use)
    if is_publishable_key(api_key):
        doc = await db.tenants.find_one({"publishable_key": api_key, "status": "active"})
        if doc:
            set_key_scope("publishable")
            return TenantContext.from_document(doc)
        return None

    key_hash = hash_api_key(api_key)
    doc = await db.tenants.find_one({"api_key_hash": key_hash, "status": "active"})
    if doc:
        set_key_scope("secret")
        return TenantContext.from_document(doc)

    # Legacy fallback: api_keys collection → default tenant.
    # S05: this path accepts the well-known DEFAULT_TEST_API_KEY, so it must
    # never be reachable in production.
    from backend.config import settings

    if settings.is_production:
        return None

    legacy = await db.api_keys.find_one({"key": api_key, "active": True})
    if legacy:
        tenant_id = legacy.get("tenant_id", DEFAULT_TENANT_ID)
        tenant_doc = await db.tenants.find_one({"tenant_id": tenant_id, "status": "active"})
        if tenant_doc:
            set_key_scope("secret")
            return TenantContext.from_document(tenant_doc)

    return None


async def ensure_publishable_keys() -> int:
    """Backfill publishable keys for tenants that only have a secret key."""
    from backend.auth.security import generate_publishable_key

    db = get_db()
    created = 0
    async for doc in db.tenants.find({"status": "active"}):
        if doc.get("publishable_key"):
            continue
        pk = generate_publishable_key()
        await db.tenants.update_one(
            {"tenant_id": doc["tenant_id"]},
            {
                "$set": {
                    "publishable_key": pk,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        created += 1
        logger.info("Issued publishable key for tenant %s", doc["tenant_id"])
    return created


# P01: this used to be an uncached find_one for the FULL tenant document — the
# entire system prompt plus every integration config — and it is called 6+ times
# per voice turn (voice greeting, sdr_node, get_tenant_system_prompt, once per
# tool via _load_tenant_context, twice in the voice fast path). At Atlas latency
# that alone accounted for several hundred ms of every spoken turn.
_TENANT_CACHE: "OrderedDict[str, tuple[float, Optional[TenantContext]]]" = OrderedDict()
_TENANT_CACHE_TTL = 60.0
_TENANT_CACHE_MAX = 2000
_TENANT_LOCKS: Dict[str, asyncio.Lock] = {}


def invalidate_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Clear ONLY the tenant-document cache. Prefer invalidate_tenant()."""
    if tenant_id is None:
        _TENANT_CACHE.clear()
    else:
        _TENANT_CACHE.pop(tenant_id, None)


def invalidate_tenant(tenant_id: Optional[str] = None) -> None:
    """
    Drop every per-tenant cache after a write to the tenant document.

    Call this from ANY code path that mutates `tenants` — settings, integrations,
    billing tier, prompt repair. Missing a call means an admin saves a change and
    the agent keeps using the old config for up to a minute.
    """
    invalidate_tenant_cache(tenant_id)

    try:
        from backend.integrations.tenant_inventory import invalidate_inventory_mappings

        invalidate_inventory_mappings(tenant_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("inventory mapping invalidation failed", exc_info=True)

    try:
        from backend.agent.graph import invalidate_prompt_memo

        invalidate_prompt_memo()
    except Exception:  # pragma: no cover - defensive
        logger.debug("prompt memo invalidation failed", exc_info=True)

    if tenant_id:
        try:
            from backend.integrations.catalog_cache import invalidate_catalog

            invalidate_catalog(tenant_id)
        except Exception:  # pragma: no cover - defensive
            logger.debug("catalog invalidation failed", exc_info=True)


def _cached_tenant(tenant_id: str) -> Optional[TenantContext]:
    hit = _TENANT_CACHE.get(tenant_id)
    if not hit:
        return None
    expires_at, ctx = hit
    if time.monotonic() > expires_at:
        _TENANT_CACHE.pop(tenant_id, None)
        return None
    _TENANT_CACHE.move_to_end(tenant_id)
    return ctx


async def get_tenant_by_id(tenant_id: str) -> Optional[TenantContext]:
    if not tenant_id:
        return None

    cached = _cached_tenant(tenant_id)
    if cached is not None:
        return cached

    lock = _TENANT_LOCKS.setdefault(tenant_id, asyncio.Lock())
    async with lock:
        # Double-check: a concurrent turn may have populated it while we queued.
        cached = _cached_tenant(tenant_id)
        if cached is not None:
            return cached

        db = get_db()
        doc = await db.tenants.find_one(
            {"tenant_id": tenant_id, "status": "active"}, max_time_ms=2000
        )
        ctx = TenantContext.from_document(doc) if doc else None
        if ctx is not None:
            _TENANT_CACHE[tenant_id] = (time.monotonic() + _TENANT_CACHE_TTL, ctx)
            _TENANT_CACHE.move_to_end(tenant_id)
            while len(_TENANT_CACHE) > _TENANT_CACHE_MAX:
                _TENANT_CACHE.popitem(last=False)
        _TENANT_LOCKS.pop(tenant_id, None)
        return ctx


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
            # P12: the repair used to be written back inline, putting a Mongo
            # update on every spoken turn until it succeeded. The startup
            # migration (migrate_stale_tenant_prompts) owns persistence; here we
            # just use the corrected prompt for this turn.
            logger.info("Using repaired prompt for tenant %s (persisted at startup)", tenant_id)

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
