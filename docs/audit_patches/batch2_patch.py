import pathlib, sys
ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
def edit(path, subs):
    p = ROOT / path; src = p.read_text()
    for i,(old,new) in enumerate(subs):
        n = src.count(old)
        assert n == 1, f"{path} anchor #{i} matched {n}x:\n{old[:200]}"
        src = src.replace(old, new)
    p.write_text(src); print(f"  patched {path} ({len(subs)} edits)")
def replace_block(path, start_marker, end_marker, new_text):
    p = ROOT / path; src = p.read_text()
    i, j = src.index(start_marker), src.index(end_marker)
    assert i < j, path
    p.write_text(src[:i] + new_text + src[j:]); print(f"  block-replaced in {path}")

# ===== batch2a.py =====

print("BATCH 2a — cross-tenant demo-data leakage (T01..T08, A33)")

# ================= T01 =================
edit('backend/integrations/normalize.py', [
('''DEFAULT_INTEGRATIONS: Dict[str, Any] = {
    "inventory": {
        "enabled": True,
        "sources": [
            {
                "id": "default_stub",
                "enabled": True,
                "provider": "stub",
                "priority": 0,
                "config": {"read_only": True},
            }
        ],
    },
    "crm": {"enabled": True, "provider": "internal", "config": {}},
    "calendar": {"enabled": True, "provider": "internal", "config": {}},
}''',
'''# T01: a tenant with no configured inventory must get NOTHING, not the shared
# demo catalog. The old default enabled a "stub" source for every unconfigured
# tenant, which routes to StubPOSAdapter -> the process-wide SQLite
# products/orders tables. Those tables have no tenant_id column, so one tenant's
# agent would recite another's demo SKUs and write its customers' PII there.
# AdapterFactory falls back to StubPOSAdapter only for DEFAULT_TENANT_ID.
DEFAULT_INTEGRATIONS: Dict[str, Any] = {
    "inventory": {
        "enabled": True,
        "sources": [],
    },
    "crm": {"enabled": True, "provider": "internal", "config": {}},
    "calendar": {"enabled": True, "provider": "internal", "config": {}},
}'''),
])

# ================= T02 =================
edit('backend/adapters/factory.py', [
("""from backend.adapters.stub_pos import EmptyPOSAdapter, StubPOSAdapter
from backend.integrations.service import IntegrationService, normalize_integrations
from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)

SQL_PROVIDERS = {"postgres", "sqlserver", "mysql"}""",
'''from backend.adapters.stub_pos import EmptyPOSAdapter, StubPOSAdapter
from backend.config import settings
from backend.integrations.service import IntegrationService, normalize_integrations
from backend.tenant.context import TenantContext

logger = logging.getLogger(__name__)

SQL_PROVIDERS = {"postgres", "sqlserver", "mysql"}


def _is_demo_tenant(tenant: TenantContext) -> bool:
    """The shared SQLite POS is demo data. Only the default tenant may see it."""
    return getattr(tenant, "tenant_id", None) == settings.DEFAULT_TENANT_ID


def _empty_or_demo(tenant: TenantContext) -> POSAdapter:
    return StubPOSAdapter(tenant) if _is_demo_tenant(tenant) else EmptyPOSAdapter(tenant)'''),

('''        pid = provider_id.lower()
        if pid in ("stub", "sqlite", "none", ""):
            return StubPOSAdapter(tenant)
        if pid == "shopify":
            return ShopifyPOSAdapter(config, tenant)
        if pid in SQL_PROVIDERS:
            return SqlPOSAdapter(pid, config, tenant)
        logger.warning("Unknown inventory provider '%s' — using stub", pid)
        return StubPOSAdapter(tenant)''',
'''        pid = provider_id.lower()
        if pid in ("stub", "sqlite", "none", ""):
            # T01/T02: the stub is the shared demo SQLite catalog.
            if not _is_demo_tenant(tenant):
                logger.warning(
                    "Refusing to serve the demo catalog to tenant %s (provider=%r)",
                    getattr(tenant, "tenant_id", "?"), pid,
                )
            return _empty_or_demo(tenant)
        if pid == "shopify":
            return ShopifyPOSAdapter(config, tenant)
        if pid in SQL_PROVIDERS:
            return SqlPOSAdapter(pid, config, tenant)
        # T02: fail closed. A config typo or a provider removed in a later release
        # must not silently downgrade a real tenant to the demo catalog.
        logger.error(
            "Unknown inventory provider %r for tenant %s — serving no inventory",
            pid, getattr(tenant, "tenant_id", "?"),
        )
        return _empty_or_demo(tenant)'''),

('''        if not inv.get("enabled", True):
            if tenant.tenant_id == "alpha_default":
                return StubPOSAdapter(tenant)
            return EmptyPOSAdapter(tenant)''',
'''        if not inv.get("enabled", True):
            return _empty_or_demo(tenant)'''),

('''        if not adapters:
            if tenant.tenant_id == "alpha_default":
                return StubPOSAdapter(tenant)
            return EmptyPOSAdapter(tenant)''',
'''        if not adapters:
            return _empty_or_demo(tenant)'''),
])

