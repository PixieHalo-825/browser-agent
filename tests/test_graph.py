"""State-machine routing tests (pure functions, no LLM/browser needed)."""

from __future__ import annotations

from app.agent.executor import route_after_executor, route_after_tools
from app.agent.reviewer import route_after_reviewer


def _assistant(tool_calls=None, content="done"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def test_executor_routes_to_tools_when_tool_calls_pending():
    state = {"messages": [_assistant(tool_calls=[{"id": "1", "function": {"name": "web_search"}}])]}
    assert route_after_executor(state) == "tools"


def test_executor_routes_to_reviewer_on_plain_text():
    state = {"messages": [_assistant()]}
    assert route_after_executor(state) == "reviewer"


def test_executor_routes_to_end_when_terminated():
    state = {"terminated": True, "messages": [_assistant(tool_calls=[])]}
    assert route_after_executor(state) == "__end__"


def test_tools_loop_back_to_executor():
    assert route_after_tools({"terminated": False}) == "executor"


def test_tools_short_circuit_on_terminate():
    assert route_after_tools({"terminated": True}) == "__end__"


def test_reviewer_routing():
    assert route_after_reviewer({"verdict": "continue"}) == "executor"
    assert route_after_reviewer({"verdict": "replan"}) == "planner"
    assert route_after_reviewer({"verdict": "finish"}) == "finish"
    assert route_after_reviewer({}) == "finish"  # safe default
