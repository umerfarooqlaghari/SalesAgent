import os
import logging
import hmac
import hashlib

from typing import Dict, Any, List, Optional, AsyncIterator
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
import json

from backend.config import settings
from backend.tenant.context import TenantContext
from backend.auth.dependencies import get_tenant_or_api_key, require_secret_tenant
from backend.auth.security import decode_access_token
from backend.auth.service import get_user_session, seed_super_admin
from backend.tenant.registry import (
    migrate_legacy_documents_to_default_tenant,
    resolve_tenant_by_api_key,
    seed_default_tenant,
)
from backend.db_indexes import ensure_all_indexes
from backend.database import (
    db_client,
    get_db,
    get_lead,
    save_lead,
    list_leads,
    save_conversation_message,
    get_conversation,
    list_conversations,
    seed_default_api_key,
    rename_conversation,
    delete_conversation,
    list_appointments,
    list_orders,
    link_voice_call,
    get_linked_console_thread,
    unlink_voice_call,
    get_recent_typed_chat_messages,
    resolve_voice_thread,
    register_voice_session,
    _extract_voice_metadata,
)
from backend.agent.graph import get_agent_graph
from backend.admin.routes import router as admin_router
from backend.auth.routes import router as auth_router
from backend.superadmin.routes import router as superadmin_router
from backend.tenant.registry import get_tenant_by_id

from backend.billing.routes import router as billing_router
from backend.supervisors.routes import router as supervisors_router
from backend.tenant.thread_scope import graph_config

active_connections: Dict[str, WebSocket] = {}

# V15: asyncio.create_task returns a task the loop only weakly references. Without
# keeping a strong reference it can be garbage-collected mid-flight, silently
# dropping the write.
_BACKGROUND_TASKS: set = set()


def spawn_background(coro, *, label: str = "task"):
    import asyncio as _asyncio

    task = _asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(t):
        _BACKGROUND_TASKS.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("Background %s failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backend.main")

app = FastAPI(title="B2B Sales SDR Agent API")
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(superadmin_router)
app.include_router(billing_router)
app.include_router(supervisors_router)

# S06: tenant websites embed our widget/voice endpoints directly with an API
# key (no cookies) — those need an open CORS policy. Everything else (the
# authenticated dashboard SPA, which sends cookies/JWT) must be locked to
# configured origins with allow_credentials=True. A single shared
# allow_origins=["*"] + allow_credentials=True policy previously exposed every
# authenticated dashboard route to any origin.
_EMBED_PATH_PREFIXES = (
    "/api/embed",
    "/api/widget",
    "/api/voice/public-key",
    "/api/voice/warmup",
    "/api/query",
    "/query",
)


def _dashboard_allowed_origins() -> list:
    origins = settings.allowed_origins_list
    return origins if origins else [settings.DASHBOARD_URL]


