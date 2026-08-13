import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
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
from backend.agent.prompts import (SYSTEM_PROMPT, build_tenant_system_prompt,
                                   ensure_non_negotiables)
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


MAX_TOOL_ROUNDS = 3

# Tools whose return string is already caller-ready prose. After these run there
# is nothing for a second LLM pass to add, so we end the turn and speak the tool
# result directly — removes a full Gemini round-trip (~800-1500ms) per booking.
TERMINAL_TOOLS = {
    "book_appointment",
    "place_order",
    "cancel_appointment",
    "reschedule_appointment",
    "cancel_order",
    "handoff_to_human",
}


# Order matters: doubled braces are consumed first so `{{company}}` collapses to a
# literal `{company}` exactly as str.format would, instead of being substituted.
_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def safe_format_prompt(template: str, **values) -> str:
    """
    V03: substitute ONLY the placeholders we actually supply, and leave every
    other brace alone.

    Tenant system prompts are free text typed into the admin UI. str.format
    raises on a single stray '{' (a JSON example, "{price}", a code snippet, an
    unbalanced brace), and that exception used to escape sdr_node and surface to
    the caller as "Sorry, I hit a small snag" on every turn, permanently, for
    that tenant.

    A regex pass cannot raise, and — unlike a try/except around str.format — it
    still substitutes the real placeholders in a template that also happens to
    contain unbalanced braces.
    """
    if not template:
        return ""

    def _sub(match):
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        key = match.group(1)
        if key in values:
            return str(values[key])
        return token   # unknown placeholder: leave it visible, don't crash

    return _PLACEHOLDER_RE.sub(_sub, template)


def _tool_call_signatures(msg) -> set:
    sigs = set()
    for tc in (getattr(msg, "tool_calls", None) or []):
        try:
            args = json.dumps(tc.get("args", {}), sort_keys=True, default=str)
        except Exception:
            args = str(tc.get("args", {}))
        sigs.add((tc.get("name"), args))
    return sigs


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
    is_voice = thread_id.startswith("vapi_") or state.get("channel") == "voice"

    intent = "Inquiry" if is_voice else _heuristic_intent(user_text)

    # Voice: skip lead Mongo round-trip (saves ~100–400ms per turn)
    if is_voice:
        return {"intent": intent}

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


_VOICE_FAST_TOOLS = [
    handoff_to_human,
    book_appointment,
    place_order,
    get_typed_chat_details,
    lookup_appointments,
    cancel_appointment,
    reschedule_appointment,
    cancel_order,
]

_ACTION_KEYWORDS = (
    "book", "appointment", "schedule", "order", "buy", "purchase", "cancel",
    "reschedule", "handoff", "human", "representative", "email", "phone",
)

def _needs_tools(
    user_text: str,
    has_fact_cache: bool,
    has_catalog_cache: bool = False,
    inventory_intent: bool = False,
) -> bool:
    text = (user_text or "").lower()
    if any(k in text for k in _ACTION_KEYWORDS):
        return True
    # Tenant-mapped inventory: tools unless their SQL catalog is already warm
    if inventory_intent:
        return not has_catalog_cache
    if has_fact_cache:
        return False
    return True


# P03: sdr_node runs at least twice per tool-using turn, and the second pass
# rebuilt the entire prompt — tenant lookup, mappings, knowledge, catalog
# selection and RAG — for a result that is byte-identical to the first. The
# built prompt deliberately does NOT live in graph state (R7: the chat graph is
# checkpointed to Mongo and the prompt carries the full catalog + knowledge
# blocks), so it is memoised here instead.
#
# The key includes the lead profile because post_tool_node can legitimately
# refresh it mid-turn on the chat path, which changes the "Lead Profile:" line.
_PROMPT_MEMO: "OrderedDict[tuple, tuple[float, tuple]]" = OrderedDict()
_PROMPT_MEMO_TTL = 30.0        # only needs to survive one turn
_PROMPT_MEMO_MAX = 256


def invalidate_prompt_memo() -> None:
    _PROMPT_MEMO.clear()


