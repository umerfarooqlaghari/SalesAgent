import logging
from typing import Literal, Dict, Any
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from backend.agent.state import AgentState, LeadProfile
from backend.agent.tools import search_crm, update_lead_status, schedule_demo, query_pos_database, handoff_to_human
from backend.agent.checkpointer import get_checkpointer
from backend.database import get_lead
from backend.config import settings

logger = logging.getLogger(__name__)

# List of tools
agent_tools = [search_crm, update_lead_status, schedule_demo, query_pos_database, handoff_to_human]
tool_node = ToolNode(agent_tools)

SYSTEM_PROMPT = """You are an B2B Sales SDR Agent for Alpha. Your goal is to qualify leads, provide value-add information, and schedule demos.

Your active thread ID is {thread_id}.
Current Lead Profile status:
- Company: {company}
- Job Title: {job_title}
- Intent Score: {intent_score}
- Qualification Status: {status}
- Fit: {fit}

Operational Constraints:
1. Qualification: You must verify B2B fit based on firmographics (e.g., target companies are technology/SaaS companies, B2B services, mid-market to enterprise size). If a lead is not a fit, gracefully decline or suggest resources.
2. Tools: You have access to tools for `search_crm`, `update_lead_status`, `schedule_demo`, and `query_pos_database`. Use them strictly when needed. Use `query_pos_database` to look up product inventory or check a customer's order status. Note: To query an order status, you MUST request and pass the order_id and the customer's email or phone number for security verification.
3. Structured Thinking: Before outputting a response, analyze the lead's intent in a <thought> tag.
   Example response format:
   <thought>
   The user has high purchase intent and fit our firmographic profile. I will offer a demo.
   </thought>
   Sure! Based on your company size, I'd love to set up a quick 15-minute demo...
4. Handoff: If a lead exhibits high purchase intent or asks for a human, stop immediately and trigger the `handoff_to_human` tool.
5. Persistence: Remember context from previous turns. If a user asks "as I mentioned before," cross-reference the conversation history.
6. Tone & Length: Speak in an extremely short, conversational, and crisp style (maximum 1 to 2 brief sentences). Avoid paragraphs, bullet points, or list structures completely. Respond naturally like a phone call agent. Never fabricate information. If you do not have the answer, state that you will connect them with a human specialist.
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
