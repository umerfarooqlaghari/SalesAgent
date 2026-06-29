import os
import logging
import dns.resolver

# Monkeypatch dnspython Resolver to force reliable nameservers globally, bypassing router SERVFAIL DNS errors
_orig_resolver_init = dns.resolver.Resolver.__init__
def _patched_resolver_init(self, *args, **kwargs):
    _orig_resolver_init(self, *args, **kwargs)
    self.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']
dns.resolver.Resolver.__init__ = _patched_resolver_init
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']


from typing import Dict, Any, List, Optional, AsyncIterator
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
import json

from backend.config import settings
from backend.tenant.context import TenantContext
from backend.auth.dependencies import get_tenant_or_api_key
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
)
from backend.agent.graph import get_agent_graph
from backend.admin.routes import router as admin_router
from backend.auth.routes import router as auth_router
from backend.superadmin.routes import router as superadmin_router
from backend.tenant.registry import get_tenant_by_id

active_connections: Dict[str, WebSocket] = {}
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backend.main")

app = FastAPI(title="B2B Sales SDR Agent API")
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(superadmin_router)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ThoughtTokenParser:
    def __init__(self):
        self.buffer = ""
        self.emitted_thought_idx = 0
        self.emitted_response_idx = 0
        
    def feed(self, token: str):
        self.buffer += token
        
        # Check if we are inside <thought> ... </thought>
        thought_start = self.buffer.find("<thought>")
        thought_end = self.buffer.find("</thought>")
        
        thoughts_to_emit = ""
        response_to_emit = ""
        
        if thought_start != -1:
            if thought_end != -1:
                # Both start and end found
                thought_content = self.buffer[thought_start + 9:thought_end]
                response_content = self.buffer[thought_end + 10:]
                
                new_thought = thought_content[self.emitted_thought_idx:]
                self.emitted_thought_idx += len(new_thought)
                if new_thought:
                    thoughts_to_emit = new_thought
                    
                new_response = response_content[self.emitted_response_idx:]
                self.emitted_response_idx += len(new_response)
                if new_response:
                    response_to_emit = new_response
            else:
                # Start found, but no end yet
                thought_content = self.buffer[thought_start + 9:]
                new_thought = thought_content[self.emitted_thought_idx:]
                self.emitted_thought_idx += len(new_thought)
                if new_thought:
                    thoughts_to_emit = new_thought
        else:
            # No thought tag found
            new_response = self.buffer[self.emitted_response_idx:]
            self.emitted_response_idx += len(new_response)
            if new_response:
                response_to_emit = new_response
                
        return thoughts_to_emit, response_to_emit