async def _build_turn_context(
    state: AgentState,
    tenant_id: str,
    thread_id: str,
    user_text: str,
    is_voice: bool,
    lead_fields: tuple,
):
    """Assemble the system prompt and the routing flags for this turn."""
    company, job_title, intent_score, status, fit = lead_fields

    key = (tenant_id, thread_id, user_text, lead_fields, is_voice)
    hit = _PROMPT_MEMO.get(key)
    if hit and time.monotonic() <= hit[0]:
        _PROMPT_MEMO.move_to_end(key)
        return hit[1]
    _PROMPT_MEMO.pop(key, None)

    ctx = await get_tenant_by_id(tenant_id)

    # T04: only the demo tenant may fall back to the Alpha demo prompt. Everyone
    # else gets a neutral, org-specific template with no invented catalogue.
    if tenant_id == settings.DEFAULT_TENANT_ID:
        fallback_prompt = SYSTEM_PROMPT
    else:
        # ctx may be None (tenant row missing/inactive) or a partially populated
        # context, so every hop is guarded — this runs on the voice hot path and
        # an AttributeError here would surface as "Sorry, I hit a small snag".
        _settings = getattr(ctx, "settings", None)
        fallback_prompt = build_tenant_system_prompt(
            getattr(ctx, "org_name", None) or tenant_id,
            getattr(_settings, "company_description", None) or "",
        )
    prompt_template = await get_tenant_system_prompt(tenant_id, fallback_prompt)
    # A tenant can replace the entire system prompt from the admin UI, which used
    # to drop every shared rule with it.
    prompt_template = ensure_non_negotiables(prompt_template)

    system_prompt = safe_format_prompt(
        prompt_template,
        thread_id=thread_id,
        company=company,
        job_title=job_title,
        intent_score=intent_score,
        status=status,
        fit=fit,
    )

    if ctx and ctx.org_name and tenant_id != "alpha_default":
        system_prompt = (
            f"CRITICAL IDENTITY: You are the sales assistant for {ctx.org_name}. "
            f"Your company name is {ctx.org_name}. Do not invent other company names. "
            f"Answer services/packages from CACHED KNOWLEDGE when present.\n\n"
            + system_prompt
        )

    from backend.integrations.catalog_cache import (get_cached_catalog,
                                                    get_catalog_sections,
                                                    schedule_warmup,
                                                    select_catalog_section)
    from backend.integrations.knowledge_cache import get_cached_knowledge, warmup_knowledge
    from backend.integrations.tenant_inventory import (
        format_mapped_entities_for_prompt,
        is_inventory_question_for_tenant,
        load_inventory_mappings,
    )

    # P08: these two are independent and were awaited back to back.
    inventory_intent, mapped = await asyncio.gather(
        is_inventory_question_for_tenant(tenant_id, user_text),
        load_inventory_mappings(tenant_id),
    )
    mapped_hint = format_mapped_entities_for_prompt(mapped)
    if mapped_hint:
        system_prompt += f"\n\n--- TENANT DATA MODEL ---\n{mapped_hint}"

    knowledge = get_cached_knowledge(tenant_id)
    if not knowledge and is_voice and not inventory_intent:
        try:
            await asyncio.wait_for(warmup_knowledge(tenant_id), timeout=1.5)
            knowledge = get_cached_knowledge(tenant_id)
        except Exception:
            pass

    if knowledge and not inventory_intent:
        system_prompt += (
            "\n\n--- CACHED KNOWLEDGE (company FAQ — not a substitute for live tables) ---\n"
            + knowledge
        )

    # Inject the matching section only. Handing the model every category at once
    # is why a services question came back with product names.
    catalog_section = select_catalog_section(tenant_id, user_text)
    catalog = get_cached_catalog(tenant_id, section=catalog_section) if catalog_section else None
    if not catalog:
        catalog = get_cached_catalog(tenant_id)
        catalog_section = None

    if catalog:
        if catalog_section:
            others = [s for s in get_catalog_sections(tenant_id)
                      if s not in (catalog_section, "all")]
            header = (
                f"\n\n--- CACHED CATALOG · {catalog_section.upper()} "
                "(this tenant's approved SQL tables — prefer over tools) ---\n"
            )
            system_prompt += header + catalog
            if others:
                system_prompt += (
                    f"\n\nEvery row above is a {catalog_section}. This tenant also has separate "
                    f"{', '.join(others)} — those are DIFFERENT offerings. Never answer a "
                    f"{catalog_section} question with items from them; call query_pos_database "
                    "if the caller asks about another category."
                )
            system_prompt += (
                "\n\nWhen listing what we offer, name EVERY item in the section above rather "
                "than a sample of them, and group them by their category/type value when the "
                "rows carry one. The text in square brackets is an internal table label — never "
                "read it back to the caller or use it as an item name."
            )
        else:
            system_prompt += (
                "\n\n--- CACHED CATALOG (this tenant's approved SQL tables — prefer over tools) ---\n"
                + catalog
            )
    elif not is_voice:
        schedule_warmup(tenant_id)

    has_facts = bool((knowledge and not inventory_intent) or catalog)
    if (not has_facts) or (inventory_intent and not catalog):
        if not catalog or not knowledge:
            rag_snippets = await retrieve_context(tenant_id, user_text)
            if rag_snippets:
                system_prompt += f"\n\n--- RETRIEVED KNOWLEDGE (prefer for factual answers) ---\n{rag_snippets}"
                has_facts = True

    result = (system_prompt, inventory_intent, has_facts, catalog)
    _PROMPT_MEMO[key] = (time.monotonic() + _PROMPT_MEMO_TTL, result)
    _PROMPT_MEMO.move_to_end(key)
    while len(_PROMPT_MEMO) > _PROMPT_MEMO_MAX:
        _PROMPT_MEMO.popitem(last=False)
    return result


