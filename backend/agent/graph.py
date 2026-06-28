import logging
from typing import Literal, Dict, Any
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from backend.agent.state import AgentState, LeadProfile
from backend.agent.tools import search_crm, update_lead_status, schedule_demo, query_pos_database, handoff_to_human, book_appointment
from backend.agent.checkpointer import get_checkpointer
from backend.database import get_lead
from backend.config import settings

logger = logging.getLogger(__name__)

# List of tools
agent_tools = [search_crm, update_lead_status, schedule_demo, query_pos_database, handoff_to_human, book_appointment]
tool_node = ToolNode(agent_tools)

SYSTEM_PROMPT = """You are a friendly sales assistant for Alpha. Your goal is to understand what the caller needs, answer their questions, and book appointments.

Your active thread ID is {thread_id}.
Current Lead Profile status:
- Company: {company}
- Job Title: {job_title}
- Intent Score: {intent_score}
- Qualification Status: {status}
- Fit: {fit}

Operational Constraints:
1. Be helpful to ALL callers regardless of their business type — B2B, B2C, freelancer, startup, enterprise — everyone is welcome. Never reject or disqualify someone based on their business model.
2. Tools: You have access to `search_crm`, `update_lead_status`, `book_appointment`, and `query_pos_database`. Use `query_pos_database` to look up product pricing or inventory. Use `book_appointment` to schedule meetings.
3. Appointment Booking Flow: When a caller wants to book a meeting or consultation, collect the following one step at a time in a conversational way: (1) Full name, (2) Email address, (3) Phone number, (4) Preferred date, (5) Preferred time. Once you have all five, call the `book_appointment` tool.
4. Handoff: Trigger the `handoff_to_human` tool ONLY when the user explicitly says they want to speak with a human or be transferred. Never use it to reject or disqualify a lead.
5. Persistence: Remember context from previous turns.
6. Tone & Length: Extremely short, conversational, and crisp — maximum 1 to 2 sentences. Respond naturally like a phone call agent. Never fabricate information.
"""

class IntentResponse(BaseModel):
    intent: str = Field(description="Classified intent of the user message. Must be one of: 'Purchase', 'Inquiry', 'Support'")

async def router_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    if not messages:
        return {}

    thread_id = state.get("thread_id", "default_thread")
    if thread_id and thread_id.startswith("vapi_"):
        intent = "Inquiry"
    else:
        # Classify intent using Gemini
        llm = ChatGoogleGenerativeAI(
            api_key=settings.GEMINI_API_KEY,
            model="gemini-2.5-flash",
            temperature=0.0
        )
        structured_llm = llm.with_structured_output(IntentResponse)
        
        try:
            classification = await structured_llm.ainvoke([
                SystemMessage(content="You are an intent classifier. Categorize the user's query into 'Purchase', 'Inquiry', or 'Support'."),
                messages[-1]
            ])
            intent = classification.intent
        except Exception as e:
            logger.error(f"Error classifying intent: {e}")
            intent = "Inquiry"  # Default fallback
    
    # Sync or load existing LeadProfile from DB
    thread_id = state.get("thread_id", "default_thread")
    lead_doc = await get_lead(thread_id)
    
    lead_profile_dict = state.get("lead_profile") or {}
    if isinstance(lead_profile_dict, BaseModel):
        lead_profile_dict = lead_profile_dict.model_dump()
        
    if lead_doc:
        lead_profile = LeadProfile(
            company=lead_doc.get("company", lead_profile_dict.get("company")),
            job_title=lead_doc.get("job_title", lead_profile_dict.get("job_title")),
            intent_score=lead_doc.get("intent_score", lead_profile_dict.get("intent_score", 0)),
            status=lead_doc.get("status", lead_profile_dict.get("status", "New")),
            fit=lead_doc.get("fit", lead_profile_dict.get("fit"))
        )
    else:
        lead_profile = LeadProfile(**lead_profile_dict)
        
    return {
        "intent": intent,
        "lead_profile": lead_profile
    }

async def sdr_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    lead_profile = state.get("lead_profile")
    
    # Format system prompt with lead profile
    company = "Unknown"
    job_title = "Unknown"
    intent_score = 0
    status = "New"
    fit = "Unknown"
    
    if lead_profile:
        company = lead_profile.company or "Unknown"
        job_title = lead_profile.job_title or "Unknown"
        intent_score = lead_profile.intent_score
        status = lead_profile.status or "New"
        fit = str(lead_profile.fit) if lead_profile.fit is not None else "Unknown"

    system_prompt = SYSTEM_PROMPT.format(
        thread_id=state.get("thread_id", "unknown"),
        company=company,
        job_title=job_title,
        intent_score=intent_score,
        status=status,
        fit=fit
    )
    
    # Formulate messages with SystemMessage at index 0
    formatted_messages = [SystemMessage(content=system_prompt)] + list(messages)
    
    llm = ChatGoogleGenerativeAI(
        api_key=settings.GEMINI_API_KEY,
        model="gemini-2.5-flash",
        temperature=0.3
    )
    llm_with_tools = llm.bind_tools(agent_tools)
    
    response = await llm_with_tools.ainvoke(formatted_messages)
    
    return {
        "messages": [response]
    }

async def post_tool_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    thread_id = state.get("thread_id", "default_thread")
    
    # Check if handoff_to_human tool was called
    requires_handoff = state.get("requires_handoff", False)
    
    for msg in reversed(messages):
        if msg.type == "tool" and msg.name == "handoff_to_human":
            requires_handoff = True
            break
            
    # Sync lead profile
    lead_doc = await get_lead(thread_id)
    lead_profile = state.get("lead_profile")
    if lead_doc:
        lead_profile = LeadProfile(
            company=lead_doc.get("company"),
            job_title=lead_doc.get("job_title"),
            intent_score=lead_doc.get("intent_score", 0),
            status=lead_doc.get("status", "New"),
            fit=lead_doc.get("fit")
        )
        
    return {
        "requires_handoff": requires_handoff,
        "lead_profile": lead_profile
    }

def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END

def route_after_post_tool(state: AgentState) -> Literal["sdr_agent", "__end__"]:
    if state.get("requires_handoff"):
        return END
    return "sdr_agent"

# Build Graph
builder = StateGraph(AgentState)
builder.add_node("router", router_node)
builder.add_node("sdr_agent", sdr_node)
builder.add_node("tools", tool_node)
builder.add_node("post_tool_processor", post_tool_node)

builder.add_edge(START, "router")
builder.add_edge("router", "sdr_agent")
builder.add_conditional_edges(
    "sdr_agent",
    route_after_agent,
    {"tools": "tools", END: END}
)
builder.add_edge("tools", "post_tool_processor")
builder.add_conditional_edges(
    "post_tool_processor",
    route_after_post_tool,
    {"sdr_agent": "sdr_agent", END: END}
)

# Export compiled graph
async def get_agent_graph():
    checkpointer = await get_checkpointer()
    return builder.compile(checkpointer=checkpointer)