# ================= T03 =================
edit('backend/integrations/knowledge_cache.py', [
('''    org_l = (org_name or "").lower()
    tid_l = (tenant_id or "").lower()
    chunks: list[tuple[str, str]] = []
    if "alpha" in org_l or "alpha_devs" in tid_l:
        chunks = list(_ALPHA_DEVS_BASELINE)''',
'''    # T03: this used to be an unanchored substring test ("alpha" in org_name),
    # so "Alphabet Logistics", "AlphaCare Dental" and "Alpharetta Motors" all
    # matched — and the baseline chunks are PERSISTED to Mongo, then read back
    # into that tenant's system prompt. Exact tenant match only.
    from backend.config import settings

    chunks: list[tuple[str, str]] = []
    if tenant_id == settings.DEFAULT_TENANT_ID:
        chunks = list(_ALPHA_DEVS_BASELINE)'''),
])

# ================= T05/T06/T07 =================
edit('backend/agent/tools.py', [
('''    product = await AdapterFactory.pos(await _load_tenant_context(config)).lookup_product(product_name.strip())
    if not product:
        product = _lookup_product(product_name.strip())
    if not product:
        return (
            f"I couldn't find a product matching '{product_name}'. "
            "We offer SaaS Starter ($49/mo), SaaS Professional ($199/mo), and SaaS Enterprise ($999/mo). "
            "Which one would you like to order?"
        )''',
'''    tenant_ctx = await _load_tenant_context(config)
    is_demo_tenant = tenant_id == settings.DEFAULT_TENANT_ID

    product = await AdapterFactory.pos(tenant_ctx).lookup_product(product_name.strip())
    # T05: the SQLite fallback is the shared demo catalog with no tenant column.
    # Only the demo tenant may fall back to it.
    if not product and is_demo_tenant:
        product = _lookup_product(product_name.strip())
    if not product:
        # T06: this used to recite Alpha's SaaS price list to every tenant's caller.
        return (
            f"I couldn't find anything matching '{product_name}' in our catalogue. "
            "Could you tell me the exact name, or describe what you're after?"
        )'''),

('''    sqlite_order_id = _create_sqlite_order(
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
    )

    await create_order(
        tenant_id=tenant_id,
        thread_id=thread_id,
        customer_name=customer_name.strip(),
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
        sqlite_order_id=sqlite_order_id,
    )''',
'''    # T07: the SQLite orders table has no tenant_id column and a globally shared
    # INTEGER PRIMARY KEY sequence, so writing every tenant's customer email and
    # phone there both leaks PII and lets order ids collide across tenants.
    # MongoDB is the authoritative, tenant-scoped store; SQLite stays demo-only.
    if is_demo_tenant:
        order_id = _create_sqlite_order(
            customer_email=customer_email.strip(),
            customer_phone=customer_phone.strip(),
            product_name=product["name"],
            total_price=product["price"],
        )
    else:
        order_id = await next_order_id(tenant_id)

    await create_order(
        tenant_id=tenant_id,
        thread_id=thread_id,
        customer_name=customer_name.strip(),
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
        sqlite_order_id=order_id,
    )'''),

('''    return (
        f"Perfect! I've taken your order for the {product['name']} at {product['price']}. "
        f"Your order number is {sqlite_order_id}. "''',
'''    return (
        f"Perfect! I've taken your order for the {product['name']} at {product['price']}. "
        f"Your order number is {order_id}. "'''),

("""    find_active_orders,
    cancel_order_record,""",
"""    find_active_orders,
    cancel_order_record,
    next_order_id,"""),
])