def _has_output(message) -> bool:
    """A usable model turn: some text, or at least one tool call."""
    if message is None:
        return False
    content = getattr(message, "content", None)
    if isinstance(content, list):
        if content:
            return True
    elif content and str(content).strip():
        return True
    return bool(getattr(message, "tool_calls", None))


def _log_empty_response(message, thread_id: str, tenant_id: str) -> None:
    """
    Say WHY the model produced nothing.

    Nothing was logged here before, so "the agent went blank" had no evidence
    behind it at all. finish_reason and safety ratings distinguish a safety
    block from a token limit from a malformed history — three different fixes.
    """
    meta = {}
    for attr in ("response_metadata", "additional_kwargs", "usage_metadata"):
        value = getattr(message, attr, None)
        if isinstance(value, dict) and value:
            meta[attr] = {
                k: value[k] for k in (
                    "finish_reason", "safety_ratings", "prompt_feedback",
                    "block_reason", "candidates", "output_tokens",
                ) if k in value
            } or value
    logger.error(
        "EMPTY_MODEL_RESPONSE tenant=%s thread=%s message=%r meta=%s",
        tenant_id, thread_id, type(message).__name__, meta or "<none>",
    )


def _sanitize_history(formatted_messages: list):
    """
    Repair the two history shapes Gemini rejects by returning nothing:
    a conversation that opens on an assistant turn, and consecutive same-role
    turns. Returns None when there is nothing to repair, so the caller does not
    pay for a pointless second call.

    The leading SystemMessage is preserved as-is.
    """
    if not formatted_messages:
        return None

    head, rest = [], list(formatted_messages)
    if rest and getattr(rest[0], "type", None) == "system":
        head, rest = [rest[0]], rest[1:]

    cleaned = []
    for msg in rest:
        content = getattr(msg, "content", None)
        has_tools = bool(getattr(msg, "tool_calls", None))
        if not has_tools and not (content and str(content).strip()):
            continue  # empty turn: nothing for the model to condition on
        if cleaned and getattr(cleaned[-1], "type", None) == getattr(msg, "type", None):
            # Two turns from the same speaker in a row. Keep the newer one.
            if getattr(msg, "type", None) == "human":
                cleaned[-1] = msg
                continue
            cleaned[-1] = msg
            continue
        cleaned.append(msg)

    # A conversation must open on the human side.
    while cleaned and getattr(cleaned[0], "type", None) != "human":
        cleaned.pop(0)

    if not cleaned:
        return None
    repaired = head + cleaned
    return repaired if len(repaired) != len(formatted_messages) else None


def _recovery_prompt(messages: list) -> str:
    """
    A recovery line that does not destroy an in-progress booking.

    The old fallback was "I'm here to help! Could you repeat that?" — an opener.
    Mid-booking that reads as the agent having forgotten everything, which is
    exactly what was reported. If the last thing WE asked for is recoverable
    from the history, ask for that one thing again.
    """
    ASKS = (
        ("email", "Could you repeat your email address for me?"),
        ("phone", "Could you repeat your phone number?"),
        ("number", "Could you repeat your phone number?"),
        ("date", "What date works best for you?"),
        ("time", "What time suits you?"),
        ("name", "Could you tell me your name again?"),
    )
    for msg in reversed(list(messages or [])):
        if getattr(msg, "type", None) != "ai":
            continue
        text = str(getattr(msg, "content", "") or "").lower()
        if not text:
            continue
        for needle, question in ASKS:
            if needle in text:
                return question
        break
    return "Sorry — could you say that last part again?"


