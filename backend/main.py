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


from typing import Dict, Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage

from backend.config import settings
from backend.database import (
    db_client,
    get_lead,
    save_lead,
    list_leads,
    save_conversation_message,
    get_conversation,
    list_conversations,
    seed_default_api_key,
    validate_api_key_in_db,
    rename_conversation,
    delete_conversation
)
from backend.agent.graph import get_agent_graph

security = HTTPBearer()

async def validate_api_key(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    api_key = credentials.credentials
    if not await validate_api_key_in_db(api_key):
        raise HTTPException(status_code=401, detail="Invalid or inactive API Key")
    return api_key

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backend.main")

app = FastAPI(title="B2B Sales SDR Agent API")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_connections: Dict[str, WebSocket] = {}

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
    await seed_default_api_key()
    try:
        # Pre-warm connection and pre-create indices
        await get_agent_graph()
        logger.info("Startup complete: Database connection verified, checkpointer warmed, and default API Key seeded.")
    except Exception as e:
        logger.error(f"Failed to pre-warm checkpointer connection: {e}", exc_info=True)

@app.on_event("shutdown")
async def shutdown_event():
    db_client.disconnect()
    logger.info("Shutdown complete: Database connection closed.")

@app.get("/api/leads")
async def get_all_leads(api_key: str = Depends(validate_api_key)):
    return await list_leads()

@app.get("/api/leads/{thread_id}")
async def get_lead_by_id(thread_id: str, api_key: str = Depends(validate_api_key)):
    lead = await get_lead(thread_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    # Cast MongoDB ObjectId to string
    if "_id" in lead:
        lead["_id"] = str(lead["_id"])
    return lead

@app.post("/api/leads/{thread_id}")
async def update_lead_profile(thread_id: str, data: Dict[str, Any] = Body(...), api_key: str = Depends(validate_api_key)):
    await save_lead(thread_id, data)
    return {"status": "success", "lead": data}

@app.get("/api/conversations")
async def get_all_conversations(api_key: str = Depends(validate_api_key)):
    return await list_conversations()

@app.get("/api/conversations/{thread_id}")
async def get_thread_conversation(thread_id: str, api_key: str = Depends(validate_api_key)):
    conv = await get_conversation(thread_id)
    if not conv:
        return {"thread_id": thread_id, "messages": []}
    return conv

@app.put("/api/conversations/{thread_id}/title")
async def update_conversation_title(thread_id: str, data: Dict[str, str] = Body(...), api_key: str = Depends(validate_api_key)):
    title = data.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    await rename_conversation(thread_id, title)
    return {"status": "success", "thread_id": thread_id, "title": title}

@app.delete("/api/conversations/{thread_id}")
async def delete_conversation_route(thread_id: str, api_key: str = Depends(validate_api_key)):
    await delete_conversation(thread_id)
    return {"status": "success", "thread_id": thread_id}


# Human-in-the-Loop endpoints
@app.post("/api/handoffs/{thread_id}/claim")
async def claim_handoff(thread_id: str, api_key: str = Depends(validate_api_key)):
    lead = await get_lead(thread_id)
    if not lead:
        lead = {
            "thread_id": thread_id,
            "company": "Pending Verification",
            "job_title": "Pending Verification",
            "intent_score": 0,
            "status": "Human Claimed",
            "fit": None
        }
    else:
        lead["status"] = "Human Claimed"
        
    await save_lead(thread_id, lead)
    
    # Notify user client via active websocket
    if thread_id in active_connections:
        await active_connections[thread_id].send_json({
            "type": "status",
            "status": "A human representative has claimed the thread and joined the chat."
        })
        
    return {"status": "success", "lead": lead}

@app.post("/api/handoffs/{thread_id}/resolve")
async def resolve_handoff(thread_id: str, api_key: str = Depends(validate_api_key)):
    lead = await get_lead(thread_id)
    if not lead:
        lead = {
            "thread_id": thread_id,
            "company": "Pending Verification",
            "job_title": "Pending Verification",
            "intent_score": 0,
            "status": "In-Progress",
            "fit": None
        }
    else:
        lead["status"] = "In-Progress"
        lead.pop("handoff_reason", None)
        
    await save_lead(thread_id, lead)
    
    # Notify user client via active websocket
    if thread_id in active_connections:
        await active_connections[thread_id].send_json({
            "type": "status",
            "status": "The human representative has left the chat. SDR agent is now active."
        })
        
    return {"status": "success", "lead": lead}

@app.post("/api/handoffs/{thread_id}/message")
async def send_operator_message(thread_id: str, data: Dict[str, str] = Body(...), api_key: str = Depends(validate_api_key)):
    message = data.get("message")
    if not message:
        raise HTTPException(status_code=400, detail="Message content is required")
        
    # 1. Save operator response to database
    await save_conversation_message(thread_id, "assistant", message, thought="Sent by human operator")
    
    # 2. Push message to the user WebSocket if connected
    if thread_id in active_connections:
        await active_connections[thread_id].send_json({
            "type": "human_response",
            "message": message
        })
        # Keep status updated
        await active_connections[thread_id].send_json({
            "type": "status",
            "status": "Awaiting your reply..."
        })
        
    return {"status": "success", "message": message}

@app.websocket("/ws/chat/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str, api_key: Optional[str] = None):
    await websocket.accept()
    
    if not api_key or not await validate_api_key_in_db(api_key):
        logger.warning(f"Unauthorized WebSocket attempt: thread={thread_id}, key={api_key}")
        await websocket.send_json({"type": "unauthorized", "message": "Invalid or missing API Key"})
        await websocket.close(code=3000)
        return
        
    active_connections[thread_id] = websocket
    logger.info(f"WebSocket client connected for thread: {thread_id}")
    
    try:
        # Send historical messages to client
        conv = await get_conversation(thread_id)
        if conv:
            await websocket.send_json({"type": "history", "messages": conv.get("messages", [])})
            
        # Send current lead state
        lead = await get_lead(thread_id)
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
            await save_conversation_message(thread_id, "user", user_message)
            
            # 2. Check if thread is claimed by human
            lead = await get_lead(thread_id)
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
                
                config = {"configurable": {"thread_id": thread_id}}
                inputs = {"messages": [HumanMessage(content=user_message)], "thread_id": thread_id}
                
                full_thought = ""
                full_response = ""
                
                async for event in graph.astream_events(inputs, config=config, version="v2"):
                    kind = event["event"]
                    name = event["name"]
                    
                    if kind == "on_node_start":
                        node_label = "Routing" if name == "router" else "Formulating Response" if name == "sdr_agent" else name
                        await websocket.send_json({"type": "status", "status": f"Agent State: {node_label}"})
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
                        thread_id=thread_id,
                        role="assistant",
                        message=full_response,
                        thought=full_thought if full_thought else None
                    )
                    
                # Send final lead status updates to client
                updated_lead = await get_lead(thread_id)
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
async def get_vapi_public_key(api_key: str = Depends(validate_api_key)):
    return {"public_key": settings.VAPI_PUBLIC_KEY}

@app.post("/api/voice/chat/completions")
async def vapi_chat_completions(data: Dict[str, Any] = Body(...)):
    import re
    # Vapi sends messages, extract the last user message
    messages_list = data.get("messages", [])
    if not messages_list:
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Hello! Welcome to Alpha. How can I assist you today?"
                }
            }]
        }
        
    last_msg = messages_list[-1]
    user_content = last_msg.get("content", "")
    
    # Use unique Vapi Call ID or default fallback
    call_id = data.get("call", {}).get("id") or "vapi_default_session"
    thread_id = f"vapi_{call_id}"
    
    # Save user transcript message
    await save_conversation_message(thread_id, "user", user_content)
    
    # Run the SDR LangGraph graph
    graph = await get_agent_graph()
    config = {"configurable": {"thread_id": thread_id}}
    inputs = {"messages": [HumanMessage(content=user_content)], "thread_id": thread_id}
    
    result = await graph.ainvoke(inputs, config=config)
    
    # Extract the assistant's response and filter reasoning thought blocks
    assistant_msg = ""
    messages_out = result.get("messages", [])
    if messages_out:
        last_out = messages_out[-1]
        content = last_out.content
        
        # Resolve content if it is a list of blocks (multimodal/rich format)
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
                elif isinstance(part, str):
                    text_parts.append(part)
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)
            
        if "<thought>" in content and "</thought>" in content:
            content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL).strip()
        assistant_msg = content
        
    # Save assistant response transcript message
    await save_conversation_message(thread_id, "assistant", assistant_msg)
        
    # Return OpenAI-compatible response format
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": assistant_msg
                }
            }
        ]
    }
