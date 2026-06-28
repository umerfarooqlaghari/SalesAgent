import logging
from typing import Literal, Dict, Any

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from backend.agent.intent import heuristic_intent

from backend.agent.state import AgentState, LeadProfile
from backend.agent.tools import (
    search_crm,
    update_lead_status,
    schedule_demo,
    query_pos_database,
    handoff_to_human,
    book_appointment,
    place_order,
    lookup_appointments,
    cancel_appointment,
    reschedule_appointment,
    cancel_order,
    get_typed_chat_details,
)
from backend.agent.checkpointer import get_checkpointer
from backend.agent.prompts import SYSTEM_PROMPT
from backend.agent.llm import get_chat_llm
from backend.agent.rag import retrieve_context
from backend.agent.parallel_tools import build_parallel_tool_node
from backend.config import settings
from backend.database import get_lead
from backend.tenant.registry import get_tenant_system_prompt, get_tenant_by_id

logger = logging.getLogger(__name__)

agent_tools = [
    search_crm,
    update_lead_status,
    schedule_demo,
    query_pos_database,
    handoff_to_human,
    book_appointment,
    place_order,
    lookup_appointments,
    cancel_appointment,
    reschedule_appointment,
    cancel_order,
    get_typed_chat_details,
]
tool_node = build_parallel_tool_node(agent_tools)


def _tenant_id_from_state(state: AgentState) -> str:
    return state.get("tenant_id") or settings.DEFAULT_TENANT_ID


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "human":
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            return str(content or "")
    return ""


def _heuristic_intent(text: str) -> str:
    return heuristic_intent(text)


async def router_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    if not messages:
        return {}

    thread_id = state.get("thread_id", "default_thread")
    tenant_id = _tenant_id_from_state(state)
    user_text = _last_user_text(messages)

    if thread_id.startswith("vapi_"):
        intent = "Inquiry"
    else:
        intent = _heuristic_intent(user_text)

    lead_doc = await get_lead(tenant_id, thread_id)
    lead_profile_dict = state.get("lead_profile") or {}
    if isinstance(lead_profile_dict, BaseMessage):
        lead_profile_dict = {}
    elif hasattr(lead_profile_dict, "model_dump"):
        lead_profile_dict = lead_profile_dict.model_dump()

    if lead_doc:
        lead_profile = LeadProfile(
            company=lead_doc.get("company", lead_profile_dict.get("company")),
            job_title=lead_doc.get("job_title", lead_profile_dict.get("job_title")),
            intent_score=lead_doc.get("intent_score", lead_profile_dict.get("intent_score", 0)),
            status=lead_doc.get("status", lead_profile_dict.get("status", "New")),
            fit=lead_doc.get("fit", lead_profile_dict.get("fit")),
        )
    else:
        lead_profile = LeadProfile(**{k: v for k, v in lead_profile_dict.items() if k in LeadProfile.model_fields})

    return {"intent": intent, "lead_profile": lead_profile}


async def sdr_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    lead_profile = state.get("lead_profile")
    tenant_id = _tenant_id_from_state(state)
    user_text = _last_user_text(messages)

    company = job_title = status = fit = "Unknown"
    intent_score = 0
    if lead_profile:
        company = lead_profile.company or "Unknown"
        job_title = lead_profile.job_title or "Unknown"
        intent_score = lead_profile.intent_score or 0
        status = lead_profile.status or "New"
        fit = str(lead_profile.fit) if lead_profile.fit is not None else "Unknown"

    ctx = await get_tenant_by_id(tenant_id)
    prompt_template = await get_tenant_system_prompt(tenant_id, SYSTEM_PROMPT)
    system_prompt = prompt_template.format(
        thread_id=state.get("thread_id", "unknown"),
        company=company,
        job_title=job_title,
        intent_score=intent_score,
        status=status,
        fit=fit,
    )

    if ctx and ctx.org_name and tenant_id != "alpha_default":
        system_prompt = (
            f"CRITICAL IDENTITY: You are the sales assistant for {ctx.org_name}. "
            f"Your company name is {ctx.org_name}. Never say you work for Alpha or sell SaaS packages "
            f"unless a tool explicitly returns that information.\n\n"
            + system_prompt
        )

    if ctx and ctx.settings.company_description:
        org = ctx.org_name or tenant_id
        system_prompt += f"\n\n--- ABOUT {org.upper()} ---\n{ctx.settings.company_description.strip()}"

    rag_snippets = await retrieve_context(tenant_id, user_text)
    if rag_snippets:
        system_prompt += f"\n\n--- RETRIEVED KNOWLEDGE (prefer for factual answers) ---\n{rag_snippets}"

    intent = state.get("intent") or "Inquiry"
    system_prompt += f"\n\nDetected intent: {intent}. Keep replies concise for low latency."

    formatted_messages = [SystemMessage(content=system_prompt)] + list(messages)

    llm = get_chat_llm(streaming=True, temperature=0.2)
    llm_with_tools = llm.bind_tools(agent_tools)

    gathered = None
    async for chunk in llm_with_tools.astream(formatted_messages):
        gathered = chunk if gathered is None else gathered + chunk

    if gathered is None:
        gathered = await llm_with_tools.ainvoke(formatted_messages)

    return {"messages": [gathered]}


async def post_tool_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    thread_id = state.get("thread_id", "default_thread")
    tenant_id = _tenant_id_from_state(state)

    requires_handoff = state.get("requires_handoff", False)
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) == "handoff_to_human":
            requires_handoff = True
            break

    lead_doc = await get_lead(tenant_id, thread_id)
    lead_profile = state.get("lead_profile")
    if lead_doc:
        lead_profile = LeadProfile(
            company=lead_doc.get("company"),
            job_title=lead_doc.get("job_title"),
            intent_score=lead_doc.get("intent_score", 0),
            status=lead_doc.get("status", "New"),
            fit=lead_doc.get("fit"),
        )

    return {"requires_handoff": requires_handoff, "lead_profile": lead_profile}


def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def route_after_post_tool(state: AgentState) -> Literal["sdr_agent", "__end__"]:
    return "sdr_agent"


builder = StateGraph(AgentState)
builder.add_node("router", router_node)
builder.add_node("sdr_agent", sdr_node)
builder.add_node("tools", tool_node)
builder.add_node("post_tool_processor", post_tool_node)

builder.add_edge(START, "router")
builder.add_edge("router", "sdr_agent")
builder.add_conditional_edges("sdr_agent", route_after_agent, {"tools": "tools", END: END})
builder.add_edge("tools", "post_tool_processor")
builder.add_conditional_edges("post_tool_processor", route_after_post_tool, {"sdr_agent": "sdr_agent", END: END})


async def get_agent_graph():
    checkpointer = await get_checkpointer()
    return builder.compile(checkpointer=checkpointer)