# ================= T08 + order-id allocator + A33 =================
edit('backend/database.py', [
('''def _cancel_sqlite_order(order_id: int) -> bool:
    """Mark a SQLite POS order as cancelled."""
    conn = sqlite3.connect(SQLITE_DB_PATH)''',
'''async def next_order_id(tenant_id: str) -> int:
    """
    Allocate a per-tenant order number atomically.

    T07: order ids used to come from the shared SQLite AUTOINCREMENT sequence,
    which is global across every tenant on the instance.
    """
    db = get_db()
    doc = await db.counters.find_one_and_update(
        {"_id": f"order_id:{tenant_id}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return int((doc or {}).get("seq", 1))


def _cancel_sqlite_order(order_id: int) -> bool:
    """Mark a SQLite POS order as cancelled (demo tenant only — see T08)."""
    conn = sqlite3.connect(SQLITE_DB_PATH)'''),

('''async def cancel_order_record(tenant_id: str, order_id: int) -> bool:
    """Mark an order as cancelled in MongoDB and SQLite."""
    db = get_db()
    result = await db.orders.update_one(
        {"tenant_id": tenant_id, "order_id": order_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    sqlite_updated = _cancel_sqlite_order(order_id)
    return result.modified_count > 0 or sqlite_updated''',
'''async def cancel_order_record(tenant_id: str, order_id: int) -> bool:
    """
    Mark an order as cancelled. MongoDB is authoritative and tenant-scoped.

    T08: this used to call _cancel_sqlite_order(order_id) unconditionally. The
    SQLite orders table has no tenant column and a shared id sequence, so tenant
    A cancelling their order #1042 could cancel tenant B's row — and returning
    True on the strength of that SQLite update alone reported success for an
    order the caller did not own.
    """
    from backend.config import settings

    db = get_db()
    result = await db.orders.update_one(
        {"tenant_id": tenant_id, "order_id": order_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled"}},
    )
    cancelled = result.modified_count > 0

    if tenant_id == settings.DEFAULT_TENANT_ID:
        # Keep the demo POS in step, but never let it decide the outcome.
        _cancel_sqlite_order(order_id)

    return cancelled'''),

# ---- A33: unescaped caller input in identity regexes ----
('''        identity_clauses.append({"email": {"$regex": f"^{email.strip()}$", "$options": "i"}})''',
'''        # A33: caller-supplied text must be escaped before it reaches a Mongo
        # $regex, or "." matches every record and "(a+)+$" is a ReDoS.
        identity_clauses.append({"email": {"$regex": f"^{re.escape(email.strip())}$", "$options": "i"}})'''),
('''        identity_clauses.append({"customer_email": {"$regex": f"^{email.strip()}$", "$options": "i"}})''',
'''        # A33: see find_active_appointments — escape before building the pattern.
        identity_clauses.append(
            {"customer_email": {"$regex": f"^{re.escape(email.strip())}$", "$options": "i"}}
        )'''),
])
print("BATCH 2a applied")

# ===== batch2a2.py =====
edit('backend/database.py', [
("""import os
import logging
import sqlite3""",
 """import os
import logging
import re
import sqlite3"""),
("""    doc = await db.counters.find_one_and_update(
        {"_id": f"order_id:{tenant_id}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )""",
 """    from pymongo import ReturnDocument

    doc = await db.counters.find_one_and_update(
        {"_id": f"order_id:{tenant_id}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )"""),
])
print("2a2 applied")

# ===== batch2b2.py =====

