"""Run multiple tool calls concurrently when the model requests them."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from backend.agent.state import AgentState

logger = logging.getLogger(__name__)


def build_parallel_tool_node(tools: List[BaseTool]):
    tools_by_name = {t.name: t for t in tools}

    async def parallel_tool_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
        messages = state.get("messages") or []
        if not messages:
            return {"messages": []}

        last = messages[-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        if not tool_calls:
            return {"messages": []}

        async def _run_one(tc: dict) -> ToolMessage:
            name = tc.get("name")
            tool = tools_by_name.get(name)
            args = tc.get("args") or {}
            tid = tc.get("id") or name
            if not tool:
                return ToolMessage(content=f"Unknown tool: {name}", tool_call_id=tid, name=name or "unknown")
            try:
                result = await tool.ainvoke(args, config=config)
                content = result if isinstance(result, str) else str(result)
            except ValidationError as ve:
                # R3: a schema-validation failure is the model's own mistake and
                # contains no secrets — only this tool's field names. Telling the
                # model exactly what is missing lets it recover on the next turn;
                # an opaque error id would make it retry blindly and loop.
                missing = []
                for err in ve.errors():
                    loc = ".".join(str(x) for x in err.get("loc", ()))
                    if loc:
                        missing.append(loc)
                fields = ", ".join(dict.fromkeys(missing)) or "some required details"
                logger.info("Tool %s called with invalid args: %s", name, fields)
                content = (
                    f"Cannot run {name} yet — still missing: {fields}. "
                    "Ask the caller for those, then call the tool again."
                )
            except Exception:
                # V07: NEVER surface raw exception text. SQLAlchemy/asyncpg errors
                # embed the full DSN including the tenant's database password, and
                # this string is fed back to the LLM and can be spoken to the caller.
                err_id = uuid.uuid4().hex[:8]
                logger.exception("Tool %s failed [ref=%s]", name, err_id)
                content = (
                    f"That lookup didn't complete (ref {err_id}). "
                    "Ask me to try again, or I can have a team member follow up."
                )
            return ToolMessage(content=content, tool_call_id=tid, name=name)

        results = await asyncio.gather(*[_run_one(tc) for tc in tool_calls])
        return {"messages": list(results)}

    return parallel_tool_node