@app.on_event("startup")
async def startup_event():
    db_client.connect()
    await ensure_all_indexes()
    await seed_default_tenant()
    await migrate_legacy_documents_to_default_tenant()
    from backend.tenant.registry import migrate_stale_tenant_prompts, seed_default_knowledge

    await migrate_stale_tenant_prompts()
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
async def get_all_leads(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    return await list_leads(tenant.tenant_id)

@app.get("/api/leads/{thread_id}")
async def get_lead_by_id(thread_id: str, tenant: TenantContext = Depends(get_tenant_or_api_key)):
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
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    await save_lead(tenant.tenant_id, thread_id, data)
    return {"status": "success", "lead": data}

@app.get("/api/conversations")
async def get_all_conversations(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    return await list_conversations(tenant.tenant_id)

@app.get("/api/conversations/{thread_id}")
async def get_thread_conversation(thread_id: str, tenant: TenantContext = Depends(get_tenant_or_api_key)):
    conv = await get_conversation(tenant.tenant_id, thread_id)
    if not conv:
        return {"thread_id": thread_id, "messages": []}
    return conv

@app.put("/api/conversations/{thread_id}/title")
async def update_conversation_title(
    thread_id: str,
    data: Dict[str, str] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    title = data.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    await rename_conversation(tenant.tenant_id, thread_id, title)
    return {"status": "success", "thread_id": thread_id, "title": title}

@app.delete("/api/conversations/{thread_id}")
async def delete_conversation_route(thread_id: str, tenant: TenantContext = Depends(get_tenant_or_api_key)):
    await delete_conversation(tenant.tenant_id, thread_id)
    return {"status": "success", "thread_id": thread_id}

@app.post("/api/conversations/{thread_id}/typed")
async def append_typed_message(
    thread_id: str,
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
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
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            session = await get_user_session(payload["sub"])
            if session and session.tenant_id:
                from backend.tenant.registry import get_tenant_by_id
                tenant = await get_tenant_by_id(session.tenant_id)
    if not tenant and api_key:
        tenant = await resolve_tenant_by_api_key(api_key)

    if not tenant:
        logger.warning(f"Unauthorized WebSocket attempt: thread={thread_id}")
        await websocket.send_json({"type": "unauthorized", "message": "Invalid or missing credentials"})
        await websocket.close(code=3000)
        return

    tenant_id = tenant.tenant_id
    active_connections[thread_id] = websocket
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
                
                config = {
                    "configurable": {"thread_id": thread_id, "tenant_id": tenant_id},
                    "recursion_limit": 12,
                }
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
        active_connections.pop(thread_id, None)

@app.get("/api/voice/public-key")
async def get_vapi_public_key(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    return {"public_key": settings.VAPI_PUBLIC_KEY, "tenant_id": tenant.tenant_id}

@app.get("/api/widget/config")
async def get_widget_config(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Returns Vapi keys and tenant scoping info for the client-side wobbly widget."""
    return {
        "vapi_public_key": settings.VAPI_PUBLIC_KEY,
        "vapi_assistant_id": settings.VAPI_ASSISTANT_ID,
        "tenant_id": tenant.tenant_id
    }

@app.get("/api/appointments")
async def get_appointments(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Returns all scheduled appointments from MongoDB."""
    appts = await list_appointments(tenant.tenant_id)
    return {"appointments": appts}

@app.get("/api/orders")
async def get_orders(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Returns all customer orders from MongoDB."""
    orders = await list_orders(tenant.tenant_id)
    return {"orders": orders}

@app.post("/api/voice/link")
async def link_voice_call_route(
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Link a Vapi call ID to the console chat thread for typed detail capture."""
    call_id = data.get("call_id")
    console_thread_id = data.get("console_thread_id")
    if not call_id or not console_thread_id:
        raise HTTPException(status_code=400, detail="call_id and console_thread_id are required")
    await link_voice_call(tenant.tenant_id, call_id, console_thread_id)
    return {"status": "linked", "call_id": call_id, "console_thread_id": console_thread_id}


@app.post("/api/voice/register-session")
async def register_voice_session_route(
    data: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Register tenant scope for a console thread before Vapi assigns a call id."""
    console_thread_id = data.get("console_thread_id")
    if not console_thread_id:
        raise HTTPException(status_code=400, detail="console_thread_id is required")
    await register_voice_session(tenant.tenant_id, console_thread_id)
    return {"status": "registered", "console_thread_id": console_thread_id, "tenant_id": tenant.tenant_id}


async def _voice_greeting(tenant_id: str) -> str:
    ctx = await get_tenant_by_id(tenant_id)
    name = (ctx.org_name if ctx else None) or "our team"
    return f"Hello! Welcome to {name}. How can I help you today?"

@app.delete("/api/voice/link/{call_id}")
async def unlink_voice_call_route(call_id: str, tenant: TenantContext = Depends(get_tenant_or_api_key)):
    await unlink_voice_call(call_id)
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

def _extract_assistant_text(messages_out: list) -> str:
    """Pull the last speakable assistant text from graph output (handles tool-call turns)."""
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

    # Prefer the last AI message with spoken content
    for msg in reversed(messages_out):
        if getattr(msg, "type", None) == "ai":
            text = _normalize_content(getattr(msg, "content", ""))
            if text:
                return text

    # Fall back to any tool result (all tools return user-facing strings)
    for msg in reversed(messages_out):
        if getattr(msg, "type", None) == "tool":
            text = _normalize_content(getattr(msg, "content", ""))
            if text:
                return text

    return "Got it! How else can I help you today?"

@app.post("/api/voice/chat/completions")
@app.post("/chat/completions")
async def vapi_chat_completions(data: Dict[str, Any] = Body(...)):
    messages_list = data.get("messages", [])
    wants_stream = data.get("stream", False)
    call_data = data.get("call", {}) or {}

    agent_thread_id, console_thread_id, tenant_id = await resolve_voice_thread(call_data, data)
    call_id = call_data.get("id") or "vapi_default_session"
    greeting = await _voice_greeting(tenant_id)
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
        if msg.get("role") == "user":
            raw = msg.get("content", "")
            if isinstance(raw, list):
                user_content = " ".join(p.get("text", "") for p in raw if isinstance(p, dict))
            else:
                user_content = str(raw)
            break

    # Pull typed chat context when console is linked to this call
    typed_context = ""
    since_iso = None
    if console_thread_id:
        db = get_db()
        link_doc = await db.voice_call_links.find_one({"call_id": call_id})
        since_iso = link_doc.get("linked_at") if link_doc else None
        typed_context = await _get_typed_chat_context(tenant_id, console_thread_id, since_iso=since_iso)

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

    await save_conversation_message(tenant_id, agent_thread_id, "user", user_content, source="voice")

    # Run the LangGraph SDR brain
    graph = await get_agent_graph()
    config = {
        "configurable": {"thread_id": agent_thread_id, "tenant_id": tenant_id},
        "recursion_limit": 16,
    }
    inputs = {
        "messages": [HumanMessage(content=enriched_user_content)],
        "thread_id": agent_thread_id,
        "tenant_id": tenant_id,
    }

    try:
        result = await graph.ainvoke(inputs, config=config)
        messages_out = result.get("messages", [])
        assistant_msg = _extract_assistant_text(messages_out)
    except Exception as e:
        logger.error(f"Vapi agent error for {agent_thread_id}: {e}", exc_info=True)
        error_msg = str(e)
        if "RESOURCE_EXHAUSTED" in error_msg or "429" in error_msg:
            assistant_msg = (
                "I'm experiencing a brief system delay. "
                "Could you repeat that, or I can have a team member call you back?"
            )
        else:
            assistant_msg = (
                "Sorry, I hit a small snag on my end. "
                "Could you say that again, or would you like me to connect you with a team member?"
            )

    await save_conversation_message(tenant_id, agent_thread_id, "assistant", assistant_msg, source="voice")

    # Always stream SSE — Vapi requires streaming to feed TTS pipeline
    async def stream_response() -> AsyncIterator[str]:
        # Send the full content in one chunk then close
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

    return StreamingResponse(stream_response(), media_type="text/event-stream")

@app.post("/api/voice/webhook")
@app.post("/webhook")
async def vapi_webhook(data: Dict[str, Any] = Body(...)):
    message = data.get("message", {})
    msg_type = message.get("type")
    logger.info(f"Received Vapi webhook event: {msg_type}")
    return {"status": "success", "event": msg_type}