edit('backend/main.py', [
("""from backend.billing.routes import router as billing_router""",
 """from backend.billing.routes import router as billing_router
from backend.tenant.thread_scope import graph_config"""),

("""                config = {
                    "configurable": {"thread_id": thread_id, "tenant_id": tenant_id},
                    "recursion_limit": 12,
                }""",
 """                # T09: checkpoints are keyed on thread_id, which is a raw path
                # parameter here. Namespacing it stops one tenant resuming
                # another's conversation state.
                config = graph_config(tenant_id, thread_id, recursion_limit=12)"""),

("""    config = {
        "configurable": {
            "thread_id": thread_id,
            "tenant_id": tenant.tenant_id,
        }
    }""",
 """    config = graph_config(tenant.tenant_id, thread_id)   # T09"""),

("""    config = {
        "configurable": {"thread_id": agent_thread_id, "tenant_id": tenant_id},
        "recursion_limit": 12,
    }""",
 """    config = graph_config(tenant_id, agent_thread_id, recursion_limit=12)   # T09"""),

("""async def unlink_voice_call_route(call_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    await unlink_voice_call(call_id)
    return {"status": "unlinked", "call_id": call_id}""",
 '''async def unlink_voice_call_route(call_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    # T13: the authenticated tenant used to be accepted and then ignored, so any
    # tenant could unlink any call — which also stopped that call being metered.
    removed = await unlink_voice_call(tenant.tenant_id, call_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No voice link found for this call")
    return {"status": "unlinked", "call_id": call_id}'''),

# T11/T12 raise PermissionError -> surface as 409, not 500
("""    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    await link_voice_call(tenant.tenant_id, call_id, console_thread_id)
    return {"status": "linked", "call_id": call_id, "console_thread_id": console_thread_id}""",
 """    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    try:
        await link_voice_call(tenant.tenant_id, call_id, console_thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))   # T11
    return {"status": "linked", "call_id": call_id, "console_thread_id": console_thread_id}"""),

("""    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    await register_voice_session(tenant.tenant_id, console_thread_id)""",
 """    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))   # T12"""),
])
print("2b2 (main.py) applied")

# ===== batch2b3.py =====

edit('backend/database.py', [
('''async def delete_conversation(tenant_id: str, thread_id: str):
    db = get_db()
    await db.conversations.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.leads.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.checkpoints.delete_many({"thread_id": thread_id})
    await db.writes.delete_many({"thread_id": thread_id})''',
'''async def delete_conversation(tenant_id: str, thread_id: str):
    """
    T10: the checkpoint deletes used to filter on thread_id alone, so
    `DELETE /api/conversations/<any thread id>` destroyed another tenant's live
    agent state. Checkpoints are stored under the namespaced key (see T09).
    """
    from backend.tenant.thread_scope import scoped_thread_id

    db = get_db()
    await db.conversations.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})
    await db.leads.delete_many({"tenant_id": tenant_id, "thread_id": thread_id})

    ckpt_key = scoped_thread_id(tenant_id, thread_id)
    await db.checkpoints.delete_many({"thread_id": ckpt_key})
    await db.writes.delete_many({"thread_id": ckpt_key})'''),

('''    db = get_db()
    await db.voice_call_links.update_one(
        {"call_id": call_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "call_id": call_id,
                "console_thread_id": console_thread_id,
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )''',
'''    db = get_db()
    # T11: the filter used to be {call_id} alone, and call_id was globally unique,
    # so any tenant could POST a victim's live call id and rewrite the row's
    # tenant_id — redirecting that customer's conversation into their own
    # transcript and billing the minutes wherever they chose.
    existing = await db.voice_call_links.find_one({"call_id": call_id}, {"tenant_id": 1})
    if existing and existing.get("tenant_id") not in (None, tenant_id):
        logger.warning(
            "Refusing cross-tenant voice link: call_id=%s owned by %s, requested by %s",
            call_id, existing.get("tenant_id"), tenant_id,
        )
        raise PermissionError("This call is linked to a different tenant.")

    await db.voice_call_links.update_one(
        {"call_id": call_id, "tenant_id": tenant_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "call_id": call_id,
                "console_thread_id": console_thread_id,
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )'''),

('''    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.voice_call_sessions.update_one(
        {"console_thread_id": console_thread_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "console_thread_id": console_thread_id,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )''',
'''    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    # T12: keyed on console_thread_id alone, an attacker could register a victim's
    # (guessable) embed thread id and have resolve_voice_thread serve the victim's
    # caller the ATTACKER's system prompt and knowledge base.
    existing = await db.voice_call_sessions.find_one(
        {"console_thread_id": console_thread_id}, {"tenant_id": 1}
    )
    if existing and existing.get("tenant_id") not in (None, tenant_id):
        logger.warning(
            "Refusing cross-tenant voice session: console_thread_id=%s owned by %s, requested by %s",
            console_thread_id, existing.get("tenant_id"), tenant_id,
        )
        raise PermissionError("This session belongs to a different tenant.")

    await db.voice_call_sessions.update_one(
        {"console_thread_id": console_thread_id, "tenant_id": tenant_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "console_thread_id": console_thread_id,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )'''),

('''async def unlink_voice_call(call_id: str) -> None:
    db = get_db()
    await db.voice_call_links.delete_one({"call_id": call_id})''',
'''async def unlink_voice_call(tenant_id: str, call_id: str) -> bool:
    """T13: scoped to the caller's tenant; returns whether anything was removed."""
    db = get_db()
    result = await db.voice_call_links.delete_one({"call_id": call_id, "tenant_id": tenant_id})
    return result.deleted_count > 0'''),
])

