"""
Shared harness for the audit regression suite.

Runs the REAL compiled LangGraph with a scripted LLM and stubbed I/O, so these
tests need no MongoDB, no Gemini key and no network.

    pytest backend/tests/audit -q
"""
import json
import pytest
from langchain_core.messages import AIMessageChunk

import backend.agent.graph as G
import backend.integrations.catalog_cache as CC
import backend.integrations.knowledge_cache as KC
import backend.integrations.tenant_inventory as TI


class ScriptedLLM:
    """Duck-typed stand-in for ChatGoogleGenerativeAI.

    Each script entry is either a string (plain reply) or a list of
    (tool_name, args) tuples (a tool-calling turn).
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []        # message lists the model was invoked with
        self.bound_tools = []  # tool names bound on each invocation

    def bind_tools(self, tools):
        self.bound_tools.append([getattr(t, "name", str(t)) for t in tools])
        return self

    def _next(self, messages):
        self.calls.append(list(messages))
        step = self.script.pop(0) if self.script else "Anything else I can help with?"
        if isinstance(step, str):
            return AIMessageChunk(content=step)
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": n, "args": json.dumps(a), "id": f"call_{i}", "index": i}
                for i, (n, a) in enumerate(step)
            ],
        )

    async def astream(self, messages, **kw):
        yield self._next(messages)

    async def ainvoke(self, messages, **kw):
        return self._next(messages)


class Harness:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.llm = None

    def install(self, *, script=(), org="Acme Dental",
                prompt="You are the assistant for Acme. Thread {thread_id}. Lead {company}.",
                catalog=None, knowledge=None, mapped=None, tool_results=None,
                inventory_intent=False, tool_overrides=None):
        self.llm = ScriptedLLM(script)

        class Ctx:
            org_name = org
            tenant_id = "acme_1234"

        async def _tenant(tid):
            return Ctx()

        async def _prompt(tid, fallback):
            return prompt

        async def _lead(tid, thread):
            return None

        async def _rag(tid, q, limit=4):
            return ""

        async def _warm(tid, force=False):
            return {"ok": True}

        async def _inv(tid, text):
            return inventory_intent

        async def _mappings(tid):
            return mapped or []

        for owner, name, new in [
            (G, "get_tenant_by_id", _tenant),
            (G, "get_tenant_system_prompt", _prompt),
            (G, "get_lead", _lead),
            (G, "retrieve_context", _rag),
            (G, "get_chat_llm", lambda **kw: self.llm),
            (CC, "get_cached_catalog", lambda tid: catalog),
            (CC, "schedule_warmup", lambda tid: None),
            (KC, "get_cached_knowledge", lambda tid: knowledge),
            (KC, "warmup_knowledge", _warm),
            (TI, "is_inventory_question_for_tenant", _inv),
            (TI, "load_inventory_mappings", _mappings),
            (TI, "format_mapped_entities_for_prompt", lambda m: ""),
        ]:
            self.mp.setattr(owner, name, new)

        results = tool_results or {}
        overrides = tool_overrides or {}
        for t in G.agent_tools:
            if t.name in overrides:
                self.mp.setattr(t, "coroutine", overrides[t.name])
                continue

            async def _run(*a, __n=t.name, **kw):
                return results.get(__n, f"[{__n} ok]")

            self.mp.setattr(t, "coroutine", _run)
            self.mp.setattr(t, "func", None)
        return self.llm

    async def run(self, messages, *, channel="voice", thread="vapi_call1", tenant="acme_1234"):
        state = {"messages": messages, "thread_id": thread, "tenant_id": tenant}
        if channel:
            state["channel"] = channel
        graph = G.builder.compile()
        return await graph.ainvoke(
            state,
            config={"configurable": {"thread_id": thread, "tenant_id": tenant},
                    "recursion_limit": 12},
        )


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)
