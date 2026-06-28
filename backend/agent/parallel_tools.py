"""Run multiple tool calls concurrently when the model requests them."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

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
            except Exception as e:
                logger.exception("Tool %s failed", name)
                content = f"Tool error: {e}"
            return ToolMessage(content=content, tool_call_id=tid, name=name)

        results = await asyncio.gather(*[_run_one(tc) for tc in tool_calls])
        return {"messages": list(results)}

    return parallel_tool_node