class ScopedCORSMiddleware:
    def __init__(self, app):
        self.embed_app = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.dashboard_app = CORSMiddleware(
            app,
            allow_origins=_dashboard_allowed_origins(),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if any(path.startswith(p) for p in _EMBED_PATH_PREFIXES):
                await self.embed_app(scope, receive, send)
                return
        await self.dashboard_app(scope, receive, send)


app.add_middleware(ScopedCORSMiddleware)


async def verify_vapi_signature(request: Request) -> None:
    """
    S01/S02: /chat/completions and /webhook take an assistant-configured URL
    and used to accept ANY POST with no auth at all — a raw HMAC signature
    check on the outbound webhook secret is the mechanism Vapi itself
    supports, so this is the only realistic way to authenticate Vapi's own
    requests (they carry no tenant API key).
    """
    secret = settings.VAPI_WEBHOOK_SECRET
    if not secret:
        if settings.is_production:
            raise HTTPException(status_code=503, detail="Voice webhook is not configured.")
        logger.warning("VAPI_WEBHOOK_SECRET is unset — skipping signature verification (dev only).")
        return

    # Vapi has shipped two server-URL auth mechanisms and which one a given
    # assistant sends depends on how it was configured:
    #
    #   * a custom header  (commonly `X-Vapi-Secret`) whose value IS the shared
    #     secret — a plain equality check;
    #   * an HMAC-SHA256 of the raw body in `X-Vapi-Signature`.
    #
    # Accepting only the HMAC form was a live deploy risk: setting
    # VAPI_WEBHOOK_SECRET would have 401'd every real request and killed voice
    # entirely, with no way to tell that apart from an attack. Both are accepted;
    # BOTH are constant-time; an absent/garbage credential is still rejected.
    body = await request.body()
    secret_bytes = secret.encode()

    # Vapi's "Custom Credential" UI lets the operator pick ANY header name, and
    # its default is `Authorization` with an optional `Bearer ` prefix. Accept
    # that too, or a correctly-configured integration 401s and voice is dead.
    shared = (
        request.headers.get("X-Vapi-Secret")
        or request.headers.get("X-Vapi-Token")
        or ""
    )
    if not shared:
        auth = request.headers.get("Authorization") or ""
        if auth:
            # Tolerate "Bearer <secret>" and a bare "<secret>".
            shared = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()

    if shared:
        # .encode() first: hmac.compare_digest raises TypeError on str inputs
        # containing non-ASCII, which would 500 instead of returning 401.
        if hmac.compare_digest(shared.encode("utf-8", "replace"), secret_bytes):
            return

    signature = request.headers.get("X-Vapi-Signature", "")
    if signature:
        expected = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()
        # Vapi has also been observed prefixing the digest (`sha256=…`); tolerate it.
        candidate = signature.split("=", 1)[-1].strip().lower()
        if hmac.compare_digest(candidate.encode("utf-8", "replace"), expected.encode()):
            return

    logger.warning(
        "Rejected an unauthenticated Vapi request path=%s (had X-Vapi-Secret=%s, "
        "X-Vapi-Signature=%s) — if real Vapi traffic is being rejected, check which "
        "auth mechanism the assistant's server URL is configured to send.",
        request.url.path, bool(shared), bool(signature),
    )
    raise HTTPException(status_code=401, detail="Invalid Vapi signature.")


async def enforce_minutes_quota(tenant: TenantContext = Depends(get_tenant_or_api_key)) -> TenantContext:
    """
    S14/S16: publishable-key routes (public key, widget config, embed
    session, direct query) previously had no quota check at all, or (in
    get_widget_config) an inline check that the other three routes lacked —
    letting a tenant keep starting calls/queries past their allowed minutes.
    """
    db = get_db()
    tenant_doc = await db.tenants.find_one(
        {"tenant_id": tenant.tenant_id},
        {"used_minutes": 1, "allowed_minutes": 1},
    )
    if tenant_doc:
        used = tenant_doc.get("used_minutes", 0.0)
        allowed = tenant_doc.get("allowed_minutes", 30)
        if used >= allowed:
            raise HTTPException(
                status_code=403,
                detail="SaaS billing limits exceeded. Please upgrade your plan.",
            )
    return tenant

MAX_EMBED_PROMPT_CHARS = 4000

# Per-key ceiling for browser-embedded publishable keys. Generous for a real
# visitor, ruinous for a scraper.
EMBED_QUERIES_PER_MINUTE = 20
EMBED_QUERIES_PER_HOUR = 300


def _request_origin(request: Request) -> str:
    """The page that issued the request, if the browser told us."""
    origin = request.headers.get("Origin") or ""
    if origin:
        return origin
    referer = request.headers.get("Referer") or ""
    if referer:
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(referer)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except Exception:
            return ""
    return ""


def enforce_embed_guards(request: Request, tenant: TenantContext) -> None:
    """
    S16: rate-limit and origin-check a browser-embeddable key.

    Origin checking is OPT-IN per tenant (settings.allowed_embed_origins). An
    empty list means "any origin", which is what every existing tenant has, so
    this cannot silently break a live widget — but a tenant that fills it in
    gets a key that only works on their own site.

    Rate limiting is NOT opt-in: the point is to bound the damage from a key
    that is, by design, public.
    """
    from backend.auth.rate_limit import check_rate_limit
    from backend.tenant.key_scope import get_key_scope

    if get_key_scope() != "publishable":
        return  # secret keys and dashboard sessions are already accountable

    identity = tenant.tenant_id
    if not check_rate_limit("embed-query-min", identity, limit=EMBED_QUERIES_PER_MINUTE,
                            window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    if not check_rate_limit("embed-query-hour", identity, limit=EMBED_QUERIES_PER_HOUR,
                            window_seconds=3600):
        raise HTTPException(status_code=429, detail="Hourly limit reached for this site key.")

    allowed = []
    try:
        allowed = [
            str(o).strip().rstrip("/").lower()
            for o in ((tenant.settings or {}).get("allowed_embed_origins") or [])
            if str(o).strip()
        ]
    except Exception:
        allowed = []
    if not allowed:
        return

    origin = _request_origin(request).rstrip("/").lower()
    if origin and origin in allowed:
        return
    logger.warning(
        "Rejected embed query for tenant %s from origin %r (allowed: %s)",
        tenant.tenant_id, origin or "<none>", allowed,
    )
    raise HTTPException(
        status_code=403,
        detail="This site key is not authorised for this domain.",
    )


_THOUGHT_OPEN = "<thought>"
_THOUGHT_CLOSE = "</thought>"


class ThoughtTokenParser:
    """
    Incremental splitter for `<thought>...</thought>` reasoning vs spoken response.

    V13: the previous implementation re-scanned a growing buffer on every token
    (O(n^2)) and indexed `emitted_response_idx` against a different substring in
    each branch, so any prose emitted BEFORE a late `<thought>` tag was silently
    dropped. This version is a single-pass state machine with one cursor.

    Note: nothing in the current prompts instructs the model to emit these tags,
    so in practice everything is treated as response text — which is exactly the
    behaviour the dashboard needs. Kept working so the feature can be switched on
    by adding the instruction to the system prompt.
    """

    def __init__(self):
        self._pending = ""
        self._inside = False

    def feed(self, token: str):
        self._pending += token or ""
        thoughts, response = [], []

        while True:
            if self._inside:
                idx = self._pending.find(_THOUGHT_CLOSE)
                if idx == -1:
                    # Hold back enough characters that a tag split across tokens
                    # is never mistaken for content.
                    keep = len(_THOUGHT_CLOSE) - 1
                    if len(self._pending) > keep:
                        thoughts.append(self._pending[:-keep])
                        self._pending = self._pending[-keep:]
                    break
                thoughts.append(self._pending[:idx])
                self._pending = self._pending[idx + len(_THOUGHT_CLOSE):]
                self._inside = False
            else:
                idx = self._pending.find(_THOUGHT_OPEN)
                if idx == -1:
                    keep = len(_THOUGHT_OPEN) - 1
                    if len(self._pending) > keep:
                        response.append(self._pending[:-keep])
                        self._pending = self._pending[-keep:]
                    break
                response.append(self._pending[:idx])
                self._pending = self._pending[idx + len(_THOUGHT_OPEN):]
                self._inside = True

        return "".join(thoughts), "".join(response)

    def flush(self):
        """Emit whatever is still held back. Call once the stream is complete."""
        tail, self._pending = self._pending, ""
        return (tail, "") if self._inside else ("", tail)


@app.on_event("startup")
async def startup_event():
    db_client.connect()
    await ensure_all_indexes()
    await seed_default_tenant()
    await migrate_legacy_documents_to_default_tenant()
    from backend.tenant.registry import (
        ensure_publishable_keys,
        migrate_stale_tenant_prompts,
        seed_default_knowledge,
    )

    await migrate_stale_tenant_prompts()
    await ensure_publishable_keys()
    await seed_default_api_key()
    await seed_default_knowledge()
    await seed_super_admin()
    try:
        await get_agent_graph()
        logger.info(
            "Startup complete: tenant indexes, default tenant, legacy migration, checkpointer warmed."
        )
    except Exception as e:
        logger.error(f"Failed to pre-warm checkpointer connection: {e}", exc_info=True)

@app.on_event("shutdown")
async def shutdown_event():
    db_client.disconnect()
    logger.info("Shutdown complete: Database connection closed.")

@app.get("/api/leads")
async def get_all_leads(tenant: TenantContext = Depends(require_secret_tenant)):
    return await list_leads(tenant.tenant_id)

@app.get("/api/leads/{thread_id}")
async def get_lead_by_id(thread_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    lead = await get_lead(tenant.tenant_id, thread_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    # Cast MongoDB ObjectId to string
    if "_id" in lead:
        lead["_id"] = str(lead["_id"])
    return lead

@app.post("/api/leads/{thread_id}")
async def update_lead_profile(
    thread_id: str,
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
):
    await save_lead(tenant.tenant_id, thread_id, data)
    return {"status": "success", "lead": data}

@app.get("/api/conversations")
async def get_all_conversations(tenant: TenantContext = Depends(require_secret_tenant)):
    return await list_conversations(tenant.tenant_id)

@app.get("/api/conversations/{thread_id}")
async def get_thread_conversation(thread_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    conv = await get_conversation(tenant.tenant_id, thread_id)
    if not conv:
        return {"thread_id": thread_id, "messages": []}
    return conv

@app.put("/api/conversations/{thread_id}/title")
async def update_conversation_title(
    thread_id: str,
    data: Dict[str, str] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
):
    title = data.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    await rename_conversation(tenant.tenant_id, thread_id, title)
    return {"status": "success", "thread_id": thread_id, "title": title}

@app.delete("/api/conversations/{thread_id}")
async def delete_conversation_route(thread_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    await delete_conversation(tenant.tenant_id, thread_id)
    return {"status": "success", "thread_id": thread_id}

@app.post("/api/conversations/{thread_id}/typed")
async def append_typed_message(
    thread_id: str,
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
):
    """
    Append a user-typed message during an active voice call without running the chat agent.
    The voice pipeline reads these messages on the next spoken turn.
    """
    message = (data.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    await save_conversation_message(tenant.tenant_id, thread_id, "user", message, source="chat")
    return {"status": "saved", "thread_id": thread_id}


async def _tenant_from_jwt(token: str):
    """
    S18 ripple: a password reset bumps token_version, so a websocket auth
    check that only verifies the JWT signature (and never compares
    token_version against the live session) would keep honouring a JWT that
    was supposed to be invalidated by the reset.
    """
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        return None
    session = await get_user_session(payload["sub"])
    if not session or not session.tenant_id:
        return None
    if payload.get("tver", 0) != session.token_version:
        return None
    from backend.tenant.registry import get_tenant_by_id
    return await get_tenant_by_id(session.tenant_id)


@app.websocket("/ws/chat/{thread_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    thread_id: str,
    api_key: Optional[str] = None,
    token: Optional[str] = None,
):
    await websocket.accept()

    tenant = None
    if token:
        tenant = await _tenant_from_jwt(token)
    if not tenant and api_key:
        tenant = await resolve_tenant_by_api_key(api_key)

    # S11: query-string credentials land in server access logs and browser
    # history. Until the frontend drops that path entirely, also accept a
    # first-frame JSON auth message so a client CAN avoid putting the token in
    # the URL — additive, does not remove the existing query-string support.
    if not tenant:
        try:
            import asyncio
            first = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        except Exception:
            first = None
        if isinstance(first, dict) and first.get("type") == "auth":
            if first.get("token"):
                tenant = await _tenant_from_jwt(first["token"])
            elif first.get("api_key"):
                tenant = await resolve_tenant_by_api_key(first["api_key"])

    if not tenant:
        logger.warning(f"Unauthorized WebSocket attempt: thread={thread_id}")
        await websocket.send_json({"type": "unauthorized", "message": "Invalid or missing credentials"})
        await websocket.close(code=3000)
        return

    tenant_id = tenant.tenant_id
    conn_key = f"{tenant_id}:{thread_id}"   # V16: thread_id alone collides across tenants
    active_connections[conn_key] = websocket
    logger.info(f"WebSocket client connected for thread: {thread_id} (tenant={tenant_id})")

    try:
        conv = await get_conversation(tenant_id, thread_id)
        if conv:
            await websocket.send_json({"type": "history", "messages": conv.get("messages", [])})

        lead = await get_lead(tenant_id, thread_id)
        if lead:
            # Format ObjectId
            if "_id" in lead:
                lead["_id"] = str(lead["_id"])
            await websocket.send_json({"type": "lead_status", "lead": lead})
            
        while True:
            # Read user message
            data = await websocket.receive_json()
            user_message = data.get("message")
            if not user_message:
                continue
                
            # 1. Save user message to transcript
            await save_conversation_message(tenant_id, thread_id, "user", user_message)

            lead = await get_lead(tenant_id, thread_id)
            if lead and lead.get("status") in ["Handoff Requested", "Human Claimed"]:
                logger.info(f"Thread {thread_id} is in handoff mode. Suppressing agent execution.")
                await websocket.send_json({
                    "type": "status",
                    "status": "Human Operator mode active. Waiting for response..."
                })
                continue
                
            # 3. Trigger LangGraph execution
            await websocket.send_json({"type": "status", "status": "Thinking..."})
            
            try:
                graph = await get_agent_graph()
                parser = ThoughtTokenParser()
                
                # T09: checkpoints are keyed on thread_id, which is a raw path
                # parameter here. Namespacing it stops one tenant resuming
                # another's conversation state.
                config = graph_config(tenant_id, thread_id, recursion_limit=12)
                inputs = {
                    "messages": [HumanMessage(content=user_message)],
                    "thread_id": thread_id,
                    "tenant_id": tenant_id,
                }
                
                full_thought = ""
                full_response = ""
                stream_started = False
                
                async for event in graph.astream_events(inputs, config=config, version="v2"):
                    kind = event["event"]
                    name = event["name"]
                    
                    if kind == "on_node_start":
                        if name == "sdr_agent" and not stream_started:
                            stream_started = True
                            await websocket.send_json({"type": "stream_start"})
                        if name == "tools":
                            await websocket.send_json({"type": "status", "status": "Running tools..."})
                    elif kind == "on_chat_model_stream":
                        if event.get("metadata", {}).get("langgraph_node") != "sdr_agent":
                            continue
                            
                        chunk = event["data"]["chunk"]
                        token = chunk.content
                        if isinstance(token, list):
                            text_parts = []
                            for part in token:
                                if isinstance(part, dict) and "text" in part:
                                    text_parts.append(part["text"])
                                elif isinstance(part, str):
                                    text_parts.append(part)
                            token = "".join(text_parts)
                        
                        if token and isinstance(token, str):
                            if not stream_started:
                                stream_started = True
                                await websocket.send_json({"type": "stream_start"})
                            new_thought, new_response = parser.feed(token)
                            if new_thought:
                                full_thought += new_thought
                                await websocket.send_json({"type": "thought", "token": new_thought})
                            if new_response:
                                full_response += new_response
                                await websocket.send_json({"type": "response", "token": new_response})
                    elif kind == "on_tool_start":
                        inputs_val = event["data"].get("input", {})
                        await websocket.send_json({
                            "type": "tool_start",
                            "tool": name,
                            "inputs": inputs_val
                        })
                    elif kind == "on_tool_end":
                        output = event["data"].get("output", "")
                        await websocket.send_json({
                            "type": "tool_end",
                            "tool": name,
                            "output": str(output)
                        })
                
                # V13: emit any characters the parser held back for tag-boundary safety
                tail_thought, tail_response = parser.flush()
                if tail_thought:
                    full_thought += tail_thought
                    await websocket.send_json({"type": "thought", "token": tail_thought})
                if tail_response:
                    full_response += tail_response
                    await websocket.send_json({"type": "response", "token": tail_response})

                # Save assistant response to DB
                if full_response or full_thought:
                    await save_conversation_message(
                        tenant_id,
                        thread_id,
                        "assistant",
                        full_response,
                        thought=full_thought if full_thought else None,
                    )

                updated_lead = await get_lead(tenant_id, thread_id)
                if updated_lead:
                    if "_id" in updated_lead:
                        updated_lead["_id"] = str(updated_lead["_id"])
                    await websocket.send_json({"type": "lead_status", "lead": updated_lead})
                    
                    # Check for handoff trigger
                    if updated_lead.get("status") == "Handoff Requested":
                        await websocket.send_json({
                            "type": "handoff",
                            "reason": updated_lead.get("handoff_reason", "Requested by system logic")
                        })
                        await websocket.send_json({
                            "type": "status",
                            "status": "Transferred to a human operator. A representative will join shortly."
                        })
                        continue
                        
                await websocket.send_json({"type": "status", "status": "Idle"})
            except Exception as e:
                logger.error(f"Error during agent execution: {e}", exc_info=True)
                error_msg = str(e)
                if "RESOURCE_EXHAUSTED" in error_msg or "429" in error_msg:
                    user_facing_error = "Gemini API Quota Exceeded (429 Rate Limit). Please verify your billing/tier or try again shortly."
                else:
                    user_facing_error = f"Agent Error: {error_msg}"
                await websocket.send_json({
                    "type": "error",
                    "message": user_facing_error
                })
            
    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {thread_id}")
    except Exception as e:
        logger.error(f"WebSocket error on thread {thread_id}: {e}", exc_info=True)
    finally:
        active_connections.pop(conn_key, None)

@app.get("/api/voice/public-key")
async def get_vapi_public_key(tenant: TenantContext = Depends(enforce_minutes_quota)):
    return {"public_key": settings.VAPI_PUBLIC_KEY, "tenant_id": tenant.tenant_id}

@app.get("/api/widget/config")
@app.get("/api/embed/config")
async def get_widget_config(tenant: TenantContext = Depends(enforce_minutes_quota)):
    """Returns Vapi keys + tenant_id for website embed widgets (API key auth)."""
    from backend.integrations.catalog_cache import schedule_warmup

    schedule_warmup(tenant.tenant_id)

    return {
        "vapi_public_key": settings.VAPI_PUBLIC_KEY,
        "vapi_assistant_id": settings.VAPI_ASSISTANT_ID,
        "tenant_id": tenant.tenant_id,
        "org_name": tenant.org_name,
        "backend_url": settings.DASHBOARD_URL.replace("3000", "8765") if "localhost" in settings.DASHBOARD_URL else None,
    }


@app.post("/api/embed/warmup")
@app.post("/api/voice/warmup")
async def warmup_catalog_route(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Prefetch knowledge (await) + SQL catalog (background) before a voice call."""
    import asyncio

    from backend.integrations.catalog_cache import schedule_warmup
    from backend.integrations.knowledge_cache import warmup_knowledge

    # Knowledge is what answers services/packages — must be fast.
    # SQL catalog probes can hang for tens of seconds; never block voice on them.
    try:
        k = await asyncio.wait_for(warmup_knowledge(tenant.tenant_id, force=False), timeout=3.0)
    except Exception as e:
        k = {"ok": False, "error": str(e)}
    schedule_warmup(tenant.tenant_id)
    return {
        "tenant_id": tenant.tenant_id,
        "ok": bool(k.get("ok")),
        "knowledge": k,
        "catalog": {"ok": True, "status": "warming_in_background"},
    }


@app.post("/api/embed/session")
async def create_embed_session(
    data: Dict[str, Any] = Body(default={}),
    tenant: TenantContext = Depends(enforce_minutes_quota),
):
    """
    One-shot tenant website integration:
    returns Vapi keys + tenant_id, registers a voice session, and warms catalog cache.
    Client should start Vapi with metadata.tenant_id + metadata.console_thread_id.
    """
    import uuid

    import asyncio

    from backend.integrations.catalog_cache import get_cached_catalog, schedule_warmup
    from backend.integrations.knowledge_cache import get_cached_knowledge, warmup_knowledge

    console_thread_id = (data or {}).get("console_thread_id") or f"embed_{uuid.uuid4().hex[:12]}"
    await register_voice_session(tenant.tenant_id, console_thread_id)

    # Knowledge/FAQ must be ready before first spoken turn (SQL catalog can warm in background)
    try:
        await asyncio.wait_for(warmup_knowledge(tenant.tenant_id), timeout=2.5)
    except Exception as e:
        logger.warning("Knowledge warmup on embed/session: %s", e)
    schedule_warmup(tenant.tenant_id)

    knowledge = get_cached_knowledge(tenant.tenant_id)
    cached = get_cached_catalog(tenant.tenant_id)
    facts_ready = bool(knowledge or cached)

    return {
        "ok": True,
        "tenant_id": tenant.tenant_id,
        "org_name": tenant.org_name,
        "console_thread_id": console_thread_id,
        "vapi_public_key": settings.VAPI_PUBLIC_KEY,
        "vapi_assistant_id": settings.VAPI_ASSISTANT_ID,
        "warmup": {
            "ok": True,
            "cached": facts_ready,
            "chars": len(knowledge or "") + len(cached or ""),
            "status": "ready" if facts_ready else "warming",
            "knowledge_chars": len(knowledge or ""),
            "catalog_chars": len(cached or ""),
        },
        "metadata": {
            "tenant_id": tenant.tenant_id,
            "org_name": tenant.org_name,
            "console_thread_id": console_thread_id,
        },
    }

@app.post("/api/widget/query")
@app.post("/api/query")
@app.post("/query")
async def execute_query_route(
    request: Request,
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(enforce_minutes_quota),
):
    """
    Direct text query endpoint for client applications sending { "question": "..." } or { "message": "..." }.
    Executes the Sales SDR agent and returns the response.
    """
    # S16: this accepts pk_live_ publishable keys, which are embedded in the
    # tenant's own public website HTML. The minutes quota alone does not cover
    # it — a scraper with the key can drain the tenant's model budget at line
    # rate, and each request is an uncapped prompt.
    enforce_embed_guards(request, tenant)

    question = (data.get("question") or data.get("message") or data.get("prompt") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="'question' or 'message' field is required in request body.")
    if len(question) > MAX_EMBED_PROMPT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Question is too long (limit {MAX_EMBED_PROMPT_CHARS} characters).",
        )

    thread_id = data.get("thread_id") or f"client_query_{tenant.tenant_id}"

    await save_conversation_message(tenant.tenant_id, thread_id, "user", question, source="chat")

    from backend.integrations.catalog_cache import schedule_warmup

    schedule_warmup(tenant.tenant_id)

    agent_graph = await get_agent_graph()
    initial_state = {
        "messages": [HumanMessage(content=question)],
        "thread_id": thread_id,
        "tenant_id": tenant.tenant_id,
    }
    config = graph_config(tenant.tenant_id, thread_id)   # T09

    final_state = await agent_graph.ainvoke(initial_state, config)

    output_messages = final_state.get("messages", [])
    answer = "I'm sorry, I couldn't process your request."
    for msg in reversed(output_messages):
        if getattr(msg, "type", None) == "ai" and getattr(msg, "content", None):
            answer = str(msg.content)
            break

    await save_conversation_message(tenant.tenant_id, thread_id, "assistant", answer, source="chat")

    return {
        "status": "success",
        "question": question,
        "answer": answer,
        "response": answer,
        "thread_id": thread_id,
        "tenant_id": tenant.tenant_id,
    }

@app.get("/api/appointments")
async def get_appointments(tenant: TenantContext = Depends(require_secret_tenant)):
    """Returns all scheduled appointments from MongoDB."""
    appts = await list_appointments(tenant.tenant_id)
    return {"appointments": appts}

@app.get("/api/orders")
async def get_orders(tenant: TenantContext = Depends(require_secret_tenant)):
    """Returns all customer orders from MongoDB."""
    orders = await list_orders(tenant.tenant_id)
    return {"orders": orders}

@app.post("/api/voice/link")
async def link_voice_call_route(
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
):
    """Link a Vapi call ID to the console chat thread for typed detail capture."""
    call_id = data.get("call_id")
    console_thread_id = data.get("console_thread_id")
    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    try:
        await link_voice_call(tenant.tenant_id, call_id, console_thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))   # T11
    return {"status": "linked", "call_id": call_id, "console_thread_id": console_thread_id}


@app.post("/api/voice/register-session")
async def register_voice_session_route(
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(require_secret_tenant),
):
    """Register tenant scope for a console thread before Vapi assigns a call id."""
    console_thread_id = data.get("console_thread_id")
    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    try:
        await register_voice_session(tenant.tenant_id, console_thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))   # T12
    from backend.integrations.catalog_cache import schedule_warmup

    schedule_warmup(tenant.tenant_id)
    return {"status": "registered", "console_thread_id": console_thread_id, "tenant_id": tenant.tenant_id}


async def _voice_greeting(tenant_id: str) -> str:
    ctx = await get_tenant_by_id(tenant_id)
    name = (ctx.org_name if ctx else None) or "our team"
    return f"Hello! Welcome to {name}. How can I help you today?"

@app.delete("/api/voice/link/{call_id}")
async def unlink_voice_call_route(call_id: str, tenant: TenantContext = Depends(require_secret_tenant)):
    # T13: the authenticated tenant used to be accepted and then ignored, so any
    # tenant could unlink any call — which also stopped that call being metered.
    removed = await unlink_voice_call(tenant.tenant_id, call_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No voice link found for this call")
    return {"status": "unlinked", "call_id": call_id}

async def _get_typed_chat_context(tenant_id: str, console_thread_id: str, since_iso: Optional[str] = None) -> str:
    typed = await get_recent_typed_chat_messages(tenant_id, console_thread_id, since_iso=since_iso, limit=8)
    if not typed:
        return ""
    lines = "\n".join(f"  • {msg}" for msg in typed)
    return (
        "\n\n[TYPED CHAT MESSAGES — prefer these for name/email/phone over spoken dictation]:\n"
        f"{lines}"
    )

_VAPI_MAX_TURNS = 12

# V11: total wall-clock budget for one spoken turn, shared by the fast path and
# the graph. Vapi drops calls that stall; keep this comfortably under its limit.
from backend.observability import turn_metrics as _tm

VOICE_TURN_DEADLINE = 7.0
VOICE_FASTPATH_TIMEOUT = 2.0


def _vapi_content_to_text(raw) -> str:
    """Vapi content is either a string or a list of {type,text} parts."""
    if isinstance(raw, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in raw
        ).strip()
    return str(raw or "").strip()


def _vapi_messages_to_lc(messages_list: list, max_turns: int = _VAPI_MAX_TURNS) -> list:
    """
    Convert Vapi's OpenAI-format history into LangChain messages.

    The voice graph has no checkpointer (checkpoint I/O per turn was a major
    cause of Vapi timeouts), so Vapi's own payload is our ONLY source of
    conversation memory. Dropping it makes multi-turn detail collection
    ("name, then email, then phone") structurally impossible and causes the
    agent to re-invoke the same tool every turn.
    """
    from langchain_core.messages import AIMessage

    out: list = []
    for m in (messages_list or [])[-(max_turns * 2):]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        # Vapi sends its own system prompt; we build ours in sdr_node.
        if role not in ("user", "assistant"):
            continue
        content = _vapi_content_to_text(m.get("content", ""))
        if not content:
            continue
        out.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    return out


# Tools whose return value is caller-ready prose. Everything else (SQL dumps,
# CRM records, sanitised error refs) must never be read aloud verbatim.
_SPEAKABLE_TOOLS = {
    "book_appointment",
    "place_order",
    "cancel_appointment",
    "reschedule_appointment",
    "cancel_order",
    "lookup_appointments",
    "handoff_to_human",
}


def _extract_assistant_text(messages_out: list) -> str:
    """Pull the last speakable assistant text or speakable tool result from graph output."""
    import re

    def _normalize_content(content) -> str:
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        elif not isinstance(content, str):
            content = str(content)
        if content and "<thought>" in content and "</thought>" in content:
            content = re.sub(r"<thought>.*?</thought>", "", content, flags=re.DOTALL).strip()
        return (content or "").strip()

    if not messages_out:
        return "Got it! How else can I help you today?"

    # Examine ONLY the output messages produced in the current turn (after the last HumanMessage)
    last_human_idx = -1
    for idx in range(len(messages_out) - 1, -1, -1):
        if getattr(messages_out[idx], "type", None) in ("human", "user"):
            last_human_idx = idx
            break

    turn_messages = messages_out[last_human_idx + 1:] if last_human_idx != -1 else messages_out

    # 1. Prefer speakable tool results from the CURRENT turn (e.g. book_appointment confirmation)
    for msg in reversed(turn_messages):
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) in _SPEAKABLE_TOOLS:
            text = _normalize_content(getattr(msg, "content", ""))
            if text:
                return text

    # 2. Prefer AI spoken text from the CURRENT turn
    for msg in reversed(turn_messages):
        if getattr(msg, "type", None) == "ai":
            text = _normalize_content(getattr(msg, "content", ""))
            if text:
                return text

    # 3. Fall back to speakable tool results from earlier turns
    for msg in reversed(messages_out):
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) in _SPEAKABLE_TOOLS:
            text = _normalize_content(getattr(msg, "content", ""))
            if text:
                return text

    # 4. Fall back to AI messages from earlier turns (ignoring fallback phrases)
    for msg in reversed(messages_out):
        if getattr(msg, "type", None) == "ai":
            text = _normalize_content(getattr(msg, "content", ""))
            if text and "didn't catch that" not in text.lower():
                return text

    return "Got it! How else can I help you today?"

@app.post("/api/voice/chat/completions", dependencies=[Depends(verify_vapi_signature)])
@app.post("/chat/completions", dependencies=[Depends(verify_vapi_signature)])
async def vapi_chat_completions(data: Dict[str, Any] = Body(...)):
    messages_list = data.get("messages", [])
    wants_stream = data.get("stream", False)
    call_data = data.get("call", {}) or {}

    voice_thread = await resolve_voice_thread(call_data, data)
    agent_thread_id = voice_thread.agent_thread_id
    console_thread_id = voice_thread.console_thread_id
    tenant_id = voice_thread.tenant_id

    # Check if tenant has exceeded billing minutes limit.
    # P11: projected — this used to pull the whole tenant document (the entire
    # system prompt and every integration config) to read two numbers.
    db = get_db()
    tenant_doc = await db.tenants.find_one(
        {"tenant_id": tenant_id},
        {"used_minutes": 1, "allowed_minutes": 1},
    )
    if tenant_doc:
        used = tenant_doc.get("used_minutes", 0.0)
        allowed = tenant_doc.get("allowed_minutes", 30)
        if used >= allowed:
            blocked_msg = "We're sorry, this assistant has exceeded its usage limits. Please upgrade the account on your dashboard."
            fallback = {
                "choices": [{"message": {"role": "assistant", "content": blocked_msg}}]
            }
            if wants_stream:
                async def blocked_gen():
                    chunk = {"choices": [{"delta": {"role": "assistant", "content": blocked_msg}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(done)}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(blocked_gen(), media_type="text/event-stream")
            return fallback
    call_id = call_data.get("id") or "vapi_default_session"
    logger.info(
        "Vapi LLM request: call_id=%s tenant_id=%s thread=%s console=%s",
        call_id,
        tenant_id,
        agent_thread_id,
        console_thread_id,
    )

    # Find last user message from Vapi payload
    user_content = ""
    for msg in reversed(messages_list):
        if isinstance(msg, dict) and msg.get("role") == "user":
            user_content = _vapi_content_to_text(msg.get("content", ""))
            break

    # Pull typed chat context when console is linked to this call.
    # P09: reuses the link document resolve_voice_thread already fetched.
    typed_context = ""
    if console_thread_id:
        typed_context = await _get_typed_chat_context(
            tenant_id, console_thread_id, since_iso=voice_thread.linked_at
        )

    # If Vapi sent no user speech but caller typed in chat, use typed content instead of resetting
    if not user_content.strip() and typed_context:
        user_content = typed_context.replace(
            "\n\n[TYPED CHAT MESSAGES — prefer these for name/email/phone over spoken dictation]:\n", ""
        ).strip()
        typed_context = ""  # already merged into user_content

    # Avoid generic greeting when we have typed input or an ongoing linked conversation
    if not messages_list and not user_content.strip():
        if console_thread_id and typed_context:
            user_content = typed_context.replace(
                "\n\n[TYPED CHAT MESSAGES — prefer these for name/email/phone over spoken dictation]:\n", ""
            ).strip()
            typed_context = ""
        elif not console_thread_id:
            # P10: computed lazily — this branch is the only one that speaks it,
            # and it used to cost a tenant read on every turn.
            greeting = await _voice_greeting(tenant_id)
            fallback = {
                "choices": [{"message": {"role": "assistant", "content": greeting}}]
            }
            if wants_stream:
                async def fallback_gen():
                    chunk = {"choices": [{"delta": {"role": "assistant", "content": greeting}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(done)}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(fallback_gen(), media_type="text/event-stream")
            return fallback

    if not user_content.strip():
        # Linked call with no speech yet — ask caller to type or speak rather than resetting
        assistant_msg = (
            "I'm still here with you. You can type your details in the chat box, "
            "or say them out loud and I'll read them back to confirm."
        )
        await save_conversation_message(tenant_id, agent_thread_id, "assistant", assistant_msg, source="voice")

        async def gentle_prompt_stream() -> AsyncIterator[str]:
            chunk = {
                "id": f"chatcmpl-{agent_thread_id}",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": assistant_msg}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            done_chunk = {
                "id": f"chatcmpl-{agent_thread_id}",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gentle_prompt_stream(), media_type="text/event-stream")

    enriched_user_content = user_content
    if typed_context and typed_context not in user_content:
        enriched_user_content = user_content + typed_context

    # Persist user turn in background — do not block first TTS byte
    import asyncio

    spawn_background(
        save_conversation_message(tenant_id, agent_thread_id, "user", user_content, source="voice"),
        label="persist-voice-user-turn",
    )

    from backend.agent.graph import get_voice_agent_graph
    from backend.integrations.knowledge_cache import get_cached_knowledge
    from backend.integrations.voice_fastpath import try_voice_faq_answer

    config = graph_config(tenant_id, agent_thread_id, recursion_limit=12)   # T09
    # V01: Vapi already sends the full conversation — use it as the memory the
    # checkpointer-less voice graph otherwise lacks. The final human turn is
    # replaced with the typed-chat-enriched version.
    _history = _vapi_messages_to_lc(messages_list)
    if _history and isinstance(_history[-1], HumanMessage):
        _history[-1] = HumanMessage(content=enriched_user_content)
    else:
        _history.append(HumanMessage(content=enriched_user_content))

    inputs = {
        "messages": _history,
        "thread_id": agent_thread_id,
        "tenant_id": tenant_id,
        "channel": "voice",
    }

    def _sse(content: str = "", *, role: Optional[str] = None, finish: Optional[str] = None) -> str:
        delta: Dict[str, Any] = {}
        if role:
            delta["role"] = role
        if content:
            delta["content"] = content
        chunk = {
            "id": f"chatcmpl-{agent_thread_id}",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    async def _timeout_fallback() -> str:
        """
        Spoken when a turn misses its deadline.

        This must NOT read out the company blurb. Doing so made the agent appear
        to "break" mid-booking: the caller gave a date and time, the turn ran long,
        and instead of confirming the appointment the agent started describing what
        the company does. A timeout is a *stall*, so the reply has to keep the
        caller in whatever they were already doing.

        It must also never name a specific industry (V09) — the old hardcoded
        "AI ERP, computer vision, SaaS, ed-tech" line was one tenant's service
        list being spoken to all of them.
        """
        return (
            "Sorry, I didn't quite catch that — could you say that one more time?"
        )

    # Stream IMMEDIATELY so Vapi does not drop the call waiting on Gemini/Mongo
    async def stream_response() -> AsyncIterator[str]:
        from langgraph.errors import GraphRecursionError

        from backend.agent.graph import _ACTION_KEYWORDS

        yield _sse(role="assistant")
        assistant_msg = ""
        t0 = asyncio.get_event_loop().time()

        def _remaining() -> float:
            # V11: ONE budget for the whole turn. Previously the fast path (4.5s)
            # and the graph (9s) were sequential, so worst case was 13.5s — well
            # past what Vapi tolerates.
            return max(0.5, VOICE_TURN_DEADLINE - (asyncio.get_event_loop().time() - t0))

        _low = (enriched_user_content or "").lower()
        looks_like_action = any(k in _low for k in _ACTION_KEYWORDS) or len(messages_list) >= 2

        # FAQ fast-path: no LangGraph, no tools — prevents "let me check" stalls.
        # Skipped for action intents (book/order/cancel), which always need tools.
        fast = None
        if not looks_like_action:
            try:
                fast = await asyncio.wait_for(
                    try_voice_faq_answer(tenant_id, enriched_user_content),
                    timeout=min(VOICE_FASTPATH_TIMEOUT, _remaining()),
                )
            except asyncio.TimeoutError:
                # V10: a slow fast path must fall THROUGH to the real agent, not
                # short-circuit the whole turn into a canned fallback.
                logger.info("Voice fast-path timed out call_id=%s — falling through to graph", call_id)
            except Exception:
                logger.debug("Voice fast-path errored — falling through to graph", exc_info=True)

        turn_outcome = _tm.OK
        turn_detail = ""
        try:
            from backend.integrations.catalog_cache import is_catalog_warm

            _catalog_was_warm = is_catalog_warm(tenant_id)
        except Exception:
            _catalog_was_warm = None

        if fast:
            assistant_msg = fast
            turn_outcome = _tm.OK_FASTPATH
            logger.info(
                "Voice FAQ fast-path ok call_id=%s tenant=%s ms=%.0f",
                call_id,
                tenant_id,
                (asyncio.get_event_loop().time() - t0) * 1000,
            )
        else:
            try:
                graph = await get_voice_agent_graph()
                result = await asyncio.wait_for(
                    graph.ainvoke(inputs, config=config),
                    timeout=_remaining(),
                )
                assistant_msg = _extract_assistant_text(result.get("messages", []))
            except asyncio.TimeoutError:
                logger.warning(
                    "Vapi agent timed out call_id=%s tenant=%s — using knowledge fallback",
                    call_id,
                    tenant_id,
                )
                assistant_msg = await _timeout_fallback()
                turn_outcome = _tm.TURN_DEADLINE
            except GraphRecursionError:
                # V06: this is not a TimeoutError, so it used to land in the generic
                # handler below and surface as "Sorry, I hit a small snag."
                logger.error(
                    "Graph recursion limit hit call_id=%s tenant=%s thread=%s",
                    call_id,
                    tenant_id,
                    agent_thread_id,
                )
                assistant_msg = (
                    "Let me make sure I have this right — could you repeat that last detail for me?"
                )
                turn_outcome = _tm.RECURSION_LIMIT
            except Exception as e:
                logger.error("Vapi agent error for %s: %s", agent_thread_id, e, exc_info=True)
                error_msg = str(e)
                # Classify BEFORE choosing the wording, so the operator can tell a
                # model-quota problem from a tool problem from a config problem.
                # All three used to collapse into "hit a small snag".
                turn_outcome = _tm.classify_exception(e)
                turn_detail = type(e).__name__
                if turn_outcome == _tm.QUOTA_EXHAUSTED:
                    assistant_msg = (
                        "I'm experiencing a brief system delay. "
                        "Could you repeat that, or I can have a team member call you back?"
                    )
                elif turn_outcome == _tm.CONFIG_ERROR:
                    assistant_msg = (
                        "I can't reach our catalog right now. "
                        "Can I take your details so a colleague can follow up?"
                    )
                else:
                    assistant_msg = "Sorry, I hit a small snag. Could you say that again?"

        if not (assistant_msg or "").strip():
            assistant_msg = await _timeout_fallback()

        # Strip stall phrases if the model still produced one. Never swap in the
        # company blurb here — mid-booking that reads as the agent losing the plot.
        low = assistant_msg.lower()
        if "let me check" in low or "one moment" in low or "pull that up" in low:
            assistant_msg = await _timeout_fallback()

        _tm.record_turn_bg(
            tenant_id=tenant_id,
            channel="voice",
            outcome=turn_outcome,
            duration_ms=(asyncio.get_event_loop().time() - t0) * 1000,
            model=settings.GEMINI_MODEL,
            catalog_warm=_catalog_was_warm,
            detail=turn_detail,
        )

        yield _sse(assistant_msg)
        yield _sse(finish="stop")
        yield "data: [DONE]\n\n"

        try:
            await save_conversation_message(
                tenant_id, agent_thread_id, "assistant", assistant_msg, source="voice"
            )
        except Exception:
            logger.debug("Failed to persist voice assistant message", exc_info=True)

    return StreamingResponse(stream_response(), media_type="text/event-stream")

@app.post("/api/voice/webhook", dependencies=[Depends(verify_vapi_signature)])
@app.post("/webhook", dependencies=[Depends(verify_vapi_signature)])
async def vapi_webhook(data: Dict[str, Any] = Body(...)):
    message = data.get("message", {})
    msg_type = message.get("type")
    logger.info(f"Received Vapi webhook event: {msg_type}")

    if msg_type == "end-of-call-report":
        call = message.get("call", {})
        call_id = call.get("id")
        duration = call.get("duration")  # In seconds

        if call_id and duration is not None:
            # S02: clamp an obviously bogus/malicious duration (a compromised
            # or misbehaving Vapi-compatible client claiming a multi-day call)
            # before it inflates a tenant's billed minutes.
            duration = max(0, min(float(duration), 4 * 3600))

            db = get_db()
            # S02: end-of-call-report can be redelivered (Vapi retries on a
            # non-2xx or timed-out response). A unique index on call_id makes
            # a duplicate delivery a no-op instead of double-billing minutes.
            try:
                await db.voice_billing_events.insert_one({"call_id": call_id, "duration": duration})
            except Exception as e:
                if "duplicate key" in str(e).lower() or e.__class__.__name__ == "DuplicateKeyError":
                    logger.info("Duplicate end-of-call-report for call %s — skipping re-metering", call_id)
                    return {"status": "success", "event": msg_type}
                raise

            # Resolve tenant ID from call link or call sessions, falling back
            # to whatever metadata Vapi attached to the call itself.
            tenant_id = None
            link_doc = await db.voice_call_links.find_one({"call_id": call_id})
            if link_doc:
                tenant_id = link_doc.get("tenant_id")
            else:
                session = await db.voice_call_sessions.find_one({"call_id": call_id})
                if session:
                    tenant_id = session.get("tenant_id")

            if not tenant_id:
                tenant_id = _extract_voice_metadata({"call": call}).get("tenant_id")

            if tenant_id:
                minutes_used = duration / 60.0
                await db.tenants.update_one(
                    {"tenant_id": tenant_id},
                    {"$inc": {"used_minutes": minutes_used}}
                )
                logger.info(
                    "Vapi call %s ended. Duration: %d seconds. Incremented tenant %s used_minutes by %.2f",
                    call_id, duration, tenant_id, minutes_used
                )
            else:
                logger.error(
                    "Vapi call %s ended with no attributable tenant — %.2f minutes not metered.",
                    call_id, duration / 60.0,
                )

    return {"status": "success", "event": msg_type}