# T14: make the voice uniqueness constraints tenant-scoped
edit('backend/db_indexes.py', [
('''    await db.checkpoints.create_index("thread_id", name="checkpoints_thread_id")
    await db.writes.create_index("thread_id", name="writes_thread_id")''',
'''    # T09: thread_id reaches the checkpointer namespaced as "<tenant_id>::<thread_id>",
    # so this index is tenant-partitioned by prefix.
    await db.checkpoints.create_index("thread_id", name="checkpoints_thread_id")
    await db.writes.create_index("thread_id", name="writes_thread_id")'''),
])
print("2b3 applied")

# ===== batch2b4.py =====

# T14: console_thread_id is client-supplied and guessable ("embed_<12 hex>").
# Even with the T11/T12 ownership checks, a globally-unique index lets an attacker
# pre-claim ids to deny a victim's session creation. Prefixing server-side makes a
# cross-tenant collision impossible by construction, so the unique index is safe.
edit('backend/main.py', [
("""from backend.tenant.thread_scope import graph_config""",
 """from backend.tenant.thread_scope import graph_config, scoped_thread_id"""),

("""    console_thread_id = (data or {}).get("console_thread_id") or f"embed_{uuid.uuid4().hex[:12]}"
    await register_voice_session(tenant.tenant_id, console_thread_id)""",
 """    # T14: always tenant-prefixed, whether the client supplied one or not.
    console_thread_id = scoped_thread_id(
        tenant.tenant_id,
        (data or {}).get("console_thread_id") or f"embed_{uuid.uuid4().hex[:12]}",
    )
    await register_voice_session(tenant.tenant_id, console_thread_id)"""),

("""    console_thread_id = data.get("console_thread_id")
    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)""",
 """    console_thread_id = data.get("console_thread_id")
    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    console_thread_id = scoped_thread_id(tenant.tenant_id, console_thread_id)   # T14
    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)"""),

("""    call_id = data.get("call_id")
    console_thread_id = data.get("console_thread_id")
    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")""",
 """    call_id = data.get("call_id")
    console_thread_id = data.get("console_thread_id")
    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    console_thread_id = scoped_thread_id(tenant.tenant_id, console_thread_id)   # T14"""),
])

edit('backend/db_indexes.py', [
('''    await db.voice_call_sessions.create_index(
        "console_thread_id",
        unique=True,
        name="voice_session_console_thread",
    )''',
'''    # T14: console_thread_id is tenant-prefixed at every write site, so a
    # cross-tenant collision is impossible by construction and this uniqueness
    # constraint can no longer be used to deny another tenant's session creation.
    await db.voice_call_sessions.create_index(
        "console_thread_id",
        unique=True,
        name="voice_session_console_thread",
    )
    await db.voice_call_sessions.create_index(
        [("tenant_id", 1), ("console_thread_id", 1)],
        name="voice_session_tenant_console_thread",
    )'''),
])
print("2b4 applied")

# ===== batch2c.py =====

# T04: SYSTEM_PROMPT hardcodes Alpha's $49/$199/$999 SaaS packages AND tells the
# model to answer from them without checking tools. Handing that to a real tenant
# as the fallback — then prepending a "CRITICAL IDENTITY: you are <org>" line —
# gives the model two contradictory sources of truth, which is the single most
# reliable way to make it hallucinate.
edit('backend/agent/graph.py', [
("""from backend.agent.prompts import SYSTEM_PROMPT""",
 """from backend.agent.prompts import SYSTEM_PROMPT, build_tenant_system_prompt"""),

("""    ctx = await get_tenant_by_id(tenant_id)
    prompt_template = await get_tenant_system_prompt(tenant_id, SYSTEM_PROMPT)""",
 '''    ctx = await get_tenant_by_id(tenant_id)

    # T04: only the demo tenant may fall back to the Alpha demo prompt. Everyone
    # else gets a neutral, org-specific template with no invented catalogue.
    if tenant_id == settings.DEFAULT_TENANT_ID:
        fallback_prompt = SYSTEM_PROMPT
    else:
        fallback_prompt = build_tenant_system_prompt(
            (ctx.org_name if ctx else None) or tenant_id,
            (getattr(ctx.settings, "company_description", "") if ctx else "") or "",
        )
    prompt_template = await get_tenant_system_prompt(tenant_id, fallback_prompt)'''),
])
print("2c (T04) applied")

