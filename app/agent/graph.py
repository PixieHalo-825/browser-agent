"""LangGraph state-machine assembly.

Topology (ReAct-style layered state machine)::

                    ┌─────────────┐
                    │   planner   │◄──────────────┐
                    └──────┬──────┘               │ replan
                           ▼                      │
                    ┌─────────────┐  tool_calls   │
              ┌────►│  executor   │──────────┐    │
              │     └──────┬──────┘          ▼    │
        results      plain text │           ┌─────────┐
              │     ┌──────────┘           │  tools  │
              │     ▼                      └────┬────┘
              │ ┌─────────────┐  terminate       │ results
              └─│  reviewer   │──────────────────┼──► END
                └──────┬──────┘                  │
                       │ finish                  ▼
                       ▼                    (loop back)
                  ┌─────────┐
                  │ finish  │──► END
                  └─────────┘

Reviewer routes: continue → next step (executor), replan → planner, finish → END.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.executor import executor_node, route_after_executor, route_after_tools, tool_node
from app.agent.planner import planner_node
from app.agent.reviewer import reviewer_node, route_after_reviewer
from app.agent.state import AgentState
from app.browser.browser import get_browser
from app.config import get_config
from app.mcp.registry import ToolRegistry, get_registry
from app.sandbox.manager import shutdown_sandbox
from app.tools import (
    BrowserClick,
    BrowserExtract,
    BrowserNavigate,
    BrowserScreenshot,
    BrowserScroll,
    BrowserType,
    PythonExecute,
    ReadFile,
    Terminate,
    WebSearch,
    WriteFile,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], Awaitable[None]]


def build_agent_graph():
    """Compile the planner/executor/reviewer state machine."""
    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("tools", tool_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("finish", finish_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_conditional_edges(
        "executor",
        route_after_executor,
        {"tools": "tools", "reviewer": "reviewer", END: END},
    )
    graph.add_conditional_edges("tools", route_after_tools, {"executor": "executor", END: END})
    graph.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {"executor": "executor", "planner": "planner", "finish": "finish"},
    )
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=MemorySaver())


async def finish_node(state: AgentState) -> dict:
    """Last stop: make sure a final answer exists."""
    final = state.get("final_answer", "")
    if not final:
        for message in reversed(state.get("messages", [])):
            if message.get("role") == "assistant" and message.get("content"):
                final = message["content"]
                break
    return {
        "final_answer": final or "任务结束（未产生最终答案）",
        "step_notes": ["[Finish] 任务结束"],
    }


async def setup_registry() -> ToolRegistry:
    """Register local tools (once) and connect configured MCP servers.

    MCP connections are torn down after each run (see ``_cleanup``), so they
    must be re-established on every run — local tool registration is skipped
    when already present.
    """
    registry = get_registry()
    if not registry.all_names:
        registry.register_many(
            [
                BrowserNavigate(),
                BrowserClick(),
                BrowserType(),
                BrowserScroll(),
                BrowserScreenshot(),
                BrowserExtract(),
                WebSearch(),
                ReadFile(),
                WriteFile(),
                PythonExecute(),
                Terminate(),
            ]
        )
    if not registry.mcp_connected:
        connected = await registry.connect_mcp_servers()
        if connected:
            logger.info("MCP servers connected: %s", connected)
    return registry


async def run_agent(
    task: str,
    on_event: Optional[EventCallback] = None,
    max_super_steps: int = 60,
) -> dict:
    """Run one task through the full state machine.

    Returns ``{"final_answer": str, "state": dict}``. The graph is stepped
    with a super-step guard so a pathological loop cannot run forever.
    """
    graph = build_agent_graph()
    thread = {"configurable": {"thread_id": uuid.uuid4().hex}}
    initial: dict[str, Any] = {"task": task, "messages": [{"role": "user", "content": task}]}

    await setup_registry()

    final_state: dict[str, Any] = initial
    try:
        async for chunk in _bounded_stream(graph, initial, thread, max_super_steps):
            if on_event is not None and isinstance(chunk, dict):
                for node, update in chunk.items():
                    if update:
                        try:
                            await on_event(node, update)
                        except Exception as exc:  # a UI callback must never kill the run
                            logger.debug("event callback error: %s", exc)
        snapshot = graph.get_state(thread)
        final_state = snapshot.values if snapshot else final_state
    finally:
        await _cleanup()

    return {
        "final_answer": final_state.get("final_answer", ""),
        "state": final_state,
    }


async def _bounded_stream(graph, initial: dict, thread: dict, max_steps: int):
    """Stream graph updates with a hard super-step cap."""
    steps = 0
    async for chunk in graph.astream(initial, config=thread, stream_mode="updates"):
        steps += 1
        if steps > max_steps:
            logger.error("Super-step cap (%d) exceeded; aborting run", max_steps)
            break
        yield chunk


async def _cleanup() -> None:
    """Tear down browser, MCP connections and sandbox."""
    try:
        await get_browser().stop()
    except Exception as exc:
        logger.debug("browser cleanup: %s", exc)
    try:
        await get_registry().shutdown()
    except Exception as exc:
        logger.debug("registry cleanup: %s", exc)
    try:
        await shutdown_sandbox()
    except Exception as exc:
        logger.debug("sandbox cleanup: %s", exc)
