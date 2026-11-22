"""Reviewer node: judge the current step and route the state machine."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.planner import plan_status_text
from app.agent.state import AgentState
from app.config import get_config
from app.llm.llm import get_llm
from app.prompt import reviewer as prompts

logger = logging.getLogger(__name__)

_MAX_TRACE_MESSAGES = 12


def _executor_summary(state: AgentState) -> str:
    """Last assistant text message (the step conclusion)."""
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "assistant" and message.get("content"):
            return message["content"][:1500]
    return "（Executor 未给出文字总结）"


def _recent_trace(state: AgentState) -> str:
    """Compact tool-call trace of the current step."""
    lines = []
    for message in state.get("messages", [])[-_MAX_TRACE_MESSAGES:]:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                args = call["function"]["arguments"][:80]
                lines.append(f"→ {call['function']['name']}({args})")
        elif role == "tool":
            content = (message.get("content") or "").replace("\n", " ")[:120]
            lines.append(f"   ↳ {content}")
    return "\n".join(lines) or "（无工具调用）"


async def reviewer_node(state: AgentState) -> dict:
    cfg = get_config().agent
    llm = get_llm()

    idx = state.get("current_step", 0)
    plan = [dict(s) for s in state.get("plan", [])]
    replans = state.get("replans", 0)
    terminated = state.get("terminated", False)

    verdict = "finish"
    reason = ""
    final_answer = state.get("final_answer", "")

    if terminated:
        # Executor called terminate; just close out.
        verdict, reason = "finish", "executor 调用了 terminate"
    else:
        messages = [
            {
                "role": "system",
                "content": prompts.REVIEWER_SYSTEM.format(
                    task=state["task"],
                    plan_status=plan_status_text(plan),
                    executor_summary=_executor_summary(state),
                    recent_trace=_recent_trace(state),
                    replans=replans,
                    max_replans=cfg.max_replans,
                ),
            },
            {"role": "user", "content": "请审查当前步骤执行结果并给出 verdict。"},
        ]
        try:
            result = await llm.ask_json(messages)
            verdict = str(result.get("verdict", "continue")).lower()
            reason = str(result.get("reason", ""))[:300]
            if verdict == "finish":
                final_answer = final_answer or str(result.get("final_answer", ""))
        except Exception as exc:
            logger.warning("[Reviewer] structured output failed (%s); defaulting to continue", exc)
            verdict, reason = "continue", f"审查失败，默认继续: {exc}"

        # Guard rails.
        if verdict not in ("continue", "replan", "finish"):
            verdict = "continue"
        if verdict == "replan" and replans >= cfg.max_replans:
            logger.warning("[Reviewer] replan cap reached; forcing finish")
            verdict, reason = "finish", "达到重规划上限，强制结束"
        if verdict == "finish" and not final_answer:
            final_answer = _executor_summary(state)

    # ---- bookkeeping -------------------------------------------------------
    update: dict = {"verdict": verdict, "verdict_reason": reason, "react_iteration": 0}

    if verdict == "continue":
        if idx < len(plan):
            plan[idx]["status"] = "done"
            plan[idx]["result"] = _executor_summary(state)[:300]
        next_idx = idx + 1
        if next_idx >= len(plan):
            # Everything checked off: finish with the executor's summary.
            logger.info("[Reviewer] all steps done; finishing")
            update["verdict"] = "finish"
            update["verdict_reason"] = "全部步骤完成"
            update["final_answer"] = final_answer or _executor_summary(state)
        else:
            update["current_step"] = next_idx
        update["plan"] = plan
    elif verdict == "replan":
        if idx < len(plan):
            plan[idx]["status"] = "failed"
            plan[idx]["result"] = reason
        update["plan"] = plan
    else:  # finish
        if idx < len(plan) and plan[idx].get("status") != "done":
            plan[idx]["status"] = "done" if final_answer else "failed"
            plan[idx]["result"] = reason
        update["plan"] = plan
        update["final_answer"] = final_answer or _executor_summary(state)

    update["step_notes"] = [f"[Reviewer] verdict={update['verdict']} | {reason or update.get('verdict_reason', '')}"]
    logger.info("[Reviewer] %s (%s)", update["verdict"], reason or "-")
    return update


def route_after_reviewer(state: AgentState) -> str:
    verdict = state.get("verdict", "finish")
    if verdict == "continue":
        return "executor"
    if verdict == "replan":
        return "planner"
    return "finish"