# ===== batch2d.py =====

print("BATCH 2d — ripple fixes")

# ---- Ripple 1: resolve_voice_thread must not 500 when the link is contested ----
edit('backend/database.py', [
('''    if console_from_meta:
        if not tenant_id:
            session = await db.voice_call_sessions.find_one({"console_thread_id": console_from_meta})
            tenant_id = (session or {}).get("tenant_id")
        tenant_id = tenant_id or settings.DEFAULT_TENANT_ID
        await link_voice_call(tenant_id, call_id, console_from_meta)
        return console_from_meta, console_from_meta, tenant_id''',
'''    if console_from_meta:
        if not tenant_id:
            session = await db.voice_call_sessions.find_one({"console_thread_id": console_from_meta})
            tenant_id = (session or {}).get("tenant_id")
        tenant_id = tenant_id or settings.DEFAULT_TENANT_ID
        try:
            await link_voice_call(tenant_id, call_id, console_from_meta)
        except PermissionError:
            # T11 now rejects a cross-tenant claim. On this path that must degrade
            # to an isolated thread rather than raising — a raised exception here
            # would 500 the whole spoken turn.
            logger.warning(
                "Voice link contested for call %s (tenant %s) — using an isolated thread",
                call_id, tenant_id,
            )
            return f"vapi_{call_id}", None, tenant_id
        return console_from_meta, console_from_meta, tenant_id'''),
])

# ---- Ripple 2: revert server-side console_thread_id prefixing ----
# The dashboard sends threadIdRef.current to /api/conversations/{id}/typed,
# /api/voice/register-session and /api/voice/link, and ignores the id the API
# returns. Prefixing server-side would file typed chat under the raw id while the
# voice path looked it up under the prefixed one, silently breaking typed capture
# and the dashboard's view of the voice transcript.
# Cross-tenant hijack is already blocked by the T11/T12 ownership checks; the
# residual pre-claim DoS needs server-generated ids and a frontend change (T15).
edit('backend/main.py', [
("""from backend.tenant.thread_scope import graph_config, scoped_thread_id""",
 """from backend.tenant.thread_scope import graph_config"""),

("""    # T14: always tenant-prefixed, whether the client supplied one or not.
    console_thread_id = scoped_thread_id(
        tenant.tenant_id,
        (data or {}).get("console_thread_id") or f"embed_{uuid.uuid4().hex[:12]}",
    )
    await register_voice_session(tenant.tenant_id, console_thread_id)""",
 """    console_thread_id = (data or {}).get("console_thread_id") or f"embed_{uuid.uuid4().hex[:12]}"
    await register_voice_session(tenant.tenant_id, console_thread_id)"""),

("""    console_thread_id = scoped_thread_id(tenant.tenant_id, console_thread_id)   # T14
    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)""",
 """    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)"""),

("""        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    console_thread_id = scoped_thread_id(tenant.tenant_id, console_thread_id)   # T14""",
 """        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")"""),
])

edit('backend/db_indexes.py', [
('''    # T14: console_thread_id is tenant-prefixed at every write site, so a
    # cross-tenant collision is impossible by construction and this uniqueness
    # constraint can no longer be used to deny another tenant's session creation.
    await db.voice_call_sessions.create_index(''',
'''    # T14: kept globally unique so resolve_voice_thread can look a session up by
    # console_thread_id alone (the Vapi webhook has no authenticated tenant).
    # Cross-tenant hijack is blocked by the ownership checks in
    # register_voice_session / link_voice_call. See T15 for the remaining
    # pre-claim denial-of-service, which needs server-generated ids.
    await db.voice_call_sessions.create_index('''),
])
print("BATCH 2d applied")

# ===== batch2e.py =====

edit('backend/agent/graph.py', [
('''        fallback_prompt = build_tenant_system_prompt(
            (ctx.org_name if ctx else None) or tenant_id,
            (getattr(ctx.settings, "company_description", "") if ctx else "") or "",
        )''',
 '''        # ctx may be None (tenant row missing/inactive) or a partially populated
        # context, so every hop is guarded — this runs on the voice hot path and
        # an AttributeError here would surface as "Sorry, I hit a small snag".
        _settings = getattr(ctx, "settings", None)
        fallback_prompt = build_tenant_system_prompt(
            getattr(ctx, "org_name", None) or tenant_id,
            getattr(_settings, "company_description", None) or "",
        )'''),
])
print("2e applied")