async def sdr_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    lead_profile = state.get("lead_profile")
    tenant_id = _tenant_id_from_state(state)
    user_text = _last_user_text(messages)
    thread_id = state.get("thread_id", "unknown")
    is_voice = str(thread_id).startswith("vapi_") or state.get("channel") == "voice"

    company = job_title = status = fit = "Unknown"
    intent_score = 0
    if lead_profile:
        company = lead_profile.company or "Unknown"
        job_title = lead_profile.job_title or "Unknown"
        intent_score = lead_profile.intent_score or 0
        status = lead_profile.status or "New"
        fit = str(lead_profile.fit) if lead_profile.fit is not None else "Unknown"

    system_prompt, inventory_intent, has_facts, catalog = await _build_turn_context(
        state, tenant_id, thread_id, user_text, is_voice,
        (company, job_title, intent_score, status, fit),
    )

    intent = state.get("intent") or "Inquiry"
    system_prompt += f"\n\nDetected intent: {intent}. Keep replies concise for low latency."
    if is_voice:
        system_prompt += (
            " Voice channel: ONE short spoken sentence (max ~25 words) unless they ask for a list — "
            "then at most 2 short sentences. Handle interruptions gracefully."
        )
        if catalog and inventory_intent:
            system_prompt += (
                " Answer from CACHED CATALOG using this tenant's entity names/labels — "
                "do not invent a generic products/services script."
            )

    # V02: history is NEVER truncated. The voice graph has no checkpointer, so the
    # inbound message list (built from Vapi's own payload) is the only memory the
    # agent has. Dropping it made multi-turn collection impossible and caused the
    # model to re-invoke the same tool every turn.
    formatted_messages = [SystemMessage(content=system_prompt)] + list(messages)

    llm = get_chat_llm(
        streaming=True,
        temperature=0.1 if is_voice else 0.2,
        max_retries=1 if is_voice else 2,
    )
    # R5: once the tool budget is spent, force a final answer. Ending the turn
    # here instead would leave a raw tool result as the last message — which the
    # voice path either cannot speak (data tools are not in _SPEAKABLE_TOOLS) or
    # would read out verbatim as a SQL dump.
    budget_spent = (state.get("tool_rounds") or 0) >= MAX_TOOL_ROUNDS
    if budget_spent:
        system_prompt += (
            "\n\nYou already have the tool results you need. Answer the caller now "
            "in one or two short sentences using ONLY those results. Do not call any more tools."
        )

    use_tools = (not budget_spent) and _needs_tools(
        user_text,
        has_facts,
        has_catalog_cache=bool(catalog),
        inventory_intent=inventory_intent,
    )
    if use_tools:
        if is_voice and inventory_intent:
            tools = list(dict.fromkeys([*_VOICE_FAST_TOOLS, query_pos_database]))
        elif is_voice and has_facts:
            tools = _VOICE_FAST_TOOLS
        else:
            tools = agent_tools
        bound = llm.bind_tools(tools)
    else:
        bound = llm

    gathered = None
    try:
        async for chunk in bound.astream(formatted_messages):
            gathered = chunk if gathered is None else gathered + chunk
    except Exception as e:
        logger.error("LLM stream error in sdr_node: %s", e, exc_info=True)

    if gathered is None:
        try:
            gathered = await bound.ainvoke(formatted_messages)
        except Exception as e:
            logger.error("LLM invoke fallback error in sdr_node: %s", e, exc_info=True)

    # An empty model response used to be swallowed into a canned line with
    # nothing logged, which made it undiagnosable — and because voice memory is
    # Vapi's replayed history, that canned line came back as context on the next
    # turn and the agent said it again, and again.
    #
    # Gemini returns empty for a small number of concrete reasons, and the most
    # common ones are repairable history problems: a conversation that opens on
    # an assistant turn, or two same-role turns in a row. Try once more on a
    # repaired history before giving up.
    if not _has_output(gathered):
        _log_empty_response(gathered, thread_id, tenant_id)
        repaired = _sanitize_history(formatted_messages)
        if repaired is not None:
            logger.info(
                "Empty model response — retrying on a repaired history (%d -> %d messages)",
                len(formatted_messages), len(repaired),
            )
            try:
                gathered = await bound.ainvoke(repaired)
            except Exception as e:
                logger.error("Retry after empty response failed: %s", e, exc_info=True)

    if not _has_output(gathered):
        from langchain_core.messages import AIMessage

        # Deliberately NOT "I'm here to help" — a booking in progress must not be
        # answered with an opener that throws away what was already collected.
        # Re-ask for the outstanding detail instead so the flow can continue.
        gathered = AIMessage(content=_recovery_prompt(messages))

    # R7: the built prompt is deliberately NOT returned into state. The chat graph
    # is checkpointed to Mongo, and the prompt carries the full catalog + knowledge
    # blocks — persisting it would balloon every checkpoint write. P03's reuse is
    # handled with a request-scoped memo instead.
    return {"messages": [gathered]}


