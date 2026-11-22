"""Executor node: ReAct tool-calling loop for the current plan step.

The LangGraph edges give the loop shape: ``executor → tools → executor`` while
tool calls keep coming, ``executor → reviewer`` when the model answers with
plain text (or hits the iteration cap), and ``tools → END`` on terminate.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.planner import plan_status_text
from app.agent.state import AgentState
from app.config import get_config
from app.llm.llm import get_llm
from app.mcp.registry import get_registry
from app.prompt import executor as prompts

logger = logging.getLogger(__name__)

# Keep the ReAct history bounded: first messages carry the task, recent ones
# carry the working context.
_MAX_HISTORY = 60


def _assistant_to_dict(message: Any) -> dict:
    """Serialise an OpenAI assistant message (incl. tool_calls) to a dict."""
    tool_calls = []
    for call in message.tool_calls or []:
        tool_calls.append(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
        )
    return {
        "role": "assistant",
        "content": message.content or "",
        **({"tool_calls": tool_calls} if tool_calls else {}),
    }


def _trim(messages: list[dict]) -> list[dict]:
    if len(messages) <= _MAX_HISTORY:
        return messages
    head, tail = messages[:4], messages[-(_MAX_HISTORY - 4):]
    return head + [{"role": "user", "content": "(…早期轨迹已省略…)"}] + tail


def _executor_system(state: AgentState) -> str:
    plan = state.get("plan", [])
    idx = state.get("current_step", 0)
    step = plan[idx] if idx < len(plan) else {"step_index": idx + 1, "description": "(无步骤)"}
    done = [s for s in plan[:idx] if s.get("status") == "done"]
    done_summary = "\n".join(
        f"- 步骤{s['step_index']} {s['description']}: {s.get('result', '')[:150]}" for s in done
    ) or "（尚无）"
    return prompts.EXECUTOR_SYSTEM.format(
        task=state["task"],
        plan_display=plan_status_text(plan),
        current_no=idx + 1,
        total_steps=len(plan),
        current_step=f"{step['description']}",
        done_summary=done_summary,
    )


async def executor_node(state: AgentState) -> dict:
    cfg = get_config().agent
    registry = get_registry()
    llm = get_llm()

    iteration = state.get("react_iteration", 0)
    idx = state.get("current_step", 0)
    plan = state.get("plan", [])
    step = plan[idx] if idx < len(plan) else {"description": "(无步骤)", "step_index": idx + 1}

    new_messages: list[dict] = []

    # Kick off a fresh step with an explicit instruction message.
    if iteration == 0:
        new_messages.append(
            {"role": "user", "content": f"现在执行步骤 {step['step_index']}：{step['description']}"}
        )
        if state.get("verdict_reason"):
            new_messages.append(
                {"role": "user", "content": f"（Reviewer 提示：{state['verdict_reason']}）"}
            )
        updated_plan = [dict(s) for s in plan]
        if idx < len(updated_plan):
            updated_plan[idx]["status"] = "in_progress"
        plan = updated_plan

    # Iteration cap: force the step to review without another LLM call.
    if iteration >= cfg.react_max_iterations:
        logger.warning("[Executor] step %d hit iteration cap (%d)", idx + 1, cfg.react_max_iterations)
        new_messages.append(
            {"role": "user", "content": "（系统提示：本步骤已达到最大工具调用轮数，强制结束本步骤，交由 Reviewer 审查）"}
        )
        return {"messages": new_messages, "plan": plan}

    messages = [{"role": "system", "content": _executor_system(state)}] + _trim(
        state.get("messages", [])
    )
    tools = registry.openai_schemas()
    response = await llm.ask(messages, tools=tools)
    choice = response.choices[0].message
    assistant = _assistant_to_dict(choice)
    logger.debug("[Executor] response: %s", assistant)

    if assistant.get("tool_calls"):
        names = ", ".join(t["function"]["name"] for t in assistant["tool_calls"])
        logger.info("[Executor] 调用工具: %s", names)

    return {
        "messages": new_messages + [assistant],
        "react_iteration": iteration + 1,
        "plan": plan,
    }


async def tool_node(state: AgentState) -> dict:
    """Execute every tool call from the last assistant message."""
    registry = get_registry()
    last = state["messages"][-1]
    tool_messages: list[dict] = []
    terminated = state.get("terminated", False)
    final_answer = state.get("final_answer", "")

    for call in last.get("tool_calls", []):
        name = call["function"]["name"]
        raw_args = call["function"]["arguments"]
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            arguments = {}
            output = f"ERROR: 参数不是合法 JSON: {raw_args[:200]}"
        else:
            logger.info("[Tools] %s(%s)", name, str(arguments)[:160])
            output = await registry.execute(name, arguments)
        logger.info("[Tools] -> %s", output[:200].replace("\n", " "))
        tool_messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": output[:8000]}
        )
        if name == "terminate":
            terminated = True
            final_answer = output

    update: dict = {"messages": tool_messages, "terminated": terminated}
    if final_answer:
        update["final_answer"] = final_answer
    return update


def route_after_executor(state: AgentState) -> str:
    """tools while tool calls are pending, reviewer otherwise."""
    if state.get("terminated"):
        return "__end__"
    last = state["messages"][-1] if state.get("messages") else {}
    if last.get("role") == "assistant" and last.get("tool_calls"):
        return "tools"
    return "reviewer"


def route_after_tools(state: AgentState) -> str:
    if state.get("terminated"):
        return "__end__"
    return "executor"