# ===== batch2b (tools.py thread-id reads) =====
_p = ROOT / 'backend/agent/tools.py'
_src = _p.read_text()
_old = '''    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")'''
assert _src.count(_old) == 9, f"expected 9 thread_id reads, found {_src.count(_old)}"
_src = _src.replace(_old, '''    thread_id = logical_thread_id(config)''')
_src = _src.replace(
"""from backend.adapters.factory import AdapterFactory
from backend.config import settings""",
"""from backend.adapters.factory import AdapterFactory
from backend.config import settings
from backend.tenant.thread_scope import logical_thread_id""", 1)
_p.write_text(_src)
print("  patched backend/agent/tools.py (9 thread_id reads + import)")

# ===== new module: backend/tenant/thread_scope.py =====
(_ROOT_TS := ROOT / 'backend/tenant/thread_scope.py').write_text('"""\nTenant-scoped LangGraph checkpoint keys (audit T09/T10).\n\n`MongoDBSaver` keys checkpoints on `(thread_id, checkpoint_ns, checkpoint_id)`\nand reads `checkpoint_ns` from `config["configurable"]`. LangGraph\'s Pregel\nruntime overwrites `checkpoint_ns` with "" at the top level, so it cannot be\nused for tenancy — the only field we control is `thread_id`.\n\nLeft unscoped, `thread_id` is entirely client-supplied (a path parameter on\n`/ws/chat/{thread_id}`, a body field on `/api/query`, `console_thread_id` on the\nembed session) and predictable, so tenant A could resume tenant B\'s conversation\nstate: full message history, extracted lead PII and prior tool results.\n\nSo the checkpoint key is namespaced, while the *logical* thread id — the one\nused for `conversations`, `leads`, `appointments` and `orders` — stays clean.\nTools must therefore read `logical_thread_id(config)` rather than\n`config["configurable"]["thread_id"]`.\n"""\nfrom __future__ import annotations\n\nfrom typing import Any, Dict, Optional\n\nSEP = "::"\n\n\ndef scoped_thread_id(tenant_id: str, thread_id: str) -> str:\n    """Checkpoint key for this tenant\'s copy of `thread_id`."""\n    tid = (tenant_id or "").strip() or "unknown_tenant"\n    raw = (thread_id or "").strip() or "default_thread"\n    if raw.startswith(f"{tid}{SEP}"):\n        return raw          # already scoped — never double-prefix\n    return f"{tid}{SEP}{raw}"\n\n\ndef unscope_thread_id(tenant_id: str, value: str) -> str:\n    """Inverse of `scoped_thread_id`; returns `value` unchanged if not scoped."""\n    prefix = f"{(tenant_id or \'\').strip()}{SEP}"\n    if tenant_id and value and value.startswith(prefix):\n        return value[len(prefix):]\n    return value\n\n\ndef graph_config(\n    tenant_id: str,\n    thread_id: str,\n    *,\n    recursion_limit: Optional[int] = None,\n    **extra: Any,\n) -> Dict[str, Any]:\n    """Build a RunnableConfig whose checkpoints cannot collide across tenants."""\n    configurable: Dict[str, Any] = {\n        "thread_id": scoped_thread_id(tenant_id, thread_id),\n        "logical_thread_id": thread_id,\n        "tenant_id": tenant_id,\n    }\n    configurable.update(extra)\n    config: Dict[str, Any] = {"configurable": configurable}\n    if recursion_limit is not None:\n        config["recursion_limit"] = recursion_limit\n    return config\n\n\ndef logical_thread_id(config: Any) -> str:\n    """\n    The un-namespaced thread id, for tools writing tenant-scoped records.\n\n    Falls back to stripping the prefix so an old in-flight config (or a caller\n    that builds its own) still resolves correctly.\n    """\n    cfg = (config or {}).get("configurable", {}) or {}\n    explicit = cfg.get("logical_thread_id")\n    if explicit:\n        return str(explicit)\n    raw = cfg.get("thread_id") or "default_thread"\n    return unscope_thread_id(cfg.get("tenant_id") or "", str(raw))\n')
print('  created backend/tenant/thread_scope.py')
print('BATCH 2 applied to', ROOT.resolve())