async def post_tool_node(state: AgentState) -> Dict[str, Any]:
    messages = state.get("messages", [])
    thread_id = state.get("thread_id", "default_thread")
    tenant_id = _tenant_id_from_state(state)
    is_voice = str(thread_id).startswith("vapi_") or state.get("channel") == "voice"

    requires_handoff = state.get("requires_handoff", False)
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) == "handoff_to_human":
            requires_handoff = True
            break

    out: Dict[str, Any] = {
        "requires_handoff": requires_handoff,
        # V04: every trip through here is one agent<->tools round.
        "tool_rounds": (state.get("tool_rounds") or 0) + 1,
    }

    # P06: router_node already skips this Mongo read on voice to save 100-400ms;
    # doing it here on every tool round undid that saving.
    if is_voice:
        return out

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
    out["lead_profile"] = lead_profile
    return out


def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    sig = _tool_call_signatures(last_message)
    if not sig:
        return END

    # V04: hard budget, independent of LangGraph's recursion_limit.
    if (state.get("tool_rounds") or 0) >= MAX_TOOL_ROUNDS:
        logger.warning("Tool-round budget exhausted (%s) — ending turn", MAX_TOOL_ROUNDS)
        return END

    # V05: the model re-requesting a call it already made means the tool result
    # did not satisfy it (e.g. book_appointment returning "I still need ...").
    # Looping again cannot help and used to run into GraphRecursionError, which
    # surfaced to the caller as the generic "snag" message.
    prior = set()
    for m in messages[:-1]:
        prior |= _tool_call_signatures(m)
    repeated = sig & prior
    if repeated:
        logger.warning("Repeated tool call %s — ending turn instead of looping", repeated)
        return END

    return "tools"


def route_after_post_tool(state: AgentState) -> Literal["sdr_agent", "__end__"]:
    messages = state.get("messages", [])
    thread_id = state.get("thread_id", "")
    is_voice = str(thread_id).startswith("vapi_") or state.get("channel") == "voice"

    # R6: P14 is VOICE-ONLY. The voice endpoint reads the final message via
    # _extract_assistant_text and can speak a ToolMessage directly. The dashboard
    # WebSocket streams on_chat_model_stream tokens instead, so ending on a
    # ToolMessage would leave the chat UI with nothing to render and nothing to
    # persist to the transcript.
    if not is_voice:
        return "sdr_agent"

    # P14: terminal tools already return caller-ready prose. Looping back for a
    # paraphrase costs a whole extra LLM round-trip on the voice critical path.
    tool_names = []
    for msg in reversed(messages):
        if getattr(msg, "type", None) != "tool":
            break
        tool_names.append(getattr(msg, "name", None))
    if tool_names and all(n in TERMINAL_TOOLS for n in tool_names):
        return END

    # R5: when the budget is spent we still go back to the agent — sdr_node
    # unbinds the tools, so it is forced to answer from the results it already
    # has instead of dead-ending on a raw tool dump.
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


_compiled_graph = None
_voice_graph = None


async def get_agent_graph():
    """Dashboard/chat graph with Mongo checkpointer (multi-turn state)."""
    global _compiled_graph
    if _compiled_graph is None:
        checkpointer = await get_checkpointer()
        _compiled_graph = builder.compile(checkpointer=checkpointer)
    return _compiled_graph


async def get_voice_agent_graph():
    """
    Voice graph WITHOUT Mongo checkpointer.
    Checkpoint I/O on every turn was a major cause of Vapi timeouts / dropped calls.
    Conversation history is still persisted via save_conversation_message.
    """
    global _voice_graph
    if _voice_graph is None:
        _voice_graph = builder.compile()
    return _voice_graph
